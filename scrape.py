"""Collects showtimes for every cinema in Bahrain and writes data.json for the app.

Source today: elCinema (one page per cinema, a date dropdown for other days).
Each film also gets its language/country so the app can hide Indian films.

At the end it also visits each cinema chain's own website and saves what the
site loads (debug/sites/) so direct scrapers can be written for each chain."""

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
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Only keep the Bahrain chains (the listing pages also show some ads for other countries).
CHAINS = ["vox", "cineco", "epix", "mukta", "cinepolis", "cinépolis", "reel", "liwan"]

# Each chain's own site, visited for reconnaissance.
CHAIN_SITES = {
    "vox": "https://bhr.voxcinemas.com/showtimes",
    "cineco": "https://www.cineco.net/country-chooser.php?country=bhr",
    "cinepolis": "https://cinepolisgulf.com/",
    "epix": "https://www.epixcinemas.com/",
    "mukta": "https://muktaa2cinemas.com/bahrain",
    "reel": "https://www.reelcinemas.com/en-bh/",
}
NOISE = re.compile(r"google|doubleclick|facebook|analytics|hotjar|clarity|tiktok|snap|"
                   r"criteo|adservice|gstatic|fonts\.|sentry|newrelic|onesignal|appier", re.I)

THEATER_LINK = re.compile(r"^/en/theater/(\d{6,}|\d+/\d+/\d+)/?$")
WORK_LINK = re.compile(r"^/en/work/(\d+)/?$")
META_LINK = re.compile(r"/en/index/work/(language|country|genre)/")
TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*([ap]m)\b", re.I)
PRICE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*BHD", re.I)
DAY_NAMES = "Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday"
DATE_RE = re.compile(rf"^({DAY_NAMES})\s+(\d{{1,2}})\s+([A-Za-z]+)$")
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}

INDIAN_LANGS = {"hindi", "tamil", "telugu", "malayalam", "kannada", "bengali", "marathi",
                "punjabi", "gujarati", "odia", "urdu", "bhojpuri", "tulu", "konkani"}


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
    """Walks the page in order: film link -> language/country -> date -> format -> times."""
    soup = BeautifulSoup(html, "html.parser")
    if not soup.body:
        return None, [], {}, {}
    h1 = soup.find("h1")
    name = h1.get_text(" ", strip=True) if h1 else None

    shows, posters, meta = [], {}, {}
    movie, date, exp = None, forced_date, "Standard"
    for el in soup.body.descendants:
        if isinstance(el, Tag):
            if el.name == "a":
                href = (el.get("href") or "").replace(BASE, "")
                m = WORK_LINK.match(href.split("?")[0])
                if m:
                    img = el.find("img")
                    if img:
                        src = img.get("data-src") or img.get("src")
                        if src and not src.startswith("data:"):
                            posters.setdefault(m.group(1), src if src.startswith("http") else BASE + src)
                    title = el.get_text(" ", strip=True)
                    if title:
                        movie = {"id": m.group(1), "title": title}
                        meta.setdefault(movie["id"], {"lang": [], "country": [], "genres": []})
                        exp = "Standard"
                    continue
                mm = META_LINK.search(href)
                if mm and movie:
                    key = {"language": "lang", "country": "country", "genre": "genres"}[mm.group(1)]
                    val = el.get_text(" ", strip=True)
                    if val and val not in meta[movie["id"]][key]:
                        meta[movie["id"]][key].append(val)
            elif el.name == "h6" and "section-title" in (el.get("class") or []):
                txt = el.get_text(" ", strip=True)
                if txt:
                    exp = txt                       # e.g. "VIP", "IMAX", "Standard Arabic Dubbed"
            continue
        if not isinstance(el, NavigableString) or isinstance(el, Comment):
            continue
        parent = el.parent
        if parent and parent.name in ("script", "style", "noscript", "option", "select"):
            continue
        if parent and parent.name == "li" and "exp-color" in (parent.get("class") or []):
            continue                                # the "Available Experiences" summary
        text = " ".join(el.split())
        if not text:
            continue
        iso = to_iso(text, today)
        if iso:
            date = iso
            continue
        if movie and date:
            price = PRICE_RE.search(text)
            for h, mnt, ap in TIME_RE.findall(text):
                shows.append({
                    "movie": movie["id"], "title": movie["title"], "date": date,
                    "time": f"{int(h)}:{mnt} {ap.lower()}", "exp": exp,
                    "price": price.group(1) if price else None,
                })
    return name, shows, posters, meta


SELECTOR = "#theater-showtimes-date-selector"
WAIT_FOR_DAY_JS = """(lbl) => [...document.querySelectorAll('h2, h4')]
  .some(e => (e.innerText || '').trim().replace(/\\s+/g, ' ') === lbl)"""


def scrape_theater(page, url, today, debug):
    """elCinema shows one day at a time; the other days are in a date dropdown."""
    if not load(page, url):
        return None, [], {}, {}
    name, all_shows, posters, meta = None, [], {}, {}

    def collect(forced=None):
        nonlocal name
        n, s, p, mt = parse_html(page.content(), today, forced)
        name = name or n
        all_shows.extend(s)
        for k, v in p.items():
            posters.setdefault(k, v)
        for k, v in mt.items():
            cur = meta.setdefault(k, {"lang": [], "country": [], "genres": []})
            for f in cur:
                cur[f] += [x for x in v[f] if x not in cur[f]]
        return len(s)

    options = []
    if page.query_selector(SELECTOR):
        options = page.eval_on_selector_all(
            f"{SELECTOR} option",
            "os => os.map(o => [o.value, (o.textContent || '').trim().replace(/\\s+/g, ' ')])")
    if debug is not None:
        debug["tabs"] = options
        debug["html_before"] = page.content()

    def choose(value, label):
        """Picks a date in the dropdown; True once that day's heading is on screen."""
        try:
            if not page.query_selector(SELECTOR):
                load(page, url)
            page.select_option(SELECTOR, value=value, timeout=10000)
            page.wait_for_function(WAIT_FOR_DAY_JS, arg=label, timeout=15000)
        except Exception:
            return False
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(800)
        return True

    collect()  # the day that is already showing
    for value, label in options[1:DAYS_AHEAD]:
        ok = choose(value, label)
        if not ok:                       # try once more from a fresh page
            load(page, url)
            ok = choose(value, label)
        if not ok:
            print(f"    could not load {label}")
            continue
        got = collect()
        if got == 0 and value[:4].isdigit():
            collect(value)
        if debug is not None and "html_after" not in debug:
            debug["html_after"] = page.content()
    return name, all_shows, posters, meta


def is_indian(m):
    langs = {x.lower() for x in m.get("lang", [])}
    return "India" in m.get("country", []) or bool(langs & INDIAN_LANGS)


def probe_chain_sites(browser):
    """Saves what each chain's website loads, so direct scrapers can be written later."""
    os.makedirs("debug/sites", exist_ok=True)
    for key, url in CHAIN_SITES.items():
        report = {"start": url}
        ctx = browser.new_context(user_agent=DESKTOP_UA, locale="en-US",
                                  viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        responses = []
        page.on("response", lambda r: responses.append(r))
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            report["status"] = resp.status if resp else None
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            for _ in range(4):                       # trigger lazy loading
                page.mouse.wheel(0, 2500)
                page.wait_for_timeout(1200)
            report["final_url"] = page.url
            report["title"] = page.title()
            report["links"] = page.eval_on_selector_all(
                "a[href]", "as => [...new Set(as.map(a => a.href))]")[:400]
            html = page.content()
            with open(f"debug/sites/{key}.html", "w", encoding="utf-8") as f:
                f.write(html[:600000])
        except Exception as e:
            report["error"] = str(e)[:500]
        calls = []
        for r in responses:
            try:
                if NOISE.search(r.url):
                    continue
                ct = r.headers.get("content-type", "")
                if r.request.resource_type not in ("xhr", "fetch") and "json" not in ct:
                    continue
                body = ""
                try:
                    body = r.text()[:6000]
                except Exception:
                    pass
                calls.append({"url": r.url, "method": r.request.method, "status": r.status,
                              "type": ct, "post": (r.request.post_data or "")[:1000], "body": body})
            except Exception:
                pass
        report["api_calls"] = calls[:120]
        with open(f"debug/sites/{key}.json", "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
        print(f"  probe {key}: status={report.get('status')} api_calls={len(calls)} "
              f"error={report.get('error', '')[:80]}")
        ctx.close()


def main():
    tz = datetime.timezone(datetime.timedelta(hours=3))
    today = datetime.datetime.now(tz).date()
    last = (today + datetime.timedelta(days=DAYS_AHEAD - 1)).isoformat()

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(user_agent=UA, locale="en-US", viewport={"width": 430, "height": 932})
        page = ctx.new_page()

        theaters = find_theaters(page)
        print(f"Found {len(theaters)} cinemas")

        movies, cinemas, debug, meta_all = {}, set(), {}, {}
        for i, (url, listed) in enumerate(theaters.items()):
            name, shows, posters, meta = scrape_theater(page, url, today, debug if i == 0 else None)
            name = name or listed
            days = sorted({s["date"] for s in shows})
            print(f"  {name}: {len(shows)} showtimes over {len(days)} days")
            for k, v in meta.items():
                cur = meta_all.setdefault(k, {"lang": [], "country": [], "genres": []})
                for f in cur:
                    cur[f] += [x for x in v[f] if x not in cur[f]]
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
        ctx.close()

        try:
            probe_chain_sites(browser)
        except Exception as e:
            print(f"Probe failed (ignored): {e}")
        browser.close()

    for m in movies.values():
        md = meta_all.get(m["id"], {})
        m["lang"] = md.get("lang", [])
        m["country"] = md.get("country", [])
        m["genres"] = md.get("genres", [])
        m["indian"] = is_indian(m)

    os.makedirs("debug", exist_ok=True)
    with open("debug/info.json", "w", encoding="utf-8") as f:
        json.dump({"tabs": debug.get("tabs")}, f, indent=1)
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
    print(f"Saved {len(out)} films ({sum(m['indian'] for m in out)} Indian), "
          f"{total} showtimes, {len(cinemas)} cinemas, days: {days}")


if __name__ == "__main__":
    main()
