"""Bahrain cinema showtimes, straight from each cinema chain's own website.

Direct sources:   Mukta A2 (its booking API), Cineco (its film pages), Epix (its booking API).
Being mapped:     VOX, Reel, Cinépolis. Their sites are recorded into debug/sites2/ so
                  direct scrapers can be written. Until a chain's direct scraper works,
                  elCinema fills in for that chain only and the app flags it as a fallback.

Writes data.json (films, showtimes, and a health report per cinema chain)."""

import datetime
import json
import os
import re
import sys
import time
import traceback

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from playwright.sync_api import sync_playwright

TZ = datetime.timezone(datetime.timedelta(hours=3))
TODAY = datetime.datetime.now(TZ).date()
DAYS_AHEAD = 7
LAST = TODAY + datetime.timedelta(days=DAYS_AHEAD - 1)

DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

INDIAN_LANGS = {"hindi", "tamil", "telugu", "malayalam", "kannada", "bengali", "marathi",
                "punjabi", "gujarati", "odia", "urdu", "bhojpuri", "tulu", "konkani"}
KNOWN_LANGS = INDIAN_LANGS | {"english", "arabic", "japanese", "korean", "french", "spanish",
                              "russian", "turkish", "chinese", "filipino", "tagalog", "italian"}

TIME_RE = re.compile(r"\b(\d{1,2})[:.](\d{2})\s*([ap]\.?m\.?)?", re.I)
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

os.makedirs("debug/sites2", exist_ok=True)


# ----------------------------------------------------------------- helpers

def fmt_time(h, m, ap=None):
    h, m = int(h), int(m)
    if ap:
        ap = ap.lower().replace(".", "")
        h = h % 12 + (12 if ap == "pm" else 0)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return f"{(h % 12) or 12}:{m:02d} {'pm' if h >= 12 else 'am'}"


def parse_date(text, value=""):
    """Reads a date from text like '2026-11-25', '25/11/2026', 'Wed, 25 Nov', 'Tomorrow'."""
    for s in (value or "", text or ""):
        s = str(s).strip()
        if not s:
            continue
        low = s.lower()
        if low.startswith("today"):
            return TODAY
        if low.startswith("tomorrow"):
            return TODAY + datetime.timedelta(days=1)
        m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
        if m:
            try:
                return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass
        m = re.search(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})\b", s)
        if m:
            a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            y += 2000 if y < 100 else 0
            for d, mo in ((a, b), (b, a)):          # day-first, as used in Bahrain
                try:
                    return datetime.date(y, mo, d)
                except ValueError:
                    continue
        m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?[\s-]+([A-Za-z]{3,9})\.?(?:[\s,-]+(\d{4}))?", s)
        m2 = re.search(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?", s)
        for mm, di, mi in ((m, 1, 2), (m2, 2, 1)):
            if not mm:
                continue
            mon = MONTHS.get(mm.group(mi)[:3].lower())
            if not mon:
                continue
            y = int(mm.group(3)) if mm.group(3) else TODAY.year
            if not mm.group(3) and mon < TODAY.month - 6:
                y += 1
            try:
                return datetime.date(y, mon, int(mm.group(di)))
            except ValueError:
                continue
    return None


def clean_title(t):
    """'Ramayana: Rise of a Legend (Hindi) (PG-13)' -> ('Ramayana: Rise of a Legend', ['Hindi'])."""
    t = " ".join((t or "").split())
    langs = []
    for p in re.findall(r"\(([^)]*)\)", t):
        if p.strip().lower() in KNOWN_LANGS:
            langs.append(p.strip().title())
    t = re.sub(r"\s*\([^)]*\)", "", t).strip(" -–")
    return t, langs


def title_key(t):
    t, _ = clean_title(t)
    t = t.lower().replace("&", "and")
    t = re.sub(r"[’'`]", "", t)
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    words = t.split()
    while words and words[-1] in KNOWN_LANGS:
        words.pop()
    if words and words[0] == "the":
        words = words[1:]
    return "".join(words)


class Collector:
    def __init__(self):
        self.shows = []       # dicts
        self.films = {}       # key -> {title, poster, lang:set, genres:set, chains:set}
        self.health = {}      # chain -> report

    def film(self, title, poster=None, langs=(), genres=()):
        key = title_key(title)
        if not key:
            return None
        clean, tl = clean_title(title)
        f = self.films.setdefault(key, {"title": clean, "poster": None, "lang": set(),
                                        "genres": set(), "chains": set()})
        if len(clean) > len(f["title"]) and not clean.isupper():
            f["title"] = clean
        f["poster"] = f["poster"] or poster
        f["lang"].update(x.title() for x in list(langs) + tl if x)
        f["genres"].update(g for g in genres if g)
        return key

    def add(self, chain, cinema, title, date, time_str, exp="Standard", book=None,
            poster=None, langs=(), genres=(), price=None, src="direct"):
        if not date or not time_str or not (TODAY <= date <= LAST):
            return
        key = self.film(title, poster, langs, genres)
        if not key:
            return
        self.films[key]["chains"].add(chain)
        row = {"film": key, "chain": chain, "cinema": cinema, "date": date.isoformat(),
               "time": time_str, "exp": (exp or "Standard").strip() or "Standard",
               "book": book, "price": price, "src": src}
        sig = (key, cinema, row["date"], time_str, row["exp"])
        if sig not in {(s["film"], s["cinema"], s["date"], s["time"], s["exp"]) for s in self.shows[-400:]}:
            self.shows.append(row)

    def count(self, chain, src=None):
        return [s for s in self.shows if s["chain"] == chain and (src is None or s["src"] == src)]


def report(col, chain, ok, note=""):
    shows = col.count(chain)
    days = sorted({s["date"] for s in shows})
    srcs = {s["src"] for s in shows}
    status = "ok" if ok and shows else ("fallback" if shows and "elcinema" in srcs else "fail")
    note = (note or "").strip().splitlines()[0][:160] if note else ""
    col.health[chain] = {"status": status, "shows": len(shows), "days": len(days),
                         "last_day": days[-1] if days else None,
                         "cinemas": sorted({s["cinema"] for s in shows}), "note": note}
    print(f"[{chain}] {status}: {len(shows)} showtimes over {len(days)} days. {note}")


def save_debug(name, obj):
    try:
        path = f"debug/sites2/{name}"
        with open(path, "w", encoding="utf-8") as f:
            if isinstance(obj, str):
                f.write(obj[:800000])
            else:
                json.dump(obj, f, ensure_ascii=False, indent=1, default=str)
    except Exception:
        pass


def record_calls(page, store, host_re):
    def on(resp):
        if host_re.search(resp.url):
            store.append(resp)
    page.on("response", on)


def dump_calls(responses, limit=60, body_cap=60000):
    out = []
    for r in responses[:limit]:
        try:
            ct = r.headers.get("content-type", "")
            if not any(x in ct for x in ("json", "x-component", "text/plain", "text/html")):
                continue
            body = ""
            try:
                body = r.text()[:body_cap]
            except Exception:
                pass
            out.append({"url": r.url, "method": r.request.method, "status": r.status, "type": ct,
                        "post": (r.request.post_data or "")[:2000], "body": body})
        except Exception:
            pass
    return out


# ----------------------------------------------------------------- Mukta A2 (Juffair Mall)

MUKTA = "https://www.muktaa2cinemas.com/bahrain"


def options(html_str):
    soup = BeautifulSoup(html_str or "", "html.parser")
    return [(o.get("value", ""), " ".join(o.get_text(" ").split())) for o in soup.find_all("option")
            if o.get("value", "0") not in ("", "0")]


def mukta_post(ctx, path, payload):
    r = ctx.request.post(f"{MUKTA}/Home/{path}", data=json.dumps(payload), timeout=30000, headers={
        "Content-Type": "application/json; charset=utf-8", "X-Requested-With": "XMLHttpRequest",
        "Referer": MUKTA, "Origin": "https://www.muktaa2cinemas.com"})
    txt = r.text()
    try:
        val = json.loads(txt)
    except Exception:
        val = txt
    return val if isinstance(val, str) else json.dumps(val)


def scrape_mukta(ctx, col):
    samples = {}
    try:
        ctx.request.get(MUKTA, timeout=30000)                      # session cookie
        city = "59"
        first = mukta_post(ctx, "getcityfilmstatus", {"Cityid": city})
        parts = first.split("~")
        theatres = options(parts[1]) if len(parts) > 1 else [("122", "JUFFAIR MALL, JUFFAIR")]
        samples["theatres"] = theatres
        for tid, tname in theatres:
            cinema = "Mukta A2 " + tname.split(",")[0].title()
            films = options(mukta_post(ctx, "getcityfilmthetrestatus1", {"TheatreId": tid, "Cityid": city}))
            for fid, ftitle in films:
                dates_html = mukta_post(ctx, "Showdatetime", {"TheaterId": tid, "FilmId": fid})
                if len(samples) < 4:
                    samples[f"dates_{fid}"] = dates_html[:3000]
                for dval, dtext in options(dates_html):
                    d = parse_date(dtext, dval)
                    if not d or d > LAST:
                        continue
                    times_html = mukta_post(ctx, "Showtime", {"CityId": city, "TheaterId": tid,
                                                              "FilmId": fid, "showtime": dval})
                    if len(samples) < 8:
                        samples[f"times_{fid}_{dval}"] = times_html[:3000]
                    for tval, ttext in options(times_html):
                        m = TIME_RE.search(ttext)
                        if not m:
                            continue
                        t = fmt_time(*m.groups())
                        rest = (ttext[:m.start()] + " " + ttext[m.end():]).strip(" -–|/()")
                        exp = rest if rest and not TIME_RE.search(rest) else "Standard"
                        col.add("Mukta A2", cinema, ftitle, d, t, exp, book=MUKTA)
                    time.sleep(0.2)
        report(col, "Mukta A2", True)
    except Exception as e:
        samples["error"] = traceback.format_exc()
        report(col, "Mukta A2", False, f"Error: {str(e)[:120]}")
    save_debug("mukta.json", samples)


# ----------------------------------------------------------------- Cineco (Seef, Juffair, Wadi Al Sail, Al Hamra)

CINECO = "https://bahrain.cineco.net"
CINECO_SITES = {"seef": "Cineco Seef", "oasis": "Cineco Juffair", "wadi": "Cineco Wadi Al Sail",
                "hamra": "Cineco Al Hamra"}


def scrape_cineco(ctx, col):
    samples = {}
    try:
        links = set()
        for u in (f"{CINECO}/now-showing/", f"{CINECO}/"):
            r = ctx.request.get(u, timeout=40000)
            soup = BeautifulSoup(r.text(), "html.parser")
            for a in soup.find_all("a", href=True):
                h = a["href"].split("#")[0]
                if "/amy_movie/" in h:
                    links.add(h if h.startswith("http") else CINECO + h)
        samples["films"] = sorted(links)
        for url in sorted(links):
            html = ctx.request.get(url, timeout=40000).text()
            if "sample_page" not in samples:
                samples["sample_page"] = url
                save_debug("cineco_film.html", html)
            parse_cineco_film(html, url, col)
            time.sleep(0.3)
        report(col, "Cineco", True, "" if col.count("Cineco") else "Film pages had no times")
    except Exception as e:
        samples["error"] = traceback.format_exc()
        report(col, "Cineco", False, f"Error: {str(e)[:120]}")
    save_debug("cineco.json", samples)


def parse_cineco_film(html, url, col):
    soup = BeautifulSoup(html, "html.parser")
    for bad in soup.select("header, nav, footer, script, style, noscript, .amy-site-nav, .amy-primary-navigation"):
        bad.decompose()
    h1 = soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else url.rstrip("/").split("/")[-1].replace("-", " ")
    og = soup.find("meta", property="og:image")
    poster = og["content"] if og and og.get("content") else None
    langs = [a.get_text(strip=True) for a in soup.select('a[href*="/movie_language/"]')]
    book = None
    for a in soup.find_all("a", href=True):
        if "q-tickets.com" in a["href"]:
            book = a["href"]
            break
    cinema, exp, date = None, "Standard", TODAY
    for el in soup.body.descendants if soup.body else []:
        if isinstance(el, Tag):
            if el.name == "a":
                m = re.search(r"/cinema/([a-z-]+)/?", el.get("href", ""))
                if m and m.group(1) in CINECO_SITES:
                    cinema, exp = CINECO_SITES[m.group(1)], "Standard"
            continue
        if not isinstance(el, NavigableString) or isinstance(el, Comment):
            continue
        text = " ".join(el.split())
        if not text:
            continue
        hall = re.fullmatch(r"\(\s*([^)]{2,30}?)\s*\)", text)
        if hall and cinema:
            exp = hall.group(1)
            continue
        if len(text) < 40 and not TIME_RE.search(text):
            d = parse_date(text)
            if d and TODAY <= d <= LAST:
                date = d
                continue
        if cinema:
            for m in re.finditer(r"(\d{1,2}):(\d{2})\s*([ap]m)", text, re.I):
                col.add("Cineco", cinema, title, date, fmt_time(*m.groups()), exp,
                        book=book or url, poster=poster, langs=langs)


# ----------------------------------------------------------------- Epix (Dana Mall, Al Liwan)

EPIX_API = "https://api.epixcinemas.com/api/v3/external"
EPIX_WEB = "https://www.epixcinemas.com"
DT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})")


def walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v)


def epix_show_from(d, cine_names):
    """Pulls (cinema, date, time, format) out of one JSON object if it looks like a show."""
    when = None
    for k, v in d.items():
        if isinstance(v, str) and re.search(r"show|start|ss_", k, re.I) and \
                not re.search(r"creat|updat|release|end|cutoff|close|open|booking", k, re.I):
            m = DT_RE.match(v)
            if m:
                when = (m.group(1), m.group(2), m.group(3))
                break
    if not when:
        dk = next((v for k, v in d.items() if isinstance(v, str) and re.search(r"date", k, re.I)
                   and re.search(r"show|ss_|start", k, re.I) and re.match(r"^\d{4}-\d{2}-\d{2}", v)), None)
        tk = next((v for k, v in d.items() if isinstance(v, str) and re.search(r"time", k, re.I)
                   and re.search(r"show|ss_|start", k, re.I) and re.match(r"^\d{1,2}:\d{2}", v)), None)
        if dk and tk:
            when = (dk[:10],) + tuple(tk.split(":")[:2])
    if not when:
        return None
    cinema = None
    for k, v in d.items():
        if isinstance(v, str) and re.search(r"cine(ma)?_name", k, re.I) and v.strip():
            cinema = v.strip()
            break
    if not cinema:
        cid = next((d[k] for k in ("cinema_id", "cine_id") if k in d), None)
        cinema = cine_names.get(str(cid))
    exp = None
    for k in ("movie_experience", "experience", "screen_type_name", "screen_type", "seat_type",
              "mf_name", "format_name", "sf_name"):
        if isinstance(d.get(k), str) and d[k].strip():
            exp = d[k].strip()
            break
    return cinema, when, exp


def epix_cinema_label(name):
    n = (name or "").lower()
    if "liwan" in n:
        return "Epix Al Liwan"
    if "dana" in n:
        return "Epix Dana Mall"
    return ("Epix " + (name or "")).strip()


def scrape_epix(browser, col):
    samples = {}
    ctx = browser.new_context(user_agent=DESKTOP_UA, locale="en-US", viewport={"width": 1366, "height": 900})
    try:
        cin = ctx.request.get(f"{EPIX_API}/cinemas", timeout=30000).json()
        cine_names = {str(c.get("cinema_id")): c.get("cinema_name") for c in cin.get("Records", [])}
        ns = ctx.request.get(f"{EPIX_API}/now-showing-movies", timeout=30000).json()
        movies = (ns.get("Records") or {}).get("data") or []
        samples["movies"] = [{k: m.get(k) for k in ("movie_id", "content_lang_id", "movie_title", "lang_name",
                                                     "available_dates", "available_cinemas", "format_name")}
                             for m in movies]
        seen = set()
        for m in movies:
            mid, lid = m.get("movie_id"), m.get("content_lang_id") or 1
            if (mid, lid) in seen:
                continue
            seen.add((mid, lid))
            title = m.get("movie_title") or m.get("alternate_movie_name_language") or ""
            alt = (m.get("altMovieContent") or [{}])[0]
            poster = alt.get("artwork")
            genres = [g for g in (m.get("genre_name") or "").split(",") if g]
            book = f"{EPIX_WEB}/now-showing-booking/{mid}/{lid}/"
            page = ctx.new_page()
            calls = []
            record_calls(page, calls, re.compile(r"api\.epixcinemas\.com"))
            try:
                page.goto(book, wait_until="domcontentloaded", timeout=45000)
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                page.wait_for_timeout(1500)
                found = 0
                for r in calls:
                    try:
                        data = r.json()
                    except Exception:
                        continue
                    for d in walk(data):
                        got = epix_show_from(d, cine_names)
                        if not got:
                            continue
                        cinema, (ds, hh, mm), exp = got
                        try:
                            day = datetime.date.fromisoformat(ds)
                        except ValueError:
                            continue
                        col.add("Epix", epix_cinema_label(cinema), title, day, fmt_time(hh, mm), exp or "Standard",
                                book=book, poster=poster, langs=[m.get("lang_name") or ""], genres=genres)
                        found += 1
                if "booking_calls" not in samples or (found and not samples.get("booking_found")):
                    samples["booking_calls"] = dump_calls(calls, 40, 40000)
                    samples["booking_found"] = found
                    samples["booking_text"] = page.inner_text("body")[:20000]
            except Exception as e:
                samples.setdefault("page_errors", []).append(f"{book}: {e}"[:300])
            page.close()
        report(col, "Epix", True, "" if col.count("Epix") else "Booking pages gave no readable times")
    except Exception as e:
        samples["error"] = traceback.format_exc()
        report(col, "Epix", False, f"Error: {str(e)[:120]}")
    ctx.close()
    save_debug("epix.json", samples)


# ----------------------------------------------------------------- VOX, Reel, Cinépolis: deep recording

def deep_record(browser, name, steps, http1=False):
    """Visits pages, clicks a few things, and saves every data call and the visible text."""
    out = {"pages": []}
    ctx = browser.new_context(user_agent=DESKTOP_UA, locale="en-US", viewport={"width": 1366, "height": 900})
    page = ctx.new_page()
    calls = []
    page.on("response", lambda r: calls.append(r))
    for kind, arg in steps:
        entry = {"step": kind, "arg": arg}
        try:
            if kind == "goto":
                resp = page.goto(arg, wait_until="domcontentloaded", timeout=45000)
                entry["status"] = resp.status if resp else None
            elif kind == "click_text":
                page.get_by_text(re.compile(arg, re.I)).first.click(timeout=8000)
            elif kind == "click_css":
                page.locator(arg).first.click(timeout=8000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(2500)
            entry["url"] = page.url
            entry["title"] = page.title()
            entry["text"] = page.inner_text("body")[:15000]
            entry["links"] = page.eval_on_selector_all("a[href]", "as => [...new Set(as.map(a => a.href))]")[:300]
            if len(out["pages"]) < 4:
                save_debug(f"{name}_{len(out['pages'])}.html", page.content())
        except Exception as e:
            entry["error"] = str(e)[:400]
        out["pages"].append(entry)
    noise = re.compile(r"google|doubleclick|facebook|analytics|hotjar|clarity|tiktok|snap|criteo|"
                       r"grafana|yandex|sentry|onesignal|netcore|stripe|cdn-cgi|\.run\.app|on\.aws", re.I)
    out["calls"] = dump_calls([r for r in calls if not noise.search(r.url)], 80, 50000)
    save_debug(f"{name}.json", out)
    ctx.close()
    print(f"  recorded {name}: {[p.get('status') or p.get('error', '')[:60] for p in out['pages']]}")


def record_unmapped(pw):
    b2 = pw.chromium.launch(args=["--disable-http2"])          # VOX rejects the default connection
    try:
        deep_record(b2, "vox", [("goto", "https://bhr.voxcinemas.com/"),
                                ("goto", "https://bhr.voxcinemas.com/showtimes"),
                                ("goto", "https://bhr.voxcinemas.com/movies/whats-on")])
    except Exception as e:
        print("vox record failed", e)
    b2.close()
    b = pw.chromium.launch()
    try:
        deep_record(b, "reel", [("goto", "https://www.reelcinemas.com/en-bh/showtime/"),
                                ("click_css", "main button:nth-of-type(2)"),
                                ("goto", "https://www.reelcinemas.com/en-bh/movie-details/HO00005648/the-hunger-games-sunrise-on-the-reaping/")])
    except Exception as e:
        print("reel record failed", e)
    try:
        deep_record(b, "cinepolis", [("goto", "https://www.cinepolisgulf.com/"),
                                     ("click_text", r"^\s*Bahrain|البحرين"),
                                     ("goto", "https://www.cinepolisgulf.com/bh"),
                                     ("goto", "https://www.cinepolisgulf.com/en/bh")])
    except Exception as e:
        print("cinepolis record failed", e)
    b.close()


# ----------------------------------------------------------------- elCinema stand-in (only for chains not yet direct)

EL = "https://elcinema.com"
EL_LISTS = [f"{EL}/en/theater/6/"] + [f"{EL}/en/theater/6/?page={n}" for n in (2, 3)]
EL_THEATER = re.compile(r"^/en/theater/(\d{6,}|\d+/\d+/\d+)/?$")
EL_WORK = re.compile(r"^/en/work/(\d+)/?$")
EL_META = re.compile(r"/en/index/work/(language|country|genre)/")
EL_DATE = re.compile(r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+(\d{1,2})\s+([A-Za-z]+)$")
CHAIN_OF = [("vox", "VOX"), ("reel", "Reel"), ("cinepolis", "Cinépolis"), ("cinépolis", "Cinépolis"),
            ("epix", "Epix"), ("liwan", "Epix"), ("cineco", "Cineco"), ("mukta", "Mukta A2")]


def chain_of(name):
    n = name.lower()
    return next((c for k, c in CHAIN_OF if k in n), None)


def el_parse(html):
    soup = BeautifulSoup(html, "html.parser")
    out, meta = [], {}
    movie, date, exp = None, None, "Standard"
    for el in soup.body.descendants if soup.body else []:
        if isinstance(el, Tag):
            if el.name == "a":
                href = (el.get("href") or "").replace(EL, "")
                m = EL_WORK.match(href.split("?")[0])
                if m:
                    meta.setdefault(m.group(1), {"lang": [], "country": [], "genres": [], "poster": None})
                    img = el.find("img")
                    if img:
                        src = img.get("data-src") or img.get("src") or ""
                        if src.startswith("http"):
                            meta[m.group(1)]["poster"] = src
                    t = el.get_text(" ", strip=True)
                    if t:
                        movie = (m.group(1), t)
                        meta.setdefault(movie[0], {"lang": [], "country": [], "genres": [], "poster": None})
                        exp = "Standard"
                    continue
                mm = EL_META.search(href)
                if mm and movie:
                    k = {"language": "lang", "country": "country", "genre": "genres"}[mm.group(1)]
                    meta[movie[0]][k].append(el.get_text(" ", strip=True))
            elif el.name == "h6" and "section-title" in (el.get("class") or []):
                exp = el.get_text(" ", strip=True) or exp
            continue
        if not isinstance(el, NavigableString) or isinstance(el, Comment):
            continue
        p = el.parent
        if p and p.name in ("script", "style", "noscript", "option", "select"):
            continue
        if p and p.name == "li" and "exp-color" in (p.get("class") or []):
            continue
        text = " ".join(el.split())
        if not text:
            continue
        dm = EL_DATE.match(text)
        if dm:
            date = parse_date(f"{dm.group(2)} {dm.group(3)}")
            continue
        if movie and date:
            for h, mi, ap in re.findall(r"\b(\d{1,2}):(\d{2})\s*([ap]m)\b", text, re.I):
                out.append((movie, date, fmt_time(h, mi, ap), exp))
    return out, meta


def scrape_elcinema(browser, col, chains):
    ctx = browser.new_context(user_agent=MOBILE_UA, locale="en-US", viewport={"width": 430, "height": 932})
    page = ctx.new_page()
    try:
        theaters = {}
        for u in EL_LISTS:
            try:
                page.goto(u, wait_until="domcontentloaded", timeout=45000)
            except Exception:
                continue
            for a in BeautifulSoup(page.content(), "html.parser").find_all("a", href=True):
                href = a["href"].replace(EL, "").split("?")[0]
                name = a.get_text(" ", strip=True)
                if EL_THEATER.match(href) and name and name.lower() != "favourite" and chain_of(name) in chains:
                    theaters[EL + href] = name
        for url, name in theaters.items():
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(1500)
            except Exception:
                continue
            h1 = page.query_selector("h1")
            cname = (h1.inner_text().strip() if h1 else name).replace(" Cinema", "")
            chain = chain_of(cname) or chain_of(name)
            pages = [page.content()]
            opts = page.eval_on_selector_all("#theater-showtimes-date-selector option", "os=>os.map(o=>o.value)") \
                if page.query_selector("#theater-showtimes-date-selector") else []
            for v in opts[1:DAYS_AHEAD]:
                try:
                    page.select_option("#theater-showtimes-date-selector", value=v, timeout=8000)
                    page.wait_for_timeout(2500)
                    pages.append(page.content())
                except Exception:
                    break
            for html in pages:
                rows, meta = el_parse(html)
                for (mid, title), d, t, exp in rows:
                    md = meta.get(mid, {})
                    langs = list(md.get("lang", []))
                    if "India" in md.get("country", []) and not any(x.lower() in INDIAN_LANGS for x in langs):
                        langs.append("Hindi")
                    col.add(chain, cname, title, d, t, exp, poster=md.get("poster"), langs=langs,
                            genres=md.get("genres", []), src="elcinema")
    except Exception as e:
        print("elCinema stand-in failed:", e)
    ctx.close()


# ----------------------------------------------------------------- main

def main():
    col = Collector()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        api = browser.new_context(user_agent=DESKTOP_UA, locale="en-US")
        scrape_mukta(api, col)
        scrape_cineco(api, col)
        api.close()
        scrape_epix(browser, col)

        # Chains with no working direct scraper yet get elCinema, flagged as a fallback.
        need = [c for c in ("VOX", "Reel", "Cinépolis", "Cineco", "Epix", "Mukta A2")
                if not col.count(c, "direct")]
        if need:
            print("elCinema stand-in for:", need)
            scrape_elcinema(browser, col, set(need))
        for c in need:
            notes = {"VOX": "Direct VOX scraper not built yet",
                     "Reel": "Direct Reel scraper not built yet",
                     "Cinépolis": "Not built yet, and elCinema has no Cinépolis times"}
            prev = col.health.get(c, {}).get("note", "")
            report(col, c, False, notes.get(c, prev or "Direct scraper failed"))
        browser.close()
        try:
            record_unmapped(pw)
        except Exception as e:
            print("recording failed (ignored):", e)

    # Indian films: an Indian language from any cinema, or shown only at Mukta A2.
    films_out = []
    for key, f in col.films.items():
        rows = [s for s in col.shows if s["film"] == key]
        if not rows:
            continue
        langs = sorted(f["lang"])
        indian = any(l.lower() in INDIAN_LANGS for l in langs) or f["chains"] == {"Mukta A2"}
        films_out.append({
            "id": key, "title": f["title"], "poster": f["poster"], "lang": langs,
            "genres": sorted(f["genres"])[:4], "indian": indian,
            "shows": [{k: s[k] for k in ("chain", "cinema", "date", "time", "exp", "book", "price", "src")}
                      for s in rows],
        })

    total = sum(len(f["shows"]) for f in films_out)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if total == 0:
        # Keep the last good showtimes, but publish the failure so the app shows red flags.
        print("No showtimes at all. Keeping the old showtimes and flagging every chain.")
        try:
            old = json.load(open("data.json", encoding="utf-8"))
        except Exception:
            old = {"movies": [], "cinemas": [], "updated": now}
        old["health"] = col.health
        old["checked"] = now
        old["stale"] = True
        with open("data.json", "w", encoding="utf-8") as fh:
            json.dump(old, fh, ensure_ascii=False, indent=1)
        return

    films_out.sort(key=lambda f: (-len({s["cinema"] for s in f["shows"]}), f["title"]))
    with open("data.json", "w", encoding="utf-8") as fh:
        json.dump({
            "updated": now,
            "checked": now,
            "cinemas": sorted({s["cinema"] for f in films_out for s in f["shows"]}),
            "health": col.health,
            "movies": films_out,
        }, fh, ensure_ascii=False, indent=1)
    print(f"Saved {len(films_out)} films ({sum(f['indian'] for f in films_out)} Indian), {total} showtimes.")
    for c, h in col.health.items():
        print(f"  {c}: {h['status']} {h['shows']} shows, {h['days']} days. {h['note']}")


if __name__ == "__main__":
    main()
