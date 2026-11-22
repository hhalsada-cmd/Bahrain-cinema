"""Collects today's and upcoming showtimes for every cinema in Bahrain
from elCinema and writes them to data.json for the app page."""

import datetime
import json
import re
import sys
import time

import requests
from bs4 import BeautifulSoup, Comment, NavigableString, Tag

BASE = "https://elcinema.com"
LIST_URLS = [f"{BASE}/en/theater/6/"] + [f"{BASE}/en/theater/6/?page={n}" for n in (2, 3)]
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "en",
}

# Only keep the Bahrain chains (the listing pages also show some ads for other countries).
CHAINS = ["vox", "cineco", "epix", "mukta", "cinepolis", "cinépolis", "reel", "liwan"]

THEATER_LINK = re.compile(r"^/en/theater/(\d{6,}|\d+/\d+/\d+)/?$")
WORK_LINK = re.compile(r"^/en/work/(\d+)/?$")
TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*([ap]m)\b", re.I)
PRICE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*BHD", re.I)
DATE_RE = re.compile(
    r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+(\d{1,2})\s+([A-Za-z]+)$"
)
EXPERIENCES = {
    "standard", "imax", "max", "vip", "gold", "kids", "4dx", "screenx", "theatre",
    "platinum", "dolby", "dolby atmos", "premium", "junior", "luxe", "3d", "2d", "family",
}
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}


def get(url):
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                return BeautifulSoup(r.text, "html.parser")
            print(f"  {url} -> HTTP {r.status_code}")
        except requests.RequestException as e:
            print(f"  {url} -> {e}")
        time.sleep(3 * (attempt + 1))
    return None


def to_iso(day_text, today):
    m = DATE_RE.match(day_text)
    if not m:
        return None
    day, month = int(m.group(2)), MONTHS.get(m.group(3).lower())
    if not month:
        return None
    year = today.year
    # Handle the December -> January rollover.
    if month < today.month - 6:
        year += 1
    elif month > today.month + 6:
        year -= 1
    try:
        return datetime.date(year, month, day).isoformat()
    except ValueError:
        return None


def find_theaters():
    theaters = {}
    for url in LIST_URLS:
        soup = get(url)
        if not soup:
            continue
        for a in soup.find_all("a", href=True):
            href = a["href"].split("?")[0]
            if not THEATER_LINK.match(href):
                continue
            name = a.get_text(" ", strip=True)
            if not name or name.lower() == "favourite":
                continue
            if not any(c in name.lower() for c in CHAINS):
                continue
            full = BASE + href
            if len(name) > len(theaters.get(full, "")):
                theaters[full] = name
        time.sleep(1)
    return theaters


def parse_theater(url, name, today):
    soup = get(url)
    if not soup or not soup.body:
        return name, [], {}
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        name = h1.get_text(" ", strip=True)

    shows, posters = [], {}
    movie, date, exp = None, None, "Standard"
    for el in soup.body.descendants:
        if isinstance(el, Tag):
            if el.name == "a":
                m = WORK_LINK.match((el.get("href") or "").split("?")[0])
                if m:
                    img = el.find("img")
                    if img:
                        src = img.get("data-src") or img.get("src")
                        if src and not src.startswith("data:"):
                            posters.setdefault(m.group(1), src if src.startswith("http") else BASE + src)
                    title = el.get_text(" ", strip=True)
                    if title:
                        movie = {"id": m.group(1), "title": title}
                        exp = "Standard"
            continue
        if not isinstance(el, NavigableString) or isinstance(el, Comment):
            continue
        if el.parent and el.parent.name in ("script", "style", "noscript"):
            continue
        text = " ".join(el.split())
        if not text:
            continue
        iso = to_iso(text, today)
        if iso:
            date = iso
            continue
        if text.lower() in EXPERIENCES:
            exp = text
            continue
        if movie and date:
            price = PRICE_RE.search(text)
            for h, mnt, ap in TIME_RE.findall(text):
                shows.append({
                    "movie": movie["id"],
                    "title": movie["title"],
                    "date": date,
                    "time": f"{int(h)}:{mnt} {ap.lower()}",
                    "exp": exp,
                    "price": price.group(1) if price else None,
                })
    return name, shows, posters


def main():
    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3))).date()
    theaters = find_theaters()
    print(f"Found {len(theaters)} cinemas")

    movies, cinemas = {}, []
    for url, name in theaters.items():
        name, shows, posters = parse_theater(url, name, today)
        print(f"  {name}: {len(shows)} showtimes")
        if shows:
            cinemas.append(name)
        for s in shows:
            if s["date"] < today.isoformat():
                continue
            mv = movies.setdefault(s["movie"], {"id": s["movie"], "title": s["title"], "poster": None, "shows": []})
            mv["poster"] = mv["poster"] or posters.get(s["movie"])
            entry = {"cinema": name, "date": s["date"], "time": s["time"], "exp": s["exp"], "price": s["price"]}
            if entry not in mv["shows"]:
                mv["shows"].append(entry)
        time.sleep(1)

    total = sum(len(m["shows"]) for m in movies.values())
    if total == 0:
        print("No showtimes found. Keeping the old data.json.")
        sys.exit(1)

    out = sorted(movies.values(), key=lambda m: (-len({s["cinema"] for s in m["shows"]}), m["title"]))
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump({
            "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "cinemas": sorted(cinemas),
            "movies": out,
        }, f, ensure_ascii=False, indent=1)
    print(f"Saved {len(out)} films, {total} showtimes, {len(cinemas)} cinemas")


if __name__ == "__main__":
    main()
