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
    if t.isupper() and len(t) > 4:                 # "THREE WISHES FOR CINDRELLA" -> "Three Wishes For Cindrella"
        t = t.title().replace("’S", "’s").replace("'S", "'s")
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


def epix_exp(s):
    """Epix hall type: Standard, XL (large screen), VIP (recliners), KINDER (kids); plus 3D if so."""
    exp = (s.get("screen_type_name") or s.get("movie_experience") or "Standard").strip() or "Standard"
    if re.search(r"3d", s.get("mf_name") or "", re.I) and "3d" not in exp.lower():
        exp += " 3D"
    return exp


def scrape_epix(browser, col):
    """Epix's own booking API: movie-shows/<film>?fromDate=<day> lists every show of that film."""
    samples = {}
    ctx = browser.new_context(user_agent=DESKTOP_UA, locale="en-US")
    try:
        ns = ctx.request.get(f"{EPIX_API}/now-showing-movies", timeout=30000).json()
        movies = (ns.get("Records") or {}).get("data") or []
        samples["movies"] = [{k: m.get(k) for k in ("movie_id", "content_lang_id", "movie_title", "lang_name",
                                                     "available_dates")} for m in movies]
        seen_movies, seen_shows = set(), set()
        for m in movies:
            mid, lid = m.get("movie_id"), m.get("content_lang_id") or 1
            if mid in seen_movies:
                continue
            seen_movies.add(mid)
            alt = (m.get("altMovieContent") or [{}])[0]
            poster = alt.get("artwork")
            genres = [g for g in (m.get("genre_name") or "").split(",") if g]
            book = f"{EPIX_WEB}/now-showing-booking/{mid}/{lid}/"
            # Only ask for the days Epix says this film plays, within our 7-day window.
            avail = [x.strip() for x in (m.get("available_dates") or "").split(",") if x.strip()]
            days = [d for d in avail if TODAY.isoformat() <= d <= LAST.isoformat()] if avail else \
                [(TODAY + datetime.timedelta(days=i)).isoformat() for i in range(DAYS_AHEAD)]
            for day in days:
                data = None
                for attempt in range(4):                 # Epix throttles fast callers: back off and retry
                    try:
                        r = ctx.request.get(f"{EPIX_API}/movie-shows/{mid}?fromDate={day}", timeout=30000)
                        body = r.text()
                        j = json.loads(body) if body.strip().startswith("{") else None
                        if r.status == 200 and j and isinstance(j.get("Records"), dict):
                            data = j
                            break
                        samples.setdefault("bad", []).append(f"{mid} {day} try{attempt}: HTTP {r.status} {body[:160]}")
                    except Exception as e:
                        samples.setdefault("bad", []).append(f"{mid} {day} try{attempt}: {e}"[:220])
                    time.sleep(3 * (attempt + 1))
                time.sleep(0.6)
                if data is None:
                    samples.setdefault("gave_up", []).append(f"{mid} {day}")
                    continue
                rows = (data.get("Records") or {}).get("data") or []
                if "sample" not in samples and rows:
                    samples["sample"] = rows[:3]
                new = 0
                for s in rows:
                    if not isinstance(s, dict) or not s.get("ss_start_date") or not s.get("ss_start_show_time"):
                        continue
                    sid = s.get("ss_id") or (s.get("cine_name"), s["ss_start_date"], s["ss_start_show_time"], s.get("screen_id"))
                    if sid in seen_shows or s.get("ss_is_active", "Y") != "Y":
                        continue
                    seen_shows.add(sid)
                    new += 1
                    try:
                        d = datetime.date.fromisoformat(s["ss_start_date"][:10])
                    except ValueError:
                        continue
                    hh, mm = s["ss_start_show_time"].split(":")[:2]
                    title = s.get("movie_title") or m.get("movie_title") or ""
                    col.add("Epix", epix_cinema_label(s.get("cine_name")), title, d, fmt_time(hh, mm),
                            epix_exp(s), book=book, poster=poster,
                            langs=[s.get("lang_name") or m.get("lang_name") or ""], genres=genres)
        missed = len(samples.get("gave_up", []))
        report(col, "Epix", missed == 0 or bool(col.count("Epix")),
               f"{missed} film-days could not be loaded" if missed else "")
    except Exception as e:
        samples["error"] = traceback.format_exc()
        report(col, "Epix", False, f"Error: {str(e)[:120]}")
    ctx.close()
    save_debug("epix.json", samples)


# ----------------------------------------------------------------- Reel (Marassi Galleria)

REEL = "https://www.reelcinemas.com/en-bh/showtime/"
REEL_TIME = re.compile(r"^(\d{1,2}):(\d{2})\s*([ap]m)$", re.I)
REEL_MINS = re.compile(r"^\d+\s*mins?$", re.I)


def parse_reel_text(text, day, col):
    """Reel's page reads: title / '160 mins' / ... / 'Marassi Bahrain' / hall / times / hall + perks."""
    lines = [l.replace("⁦", "").replace("⁩", "").strip() for l in text.splitlines()]
    lines = [l for l in lines if l]
    title, hall, cinema, n = None, "Standard", "Reel Marassi Galleria", 0
    for i in range(len(lines)):
        l = lines[i]
        if i + 1 < len(lines) and REEL_MINS.match(lines[i + 1]):
            title, hall = l, "Standard"
            continue
        if l.lower().startswith("view more") or l.startswith("Book Cinema Tickets"):
            break
        m = REEL_TIME.match(l)
        if m and title:
            col.add("Reel", cinema, title, day, fmt_time(*m.groups()), hall,
                    book=f"{REEL}?date={day.isoformat()}")
            n += 1
            continue
        if l.lower().startswith("after midnight"):
            continue
        # A hall name is the line just before its times (skipping the "After midnight," marker).
        j = i + 1
        while j < len(lines) and lines[j].lower().startswith("after midnight"):
            j += 1
        if title and j < len(lines) and REEL_TIME.match(lines[j]) and len(l) < 40:
            hall = re.sub(r"^Reel\s+", "", l) or l
    return n


def scrape_reel(browser, col):
    samples = {}
    ctx = browser.new_context(user_agent=DESKTOP_UA, locale="en-US", viewport={"width": 1366, "height": 900})
    page = ctx.new_page()
    try:
        for i in range(DAYS_AHEAD):
            day = TODAY + datetime.timedelta(days=i)
            page.goto(f"{REEL}?date={day.isoformat()}", wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(1500)
            for _ in range(15):                                   # reveal every film
                btn = page.get_by_role("button", name=re.compile(r"view more", re.I))
                if not btn.count():
                    break
                try:
                    btn.first.click(timeout=5000)
                    page.wait_for_timeout(1500)
                except Exception:
                    break
            text = page.inner_text("body")
            n = parse_reel_text(text, day, col)
            samples[day.isoformat()] = n
            if i == 0:
                samples["text_today"] = text[:30000]
        report(col, "Reel", True, "" if col.count("Reel") else "Showtimes page had no readable times")
    except Exception as e:
        samples["error"] = traceback.format_exc()
        report(col, "Reel", False, f"Error: {str(e)[:120]}")
    ctx.close()
    save_debug("reel_scrape.json", samples)


# ----------------------------------------------------------------- VOX, Cinépolis, q-tickets: deep recording

def deep_record(browser, name, steps, http1=False):
    """Visits pages, clicks a few things, and saves every data call and the visible text."""
    out = {"pages": []}
    ctx = browser.new_context(user_agent=DESKTOP_UA, locale="en-US", viewport={"width": 1366, "height": 900})
    page = ctx.new_page()
    calls, failed = [], []
    page.on("response", lambda r: calls.append(r))
    page.on("requestfailed", lambda r: failed.append([r.url[:200], str(r.failure)[:120]]))
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
    out["failed"] = [f for f in failed if not noise.search(f[0])][:80]
    save_debug(f"{name}.json", out)
    ctx.close()
    print(f"  recorded {name}: {[p.get('status') or p.get('error', '')[:60] for p in out['pages']]}")


def reachability(ctx):
    """Plain requests to the sites that seem to block us, with timings, so we know exactly why."""
    tests = [
        ("vox_home", "https://bhr.voxcinemas.com/"),
        ("vox_whats_on", "https://bhr.voxcinemas.com/movies/whats-on"),
        ("cinepolis_country", "https://api.cinepolisgulf.com/api/generic/country-specific-data?country_id=4"),
        ("cinepolis_banners", "https://api.cinepolisgulf.com/api/movies/banners/?country_id=4"),
        ("cinepolis_site", "https://bahrain.cinepolisgulf.com/"),
        ("control_github", "https://github.com/"),
    ]
    out = {}
    for name, url in tests:
        t0 = time.time()
        try:
            hdrs = {"Origin": "https://bahrain.cinepolisgulf.com",
                    "Referer": "https://bahrain.cinepolisgulf.com/"} if "cinepolis" in url else {}
            r = ctx.request.get(url, timeout=25000, headers=hdrs)
            body = r.text()
            out[name] = {"status": r.status, "secs": round(time.time() - t0, 1), "bytes": len(body),
                         "body": body[:3000]}
        except Exception as e:
            out[name] = {"error": str(e).splitlines()[0][:200], "secs": round(time.time() - t0, 1)}
        print(f"  reach {name}: {out[name].get('status', out[name].get('error'))} in {out[name]['secs']}s")
    try:
        ip = ctx.request.get("https://www.cloudflare.com/cdn-cgi/trace", timeout=15000).text()
        out["runner_location"] = [l for l in ip.splitlines() if l.startswith(("loc=", "colo="))]
    except Exception:
        pass
    save_debug("reachability.json", out)


def record_unmapped(pw, qtickets_url=None):
    b = pw.chromium.launch()
    # Cinépolis runs a separate Bahrain site that reads from api.cinepolisgulf.com (country 4).
    try:
        deep_record(b, "cinepolis", [("goto", "https://bahrain.cinepolisgulf.com/"),
                                     ("goto", "https://bahrain.cinepolisgulf.com/now-showing"),
                                     ("click_css", "a[href*='movie'], a[href*='film']"),
                                     ("click_text", r"^\s*(\d{1,2}:\d{2}|book)")])
    except Exception as e:
        print("cinepolis record failed", e)
    # Cineco books through q-tickets, which may list more days than Cineco's own site.
    if qtickets_url:
        try:
            deep_record(b, "qtickets", [("goto", qtickets_url)])
        except Exception as e:
            print("qtickets record failed", e)
    b.close()
    # VOX timed out from GitHub last time; try once more with a phone browser and a light load.
    b2 = pw.chromium.launch(args=["--disable-http2"])
    try:
        deep_record(b2, "vox", [("goto", "https://bhr.voxcinemas.com/movies/whats-on")])
    except Exception as e:
        print("vox record failed", e)
    b2.close()


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


EL_SEEDS = {   # known elCinema pages, so the stand-in works even if its cinema list changes
    "VOX": {"https://elcinema.com/en/theater/3101299/": "Vox Bahrain City Centre",
            "https://elcinema.com/en/theater/3101298/": "Vox The Avenues"},
    "Reel": {"https://elcinema.com/en/theater/3101574/": "Reel Marassi Galleria"},
    "Epix": {"https://elcinema.com/en/theater/3101599/": "Epix Dana Mall",
             "https://elcinema.com/en/theater/3101621/": "Epix Al Liwan"},
    "Mukta A2": {"https://elcinema.com/en/theater/3101515/": "Mukta A2"},
    "Cineco": {"https://elcinema.com/en/theater/3101521/": "Cineco Seef",
               "https://elcinema.com/en/theater/3101522/": "Cineco Juffair",
               "https://elcinema.com/en/theater/3101524/": "Cineco Al Hamra"},
}


def scrape_elcinema(browser, col, chains):
    log = {"chains": sorted(chains), "lists": [], "theaters": {}, "errors": []}
    ctx = browser.new_context(user_agent=MOBILE_UA, locale="en-US", viewport={"width": 430, "height": 932})
    page = ctx.new_page()
    try:
        theaters = {}
        for c in chains:
            theaters.update(EL_SEEDS.get(c, {}))
        for u in EL_LISTS:
            try:
                resp = page.goto(u, wait_until="domcontentloaded", timeout=45000)
                log["lists"].append([u, resp.status if resp else None])
            except Exception as e:
                log["errors"].append(f"{u}: {e}"[:300])
                continue
            for a in BeautifulSoup(page.content(), "html.parser").find_all("a", href=True):
                href = a["href"].replace(EL, "").split("?")[0]
                name = a.get_text(" ", strip=True)
                if EL_THEATER.match(href) and name and name.lower() != "favourite" and chain_of(name) in chains:
                    url = EL + href.rstrip("/") + "/"
                    theaters.setdefault(url, name)
        log["found"] = theaters
        for url, name in theaters.items():
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(1500)
            except Exception as e:
                log["errors"].append(f"{url}: {e}"[:300])
                continue
            before = len(col.shows)
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
            log["theaters"][cname] = {"chain": chain, "pages": len(pages), "shows": len(col.shows) - before}
    except Exception:
        log["errors"].append(traceback.format_exc()[-1500:])
    ctx.close()
    save_debug("elcinema.json", log)


# ----------------------------------------------------------------- main

def main():
    col = Collector()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        api = browser.new_context(user_agent=DESKTOP_UA, locale="en-US")
        scrape_mukta(api, col)
        scrape_cineco(api, col)
        try:
            reachability(api)
        except Exception as e:
            print("reachability test failed (ignored):", e)
        api.close()
        scrape_epix(browser, col)
        scrape_reel(browser, col)

        # Chains with no working direct scraper get elCinema, flagged as a fallback.
        need = [c for c in ("VOX", "Reel", "Cinépolis", "Cineco", "Epix", "Mukta A2")
                if not col.count(c, "direct")]
        if need:
            print("elCinema stand-in for:", need)
            scrape_elcinema(browser, col, set(need))
        for c in need:
            notes = {"VOX": "VOX's website blocks GitHub's servers",
                     "Cinépolis": "Cinépolis's Bahrain site shows no films to GitHub's servers"}
            prev = col.health.get(c, {}).get("note", "")
            report(col, c, False, notes.get(c, prev or "Direct scraper failed"))
        browser.close()
        qt = next((s["book"] for s in col.shows if s["chain"] == "Cineco" and "q-tickets" in (s["book"] or "")), None)
        try:
            record_unmapped(pw, qt)
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
