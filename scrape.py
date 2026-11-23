"""Collects 7 days of showtimes for every cinema in Bahrain from elCinema
and writes them to data.json for the app page.

elCinema loads the other days with JavaScript, so this uses a real
(headless) Chromium browser and clicks each day tab."""

import datetime
import json
import os
import re
import sys
import time

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from playwright.sync_api import sync_playwright

BASE = "https://elcinema.com"
LIST_URLS = [f"{BASE}/en/theater/6/"] + [f"{BASE}/en/theater/6/?page={n}" for n in (2, 3)]
DAYS_AHEAD = 7
UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

# Only keep the Bahrain chains (the listing pages also show some ads for other countries).
CHAINS = ["vox", "cineco", "epix", "mukta", "cinepolis", "cinépolis", "reel", "liwan"]

THEATER_LINK = re.compile(r"^/en/theater/(\d{6,}|\d+/\d+/\d+)/?$")
WORK_LINK = re.compile(r"^/en/work/(\d+)/?$")
TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*([ap]m)\b", re.I)
PRICE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*BHD", re.I)
DAY_NAMES = "Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday"
DATE_RE = re.compile(rf"^({DAY_NAMES})\s+(\d{{1,2}})\s+([A-Za-z]+)$")
EXPERIENCES = {
    "standard", "imax", "max", "vip", "gold", "kids", "4dx", "screenx", "theatre",
    "platinum", "dolby", "dolby atmos", "premium", "junior", "luxe", "3d", "2d", "family",
}
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}

# Marks every clickable day tab with data-cc-day="<text>", one per distinct day.
MARK_TABS_JS = r"""
(re) => {
  const rx = new RegExp(re);
  const seen = new Set(), out = [];
  const els = document.querySelectorAll('li, a, button, span, div, label');
  for (const el of els) {
    const t = (el.innerText || '').trim().replace(/\s+/g, ' ');
    if (!rx.test(t) || seen.has(t)) continue;
    // Prefer the innermost element holding just this text.
    if ([...el.children].some(c => (c.innerText || '').trim().replace(/\s+/g, ' ') === t)) continue;
    if (/^H[1-6]$/.test(el.tagName) || el.closest('h1,h2,h3,h4,h5,h6')) continue;
    seen.add(t);
    el.setAttribute('data-cc-day', t);
    out.push(t);
  }
  return out;
}
"""


def to_iso(day_text, today):
    m = DATE_RE.match(day_text)
    if not m:
        return None
    day, month = int(m.group(2)), MONTHS.get(m.group(3).lower())
    if not month:
        return None
    year = today.year
    if month < today.month - 6:      # December -> January rollover
        year += 1
    elif month > today.month + 6:
        year -= 1
    try:
        return datetime.date(year, month, day).isoformat()
    except ValueError:
        return None


def load(page, url):
    for attempt in range(3):
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            if resp and resp.status == 200:
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                return True
            print(f"  {url} -> HTTP {resp.status if resp else '?'}")
        except Exception as e:
            print(f"  {url} -> {e}")
        time.sleep(3 * (attempt + 1))
    return False


def find_theaters(page):
    theaters = {}
    for url in LIST_URLS:
        if not load(page, url):
            continue
        soup = BeautifulSoup(page.content(), "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"].replace(BASE, "").split("?")[0]
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
    return theaters


def parse_html(html, today, forced_date=None):
    """Walks the page in order: film link -> date heading -> format -> times."""
    soup = BeautifulSoup(html, "html.parser")
    if not soup.body:
        return None, [], {}
    h1 = soup.find("h1")
    name = h1.get_text(" ", strip=True) if h1 else None

    shows, posters = [], {}
    movie, date, exp = None, forced_date, "Standard"
    for el in soup.body.descendants:
        if isinstance(el, Tag):
            if el.has_attr("data-cc-day"):
                continue
            if el.name == "a":
                m = WORK_LINK.match((el.get("href") or "").replace(BASE, "").split("?")[0])
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
        # Skip the text of the day-tab strip itself.
        if any(p.has_attr("data-cc-day") for p in el.parents if isinstance(p, Tag)):
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
                    "movie": movie["id"], "title": movie["title"], "date": date,
                    "time": f"{int(h)}:{mnt} {ap.lower()}", "exp": exp,
                    "price": price.group(1) if price else None,
                })
    return name, shows, posters


def scrape_theater(page, url, today, debug):
    if not load(page, url):
        return None, [], {}
    name, all_shows, posters = None, [], {}

    def collect(forced=None):
        nonlocal name
        n, s, p = parse_html(page.content(), today, forced)
        name = name or n
        all_shows.extend(s)
        for k, v in p.items():
            posters.setdefault(k, v)
        return len(s)

    tabs = page.evaluate(MARK_TABS_JS, DATE_RE.pattern)
    if debug is not None:
        debug["tabs"] = tabs
        debug["html_before"] = page.content()
    if not tabs:
        collect()
    for t in tabs[:DAYS_AHEAD]:
        iso = to_iso(t, today)
        try:
            page.locator(f'[data-cc-day="{t}"]').first.click(timeout=8000, force=True)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            page.wait_for_timeout(1200)
        except Exception as e:
            print(f"    could not click {t}: {e}")
        # Re-mark in case the tab strip was re-rendered.
        page.evaluate(MARK_TABS_JS, DATE_RE.pattern)
        got = collect()
        if got == 0 and iso:
            collect(iso)
        if debug is not None and "html_after" not in debug and t != tabs[0]:
            debug["html_after"] = page.content()
    return name, all_shows, posters


def main():
    tz = datetime.timezone(datetime.timedelta(hours=3))
    today = datetime.datetime.now(tz).date()
    last = (today + datetime.timedelta(days=DAYS_AHEAD - 1)).isoformat()

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(user_agent=UA, locale="en-US", viewport={"width": 430, "height": 932})
        page = ctx.new_page()
        xhr = []
        page.on("request", lambda r: xhr.append(r.url) if r.resource_type in ("xhr", "fetch") else None)

        theaters = find_theaters(page)
        print(f"Found {len(theaters)} cinemas")

        movies, cinemas, debug = {}, set(), {}
        for i, (url, listed) in enumerate(theaters.items()):
            name, shows, posters = scrape_theater(page, url, today, debug if i == 0 else None)
            name = name or listed
            days = sorted({s["date"] for s in shows})
            print(f"  {name}: {len(shows)} showtimes over {len(days)} days {days[:1]}..{days[-1:]}")
            for s in shows:
                if not (today.isoformat() <= s["date"] <= last):
                    continue
                cinemas.add(name)
                mv = movies.setdefault(s["movie"], {"id": s["movie"], "title": s["title"], "poster": None, "shows": []})
                mv["poster"] = mv["poster"] or posters.get(s["movie"])
                entry = {"cinema": name, "url": url, "date": s["date"], "time": s["time"],
                         "exp": s["exp"], "price": s["price"]}
                if entry not in mv["shows"]:
                    mv["shows"].append(entry)
        browser.close()

    # Save a sample page so problems can be diagnosed later.
    os.makedirs("debug", exist_ok=True)
    with open("debug/info.json", "w", encoding="utf-8") as f:
        json.dump({"tabs": debug.get("tabs"), "xhr": sorted(set(xhr))[:200]}, f, indent=1)
    for k in ("html_before", "html_after"):
        if debug.get(k):
            with open(f"debug/{k}.html", "w", encoding="utf-8") as f:
                f.write(debug[k])

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
    days = sorted({s["date"] for m in out for s in m["shows"]})
    print(f"Saved {len(out)} films, {total} showtimes, {len(cinemas)} cinemas, days: {days}")


if __name__ == "__main__":
    main()
