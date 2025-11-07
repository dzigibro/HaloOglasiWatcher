#!/usr/bin/env python3
# HaloOglasiWatcher — v4.0 (async httpx + bounded concurrency + async Playwright fallback)
# - Async httpx client with HTTP/2, retry/backoff, env isolation (no proxies), cookies
# - Requests-first pagination via page=?N; DOM "next" fallback; Playwright last-resort (HAR+screenshot)
# - PRICE: multi-source candidates → de-dup → drop tiny (<1000) placeholders → pick by priority; dump per-ad debug JSON
# - LOCATION: prefer mikrolokacija→lokacija→structured/breadcrumb/address; expose "opstina" token; Cyrillic + diacritics tolerant
# - Date parsing unchanged
# - Desperation scoring includes URL; +1 if "hitno" in title/url
# - SQLite upsert keyed by ad_id; URL fallback
# - seen.yml dedupe keyed by ad_id (fallback URL); cooldown or price-change only
# - Email with SMTP debug
# - END-OF-RUN: counters by reason and by opština; full filter line per ad in logs

import asyncio, contextlib, json, logging, re, smtplib, socket, sqlite3, ssl, sys, time, yaml
from collections import Counter
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse, parse_qs

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from zoneinfo import ZoneInfo

import filters as F

# ---------- paths ----------
ROOT = Path(__file__).parent
CFG  = ROOT / "config.yaml"
DBP  = ROOT / "halowatch.db"
OUT  = ROOT / "out"; OUT.mkdir(parents=True, exist_ok=True)
SEEN = ROOT / "seen.yml"
print(f"[DEBUG] OUT dir => {OUT.resolve()}")

# ---------- logging ----------
LOG = logging.getLogger("halowatch")
h = logging.StreamHandler(sys.stdout)
h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%H:%M:%S"))
LOG.addHandler(h); LOG.setLevel(logging.DEBUG)

# ---------- constants ----------
UA = {"User-Agent": "Mozilla/5.0 (HaloWatch/4.0)"}  # keep site-friendly
RS_TZ = ZoneInfo("Europe/Belgrade")

PRICE_RE = re.compile(r"(\d{1,3}(?:[.\s]\d{3})*(?:[.,]\d+)?|\d+)\s*(?:€|eur|eura|evra)?", re.I)
OBJAVLJEN_RE = re.compile(r"Objavljen\s*:\s*(\d{2}\.\d{2}\.\d{4}\.)\s*u\s*(\d{2}:\d{2})", re.I)

BAD_PAGE_SIGS = [
    "are you human","enable javascript","cloudflare","captcha","403 forbidden",
    "verify you are a human","challenge","press and hold"
]

LIST_SEL = [
    "h3 a[href^='/nekretnine/prodaja-stanova/']",
    "a[href^='/nekretnine/prodaja-stanova/'][title]",
    "article a[href^='/nekretnine/prodaja-stanova/']",
    "[data-qa*='ad-title'] a[href*='/nekretnine/prodaja-stanova/']",
    ".ad a[href*='/nekretnine/prodaja-stanova/']",
    ".listing a[href*='/nekretnine/prodaja-stanova/']",
    "#results a[href*='/nekretnine/prodaja-stanova/']",
]

AMENITY_KEYWORDS = {
    "vrtići","vrtici","osnovne škole","srednje škole","fakulteti",
    "domovi zdravlja","pijace","marketi","prodavnice","prevoz","trgovine"
}

# Concurrency knobs (tweak cautiously)
CONCURRENCY = 8
PAGE_DELAY_S = 0.6  # default throttle between pages (overridden by config)

# ---------- small sr normalization helpers (Cyrillic→Latin + strip diacritics) ----------
_CYR2LAT = str.maketrans({
    # Serbian Cyrillic core
    "А":"A","Б":"B","В":"V","Г":"G","Д":"D","Ђ":"Đ","Е":"E","Ж":"Ž","З":"Z","И":"I","Ј":"J","К":"K",
    "Л":"L","Љ":"Lj","М":"M","Н":"N","Њ":"Nj","О":"O","П":"P","Р":"R","С":"S","Т":"T","Ћ":"Ć","У":"U",
    "Ф":"F","Х":"H","Ц":"C","Ч":"Č","Џ":"Dž","Ш":"Š",
    "а":"a","б":"b","в":"v","г":"g","д":"d","ђ":"đ","е":"e","ж":"ž","з":"z","и":"i","ј":"j","к":"k",
    "л":"l","љ":"lj","м":"m","н":"n","њ":"nj","о":"o","п":"p","р":"r","с":"s","т":"t","ћ":"ć","у":"u",
    "ф":"f","х":"h","ц":"c","ч":"č","џ":"dž","ш":"š",
})
_DIACRITICS = str.maketrans({"š":"s","đ":"dj","č":"c","ć":"c","ž":"z",
                              "Š":"s","Đ":"dj","Č":"c","Ć":"c","Ž":"z"})

def sr_norm(s: str) -> str:
    s = (s or "").strip()
    if not s: return ""
    s = s.translate(_CYR2LAT)     # Cyrillic → Latin
    s = s.translate(_DIACRITICS)  # strip diacritics
    s = re.sub(r"\s+", " ", s).lower()
    return s

# ---------- utils ----------
def now_utc(): return datetime.now(timezone.utc)
def iso(dt):   return dt.isoformat() if dt else None
def read_cfg(): return yaml.safe_load(CFG.read_text())

def dump_yaml(path: Path, data, label: str):
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    LOG.debug(f"Wrote {label}: {path} ({len(data) if hasattr(data,'__len__') else 'n/a'} items)")

def _norm_txt(x): 
    return re.sub(r"\s+", " ", (x or "").strip())

def guess_loc_from_url(url: str) -> str:
    try:    path = url.split("halooglasi.com", 1)[1]
    except: return ""
    return path.replace("/", " ").replace("-", " ")




HEART = "❤️"



def heartify_location(loc: str) -> str:
    """Wrap target toponyms with hearts. Tolerant to ć/c, case, and Cyrillic via sr_norm fallback."""
    if not loc:
        return loc or ""

    original = loc

    # Direct replacements on Latin (keeps original casing in the match)
    loc = re.sub(r"(?i)\b(mije\s+kova[cć]evi[cć]a)\b", HEART + r" \1 " + HEART, loc)
    loc = re.sub(r"(?i)\b(bogoslovija)\b",               HEART + r" \1 " + HEART, loc)

    # If nothing changed but normalized text contains targets (e.g., Cyrillic), append attention tags
    if loc == original:
        n = sr_norm(original)
        if "mije kovacevica" in n:
            loc = f"{loc} {HEART} Mije Kovačevića {HEART}"
        if "bogoslovija" in n:
            loc = f"{loc} {HEART} Bogoslovija {HEART}"

    return loc


# ---------- DB ----------
SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
  ad_id TEXT UNIQUE,
  url TEXT,
  title TEXT,
  location TEXT,
  price_eur REAL,
  description TEXT,
  desperation_score INTEGER,
  desperation_hits TEXT,
  first_seen TEXT,
  last_seen  TEXT,
  posted_date TEXT,
  posted_local TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_listings_adid ON listings(ad_id);
CREATE INDEX IF NOT EXISTS idx_listings_url ON listings(url);
"""

def db():
    con = sqlite3.connect(DBP)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    for stmt in [s for s in SCHEMA.split(";") if s.strip()]:
        con.execute(stmt)
    return con

def upsert_listing(con, li: dict):
    fs = None
    if li.get("ad_id"):
        fs = con.execute("SELECT first_seen FROM listings WHERE ad_id=?", (li["ad_id"],)).fetchone()
    if not fs:
        fs = con.execute("SELECT first_seen FROM listings WHERE url=?", (li["url"],)).fetchone()
    first_seen = (fs[0] if fs and fs[0] else iso(now_utc()))
    con.execute(
        """
        INSERT INTO listings(ad_id,url,title,location,price_eur,description,
                             desperation_score,desperation_hits,first_seen,last_seen,posted_date,posted_local)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(ad_id) DO UPDATE SET
          url=excluded.url,
          title=excluded.title,
          location=excluded.location,
          price_eur=excluded.price_eur,
          description=excluded.description,
          desperation_score=excluded.desperation_score,
          desperation_hits=excluded.desperation_hits,
          last_seen=excluded.last_seen,
          posted_date=COALESCE(excluded.posted_date, listings.posted_date),
          posted_local=COALESCE(excluded.posted_local, listings.posted_local)
        """,
        (
            li.get("ad_id"), li["url"], li.get("title",""), li.get("location",""), li.get("price_eur"),
            li.get("description",""), li.get("desperation_score",0),
            json.dumps(li.get("desperation_hits",[])), first_seen, iso(now_utc()),
            li.get("posted_date"), li.get("posted_local")
        )
    )
    con.commit()
    return first_seen

# ---------- HTTP (async + httpx) ----------
class HttpError(Exception): pass

def _is_retryable(exc: Exception) -> bool:
    return isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.ConnectTimeout))

def looks_protected(html: str) -> bool:
    low = html.lower()
    return any(sig in low for sig in BAD_PAGE_SIGS)

def _ua_headers():
    return {
        "User-Agent": UA["User-Agent"],
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "sr-RS,sr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.halooglasi.com/",
        "Cache-Control": "no-cache",
        #"Accept-Encoding": "identity",
    }

def new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers=_ua_headers(),
        http2=True,
        timeout=httpx.Timeout(30.0, connect=20.0),
        follow_redirects=True,
        trust_env=False,  # ignore system proxy env
        limits=httpx.Limits(max_connections=max(8, CONCURRENCY*2), max_keepalive_connections=max(8, CONCURRENCY*2)),
        cookies={"hlg_loc": "rs"},
    )

async def http_get(client: httpx.AsyncClient, url: str, *, attempts: int = 4, base_delay: float = 0.6) -> str:
    LOG.info(f"GET {url}")
    last_exc = None
    for i in range(1, attempts + 1):
        try:
            r = await client.get(url)
            LOG.info(f"{r.status_code} bytes(text)~={len(r.text)}")
            r.raise_for_status()
            text = r.text or r.content.decode(errors="ignore")
            if looks_protected(text) and i < attempts:
                LOG.warning("looks_protected() hit; retrying with backoff")
                await asyncio.sleep(base_delay * i)
                continue
            return text
        except Exception as e:
            last_exc = e
            if _is_retryable(e) and i < attempts:
                await asyncio.sleep(base_delay * i)
                continue
            break
    raise HttpError(f"GET failed after {attempts} attempts: {url} | {last_exc}")
# ---------- link extraction + pagination (fast) ----------

# Precompile fallback regex (faster than recompiling per call)
_LINK_FALLBACK_RE = re.compile(r'href="(/nekretnine/prodaja-stanova/[^"]+#?[^?]*)')

# Join selectors once so Soup does a single query pass
_LIST_SEL_JOINED = ", ".join(LIST_SEL)

def extract_links_from_html(html: str) -> set[str]:
    # lxml is 2–4x faster than html.parser
    soup = BeautifulSoup(html, "lxml")
    links = set()

    # Single select call (joined selectors) instead of looped selects
    for a in soup.select(_LIST_SEL_JOINED):
        href = a.get("href")
        if href and "/nekretnine/prodaja-stanova/" in href:
            links.add("https://www.halooglasi.com" + href.split("?")[0])

    # Regex fallback only if DOM failed
    if not links:
        for m in _LINK_FALLBACK_RE.finditer(html):
            links.add("https://www.halooglasi.com" + m.group(1).split("?")[0])

    LOG.debug(f"extract_links -> {len(links)}")
    return links


# --- windowed paginator (batch pages concurrently) ---
async def fetch_all_links_paged_windowed(
    client: httpx.AsyncClient,
    start_url: str,
    *,
    max_pages: int,
    sleep_s: float,
    window: int = 5
) -> set[str]:
    all_links, seen_any = set(), False
    n = 1

    async def _one(idx: int):
        url = with_page(start_url, idx)
        LOG.info(f"[PAGE {idx}] {url}")
        try:
            html = await http_get(client, url)
            if LOG.level <= logging.DEBUG:
                (OUT / f"page_{idx:03d}.html").write_text(html, encoding="utf-8")
            links = extract_links_from_html(html)
            LOG.info(f"[PAGE {idx}] links={len(links)}")
            return idx, links
        except Exception as e:
            LOG.warning(f"page fetch failed {url}: {e}")
            return idx, set()

    while n <= max_pages:
        batch = list(range(n, min(n + window - 1, max_pages) + 1))
        results = await asyncio.gather(*[_one(i) for i in batch])
        results.sort(key=lambda x: x[0])

        empty_in_window = all(len(links) == 0 for _, links in results)

        for _, links in results:
            if links:
                seen_any = True
                all_links |= links

        if not seen_any and empty_in_window:
            LOG.info("[FALLBACK] Chromium first page…")
            all_links |= await fetch_links_browser(start_url)
            break

        # if last page in window has 0 links, we likely hit the end
        if not results[-1][1]:
            break

        n += window
        await asyncio.sleep(sleep_s)

    return all_links



def extract_next_page(html: str, base_url: str) -> str | None:
    # Also use lxml here (you used html.parser before)
    soup = BeautifulSoup(html, "lxml")

    # 1) <link rel="next"> or <a rel="next">
    a = soup.select_one("link[rel='next'], a[rel='next']")
    if a and a.get('href'):
        return urljoin(base_url, a.get('href'))

    # 2) Common "Next" controls
    nxt = soup.select_one("a[aria-label='Next'], a.pagination__next, .pagination a.next, a.page-link[rel='next']")
    if nxt and nxt.get('href'):
        return urljoin(base_url, nxt.get('href'))

    # 3) Infer next from "active + li a"
    cur = soup.select_one(".pagination .active + li a, .pager .active + li a")
    if cur and cur.get('href'):
        return urljoin(base_url, cur.get('href'))

    return None

def with_page(url: str, n: int) -> str:
    u = urlparse(url)
    qs = parse_qs(u.query, keep_blank_values=True)
    qs["page"] = [str(n)]
    return u._replace(query=urlencode(qs, doseq=True)).geturl()

async def fetch_all_links_paged(client: httpx.AsyncClient, start_url: str, *, max_pages: int, sleep_s: float) -> set[str]:
    """Sequential pagination (kept for compatibility), but only dumps when DEBUG."""
    all_links = set()
    links_seen_any = False
    for n in range(1, max_pages + 1):
        url = with_page(start_url, n)
        LOG.info(f"[PAGE {n}] {url}")
        try:
            html = await http_get(client, url)
        except Exception as e:
            LOG.warning(f"page fetch failed: {e}")
            break

        if LOG.level <= logging.DEBUG:
            (OUT / f"page_{n:03d}.html").write_text(html, encoding="utf-8")

        links = extract_links_from_html(html)
        LOG.info(f"[PAGE {n}] links={len(links)}")

        if not links and n == 1:
            LOG.info("[FALLBACK] page=1 empty → using requests next/rel…(async)")
            return await fetch_all_links_requests(client, start_url, max_pages=max_pages, sleep_s=sleep_s)
        if not links:
            break

        links_seen_any = True
        all_links |= links
        await asyncio.sleep(sleep_s)

    if not links_seen_any:
        LOG.info("[FALLBACK] Chromium first page…")
        all_links |= await fetch_links_browser(start_url)
    return all_links

async def fetch_all_links_requests(client: httpx.AsyncClient, start_url: str, *, max_pages: int, sleep_s: float) -> set[str]:
    """DOM 'next' fallback. Also only dumps when DEBUG."""
    seen_pages = set(); all_links = set(); url = start_url; page_idx = 1
    while url and page_idx <= max_pages and url not in seen_pages:
        seen_pages.add(url)
        LOG.info(f"[PAGE {page_idx}] {url}")
        try:
            html = await http_get(client, url)
        except Exception as e:
            LOG.warning(f"page fetch failed: {e}")
            break

        if LOG.level <= logging.DEBUG:
            (OUT / f"page_{page_idx:03d}.html").write_text(html, encoding="utf-8")

        links = extract_links_from_html(html)
        all_links |= links
        nxt = extract_next_page(html, url)
        LOG.info(f"[PAGE {page_idx}] links={len(links)} next={bool(nxt)}")
        if not nxt:
            break

        url = nxt; page_idx += 1
        await asyncio.sleep(sleep_s)

    if not all_links and page_idx == 1:
        LOG.info("[FALLBACK] Chromium first page…")
        all_links |= await fetch_links_browser(start_url)
    return all_links

# ---------- Playwright (async) ----------
async def fetch_links_browser(url: str, *, headless: bool = True) -> set[str]:
    LOG.info(f"[BROWSER] {url}")
    links = set()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, args=["--disable-blink-features=AutomationControlled"])
        context = await browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0.0.0 Safari/537.36"),
            locale="sr-RS",
            timezone_id="Europe/Belgrade",
            record_har_path=str(OUT / "net.har"),
            record_har_content="embed",
        )
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        page = await context.new_page(); page.set_default_timeout(20000)
        await page.goto(url, wait_until="domcontentloaded")
        with contextlib.suppress(Exception):
            await page.locator("button:has-text('Prihvatam'), button:has-text('Prihvati'), #onetrust-accept-btn-handler").first.click(timeout=1500)
        await page.wait_for_load_state("networkidle")
        html = await page.content()
        (OUT / "last_page_browser.html").write_text(html, encoding="utf-8")
        await page.screenshot(path=str(OUT / "last_page.png"), full_page=True)
        for sel in LIST_SEL:
            for a in await page.query_selector_all(sel):
                href = await a.get_attribute("href")
                if href and "/nekretnine/prodaja-stanova/" in href:
                    links.add("https://www.halooglasi.com" + href.split("?")[0])
        if not links:
            for m in re.finditer(r'href="(/nekretnine/prodaja-stanova/[^"]+#?[^?]*)', html):
                links.add("https://www.halooglasi.com" + m.group(1).split("?")[0])
        await browser.close()
    LOG.info(f"[BROWSER] EXTRACTED {len(links)}")
    if not links and headless:
        LOG.warning("[BROWSER] 0 links headless → retry headed…")
        return await fetch_links_browser(url, headless=False)
    return links

# ---------- structured helpers ----------
def _structured_fields(soup: BeautifulSoup) -> dict:
    out = {}
    for node in soup.select('script[type="application/ld+json"]'):
        try: data = json.loads(node.string or "")
        except Exception: continue
        items = data if isinstance(data, list) else [data]
        for obj in items:
            if not isinstance(obj, dict): continue
            addr = obj.get("address")
            if isinstance(addr, dict) and not out.get("location"):
                parts = [addr.get("streetAddress"), addr.get("addressLocality"), addr.get("addressRegion"), addr.get("postalCode")]
                out["location"] = " ".join([p for p in parts if p])
            if obj.get("name") and not out.get("title"):
                out["title"] = str(obj.get("name")).strip()
            for k in ("datePublished","datePosted","dateCreated"):
                if obj.get(k) and not out.get("posted_date"): out["posted_date"] = obj.get(k)
    ogp = soup.select_one("meta[property='product:price:amount']")
    if ogp:
        out["ogp_price_hint"] = ogp.get("content", "").strip()
    return out

def find_spec_value(soup: BeautifulSoup, keys: list[str]) -> str | None:
    for code in ("lokacija", "mikrolokacija"):
        if any(k.lower() == code for k in keys):
            el = soup.select_one(f"[data-code='{code}']")
            if el:
                txt = el.get_text(" ", strip=True)
                if txt: return txt
    candidates = soup.select("dl, .property__features, .AdInfo, .kvp, table")
    label_patterns = [re.compile(re.escape(k), re.I) for k in keys]
    for block in candidates:
        for dt in block.select("dt, .label, th"):
            label = _norm_txt(dt.get_text(" ", strip=True))
            if any(p.search(label) for p in label_patterns):
                dd = dt.find_next("dd") or dt.find_next("td") or dt.find_next("div")
                if dd:
                    val = _norm_txt(dd.get_text(" ", strip=True))
                    if val: return val
    blob = soup.get_text("\n", strip=True)
    for k in keys:
        m = re.search(rf"{k}\s*[:\-]\s*([^\n]+)", blob, flags=re.I)
        if m:
            return _norm_txt(m.group(1))
    return None

def extract_ad_id(soup: BeautifulSoup, url: str | None = None) -> str | None:
    html = str(soup)
    m = re.search(r"QuidditaEnvironment\.CurrentClassified\s*=\s*({.*?});", html, flags=re.S)
    if m:
        with contextlib.suppress(Exception):
            obj = json.loads(m.group(1))
            for k in ("Id", "ID", "ClassifiedId", "ClassifiedID", "AdId", "AdID"):
                v = obj.get(k)
                if v is None: continue
                s = str(v)
                mm = re.search(r"\b(\d{6,})\b", s)
                if mm: return mm.group(1)
    keys = ["Šifra oglasa na sajtu","Sifra oglasa na sajtu","Šifra oglasa","Sifra oglasa","ID oglasa","ID"]
    val = find_spec_value(soup, keys)
    if val:
        m = re.search(r"\b(\d{6,})\b", val)
        if m: return m.group(1)
    m = re.search(r'data-(?:classified-)?id\s*=\s*"(\d{6,})"', html, flags=re.I)
    if m: return m.group(1)
    m = re.search(r"(?:Šifra oglasa(?: na sajtu)?|ID oglasa)\D{0,30}(\d{6,})", html, flags=re.I)
    if m: return m.group(1)
    if url:
        m = re.search(r"/(\d{10,})(?:[/?#]|$)", url)
        if m: return m.group(1)
    return None

# ---------- LOCATION ----------
def extract_location(soup: BeautifulSoup, url: str) -> str:
    def clean(x: str | None) -> str:
        if not x: return ""
        x = re.sub(r"\s+", " ", x).strip()
        if x.lower() in {"lokacija","mikrolokacija","grad","opština čukarica","opstina čukarica"}:
            return ""
        return x

    html = str(soup)
    m = re.search(r"QuidditaEnvironment\.CurrentClassified\s*=\s*({.*?});", html, flags=re.S)
    if m:
        with contextlib.suppress(Exception):
            obj = json.loads(m.group(1))
            of = obj.get("OtherFields") or {}
            parts = [clean(of.get("grad_s")), clean(of.get("lokacija_s")), clean(of.get("mikrolokacija_s"))]
            parts = [p for p in parts if p]
            if parts:
                seen=set(); dedup=[]
                for p in parts:
                    if p not in seen:
                        seen.add(p); dedup.append(p)
                return " - ".join(dedup)

    subs = [clean(li.get_text(" ", strip=True)) for li in soup.select("ul.subtitle-places li")]
    subs = [s for s in subs if s]
    if subs:
        return " - ".join(subs[:4])

    for code in ("mikrolokacija","lokacija","adresa","address"):
        el = soup.select_one(f"[data-code='{code}']")
        if el:
            v = clean(el.get_text(" ", strip=True))
            if v:
                return v

    el = soup.select_one("[itemprop='address'], [itemscope][itemtype*='PostalAddress']")
    if el:
        txt = clean(el.get_text(" ", strip=True))
        if txt and sum(kw in txt.lower() for kw in AMENITY_KEYWORDS) <= 1:
            return txt

    for sel in ("nav[aria-label='breadcrumb']", ".breadcrumb, .breadcrumbs",
                "[class*='crumb']", "[data-testid*='location']", ".ad-location, .AdInfo__location, .location"):
        el = soup.select_one(sel)
        if not el: continue
        txt = clean(el.get_text(" ", strip=True))
        if not txt or sum(kw in txt.lower() for kw in AMENITY_KEYWORDS) >= 2:
            continue
        parts = re.split(r"\s*[-–|]\s*", txt)
        cand = " - ".join([p for p in parts if p][:5]) or txt
        cand = clean(cand)
        if cand:
            return cand

    return guess_loc_from_url(url)

# ---------- PRICE ----------
def parse_price_eur(text: str | None) -> float | None:
    if not text: return None
    t = (text or "").replace("\xa0", " ").strip()
    m = PRICE_RE.search(t)
    if not m: return None
    raw = m.group(1).replace(" ", "").replace(".", "").replace(",", ".")
    try:
        return float(raw) if raw else None
    except Exception:
        return None

def _rescale_if_needed(v: float) -> float:
    if v > 500000 and v % 10 == 0: v = v / 10
    if v > 5000000 and v % 100 == 0: v = v / 100
    return v

def _reject_tiny(v: float | None) -> bool:
    return v is not None and v < 1000.0

def _price_candidates(soup: BeautifulSoup) -> list[tuple[str, float]]:
    cand: list[tuple[str,float]] = []

    pv = soup.select_one("span.offer-price-value")
    pu = soup.select_one("span.offer-price-unit")
    if pv:
        txt = pv.get_text(" ", strip=True)
        if pu: txt += " " + pu.get_text(" ", strip=True)
        v = parse_price_eur(txt)
        if v is not None: cand.append(("dom:value+unit", v))

    cont = soup.select_one(".offer-price")
    if cont:
        txt = cont.get_text(" ", strip=True)
        v = parse_price_eur(txt)
        if v is not None: cand.append(("dom:offer-price", v))

    meta = soup.select_one("meta[itemprop='price']")
    if meta and meta.get("content"):
        v = parse_price_eur(meta["content"])
        if v is not None: cand.append(("meta:itemprop:price", v))

    og = soup.select_one("meta[property='product:price:amount']")
    if og and og.get("content"):
        v = parse_price_eur(og["content"])
        if v is not None: cand.append(("ogp:product:price:amount", v))

    for node in soup.select('script[type="application/ld+json"]'):
        try: data = json.loads(node.string or "")
        except Exception: continue
        items = data if isinstance(data, list) else [data]
        for obj in items:
            offer = obj.get("offers") or obj.get("offer")
            if isinstance(offer, dict):
                raw = str(offer.get("price") or offer.get("lowPrice") or offer.get("highPrice") or "")
                if raw:
                    v = parse_price_eur(raw)
                    if v is not None:
                        cand.append(("jsonld:offer", _rescale_if_needed(v)))

    html = str(soup)
    m = re.search(r"QuidditaEnvironment\.CurrentClassified\s*=\s*({.*?});", html, flags=re.S)
    if m:
        with contextlib.suppress(Exception):
            obj = json.loads(m.group(1))
            of = obj.get("OtherFields") or {}
            cena = of.get("cena_d")
            if isinstance(cena, (int, float)):
                v = float(cena)
                cand.append(("quiddita:OtherFields.cena_d", v))

    full = soup.get_text(" ", strip=True).replace("\xa0", " ")
    m2 = re.search(r"(\d{1,3}(?:[.\s]\d{3})*(?:[.,]\d+)?|\d+)\s*€(?!\s*(?:/|po)?\s*m2|m²)", full, flags=re.I)
    if m2:
        v = parse_price_eur(m2.group(0))
        if v is not None:
            cand.append(("text:€", v))

    seenv=set(); out=[]
    for src,v in cand:
        v2 = float(v)
        if v2 not in seenv:
            seenv.add(v2); out.append((src,v2))
    return out

def extract_price_eur(soup: BeautifulSoup) -> tuple[float | None, str, list[tuple[str,float]]]:
    cand = _price_candidates(soup)
    non_tiny = [(s,v) for (s,v) in cand if not _reject_tiny(v)]
    pick_from = non_tiny if non_tiny else cand
    PRI = ["dom:value+unit","dom:offer-price","ogp:product:price:amount","jsonld:offer","quiddita:OtherFields.cena_d","meta:itemprop:price","text:€"]
    by_src = {s:v for s,v in pick_from}
    for key in PRI:
        if key in by_src:
            return by_src[key], key, cand
    return None, "none", cand

# ---------- DATE ----------
def extract_posted_date(soup: BeautifulSoup) -> tuple[str|None, str|None]:
    txt = soup.get_text("\n", strip=True)
    m = OBJAVLJEN_RE.search(txt)
    if not m:
        t = soup.select_one("time[datetime]")
        if t and t.get("datetime"):
            with contextlib.suppress(Exception):
                dt = datetime.fromisoformat(t["datetime"].replace("Z","+00:00"))
                return dt.astimezone(timezone.utc).isoformat(), t["datetime"]
        return None, None
    d, hm = m.group(1).rstrip("."), m.group(2)
    with contextlib.suppress(Exception):
        dt_local = datetime.strptime(f"{d} {hm}", "%d.%m.%Y %H:%M").replace(tzinfo=RS_TZ)
        return dt_local.astimezone(timezone.utc).isoformat(), f"{d}. u {hm}"
    return None, f"{d}. u {hm}"

# ---------- LISTING FETCH (async) ----------
_SEM = asyncio.Semaphore(CONCURRENCY)

async def fetch_listing_async(client: httpx.AsyncClient, url: str) -> dict:
    async with _SEM:
        html = await http_get(client, url)
    soup = BeautifulSoup(html, "lxml")

    li = {"url": url}
    li.update(_structured_fields(soup))

    li["ad_id"] = extract_ad_id(soup, url)
    LOG.debug(f"[ID] {url} -> ad_id={li.get('ad_id')}")

    if not li.get("title"):
        t = soup.select_one("h1, h1[itemprop='name']")
        li["title"] = (t.get_text(strip=True) if t else (soup.title.string.strip() if soup.title and soup.title.string else ""))

    if not li.get("location"):
        li["location"] = extract_location(soup, url)

    price, price_src, candidates = extract_price_eur(soup)
    li["price_eur"] = price
    li["_price_src"] = price_src
    li["_price_candidates"] = candidates
    if price is None:
        ident = li.get("ad_id") or "noid"
        dump_path = OUT / f"price_fail_{ident}_{int(time.time())}.html"
        with contextlib.suppress(Exception):
            dump_path.write_text(soup.prettify(), encoding="utf-8")
            LOG.debug(f"[PRICE-DEBUG] dumped HTML -> {dump_path}")
    LOG.debug(f"[PRICE] {url} src={price_src} -> {li['price_eur']} cand={candidates}")

    d = soup.select_one("[class*='description'], [itemprop='description']")
    li["description"] = d.get_text("\n", strip=True) if d else ""
    # Features: grejanje (heating) and kanalizacija (sewage)
    li["heating"] = (find_spec_value(soup, ["Grejanje", "Tip grejanja"]) or "").strip()
    li["sewage"]  = (find_spec_value(soup, ["Kanalizacija"]) or "").strip()


    posted_utc, posted_local = extract_posted_date(soup)
    if posted_utc:   li["posted_date"]  = posted_utc
    if posted_local: li["posted_local"] = posted_local

    score, hits = F.desperation_score(" ".join([li.get("title",""), li.get("description",""), li.get("location",""), url]))
    if re.search(r"\bhitno\b", (li.get('title','') + " " + url), flags=re.I):
        score += 1
        hits = list(set(hits + ["url/title:hitno+1"]))
    li["desperation_score"], li["desperation_hits"] = score, hits

    dbg = {k:v for k,v in li.items() if k in ("url","ad_id","title","location","price_eur","_price_src","_price_candidates","posted_date","posted_local")}
    (OUT / f"dbg_{(li.get('ad_id') or 'noid')}.json").write_text(json.dumps(dbg, ensure_ascii=False, indent=2), encoding="utf-8")

    return li



# --- SPLIT send_alert ---


def send_alert(c: dict, listings: list[dict], subject_prefix: str = "[HaloWatch]") -> None:
    if not listings:
        return

    lines = []
    for li in listings:
        fs = li.get("first_seen")
        days_open = "?"
        with contextlib.suppress(Exception):
            if fs:
                days_open = (now_utc() - datetime.fromisoformat(fs)).days

        # Tags
        tags = []
        if li.get("desperation_score", 0) > 0:
            tags.append("🔥DESP")
        if li.get("_nearcap"):
            tags.append("NEARCAP")
        excl = li.get("_exclude_hits") or []
        if excl:
            tags.append("EXCL:" + ",".join(excl[:3]))

        # Price
        price = f"{int(li['price_eur']):,}".replace(",", ".") if li.get("price_eur") else "?"

        # Extras
        extras = []
        if li.get("heating"):
            extras.append(li["heating"])
        if li.get("sewage"):
            extras.append(li["sewage"])
        extras_str = (" | " + ", ".join(extras)) if extras else ""

        # Heartify important streets/areas in location
        loc_txt = heartify_location(li.get("location", "?"))

        ident = li.get("ad_id") or "-"
        tag = " ".join(tags) if tags else "-"

        lines.append(
            f"{price} EUR | {loc_txt}{extras_str} | days={days_open} | id={ident} | {tag} | {li['url']}"
        )

    body = "\n".join(lines)

    msg = EmailMessage()
    cap = c.get("max_price_eur")
    core = f"{len(listings)} listing(s) ≤ €{int(float(cap))}" if cap else f"{len(listings)} listing(s)"
    msg["Subject"] = f"{subject_prefix} {core}".strip()
    msg["From"] = c["email"]["username"]
    msg["To"] = c["email"]["to"]
    msg.set_content(body)

    LOG.info("EMAIL: connecting…")
    with smtplib.SMTP(c["email"]["smtp_server"], c["email"]["smtp_port"]) as s:
        s.set_debuglevel(1 if LOG.level <= logging.DEBUG else 0)
        s.ehlo(); s.starttls(context=ssl.create_default_context()); s.ehlo()
        try:
            s.login(c["email"]["username"], c["email"]["password"])
        except smtplib.SMTPAuthenticationError as e:
            LOG.error(f"EMAIL AUTH FAILED: {e.smtp_code} {e.smtp_error}. Tip: Gmail → App Password + 2FA.")
            raise
        refused = s.send_message(msg)
        if refused:
            LOG.warning(f"EMAIL: some recipients refused: {refused}")
        else:
            LOG.info("EMAIL: accepted by server")



# ---------- diag / raw dump ----------
async def diag_async():
    LOG.info("[DIAG] starting")
    with contextlib.suppress(Exception):
        ip = socket.gethostbyname("www.halooglasi.com")
        LOG.info(f"[DIAG] DNS halooglasi -> {ip}")
    async with new_client() as client:
        for u in ["https://example.com/", "https://httpbin.org/html"]:
            try:
                html = await http_get(client, u)
                name = u.split("//",1)[1].split("/",1)[0].replace(".", "_")
                (OUT / f"diag_{name}.html").write_text(html, encoding="utf-8")
                LOG.info(f"[DIAG] wrote diag_{name}.html ({len(html)} chars)")
            except Exception as e:
                LOG.warning(f"[DIAG] fetch failed {u}: {e}")

def dump_url_raw(url: str):
    # Keep sync variant for quick manual dumps (uses httpx in blocking mode via run)
    async def _go():
        async with new_client() as client:
            html = await http_get(client, url)
            (OUT / "manual_dump.html").write_text(html, encoding="utf-8")
            with contextlib.suppress(Exception):
                (OUT / "manual_dump.bin").write_bytes(html.encode("utf-8", errors="ignore"))
            LOG.debug(f"[DUMP] out/manual_dump.html ({len(html)} chars)")
    asyncio.run(_go())

# ---------- main (async orchestrator) ----------
async def amain():
    # --- config ---
    c = read_cfg()
    price_cap         = float(c.get("max_price_eur", 9e18))
    max_pages         = int(c.get("max_pages", 10))
    delay_s           = float(c.get("page_delay_seconds", PAGE_DELAY_S))
    cooldown_days     = int(c.get("cooldown_days", 2))
    price_change_only = bool(c.get("price_change_only", True))

    # filters policy from filters.py
    F.configure(
        areas=c.get("areas", []),
        max_price_eur=price_cap,
        price_tolerance_pct=c.get("price_tolerance_pct"),
    )

    # resolve search URLs
    urls = c.get("search_urls")
    if isinstance(urls, str):
        urls = [urls]
    if not urls:
        loc = c.get("location_url")
        urls = [loc] if loc else []
    if not urls:
        LOG.error("No search URLs. Set 'search_urls' (list) or 'location_url' (string) in config.yaml.")
        sys.exit(2)

    LOG.info(
        "CONFIG search_urls=%d price_cap=%s max_pages=%d cooldown_days=%d price_change_only=%s areas=%s concurrency=%d",
        len(urls), price_cap, max_pages, cooldown_days, price_change_only, c.get("areas"), CONCURRENCY
    )

    # --- init ---
    con  = db()
    seen = load_seen()

    # prepare normalized areas (strict, but tolerant to script/diacritics)
    raw_areas  = c.get("areas") or []
    NORM_AREAS = {sr_norm(a) for a in raw_areas if a and str(a).strip()}

    # excludes (e.g., borca, krnjaca)
    raw_excludes = c.get("exclude_areas") or []
    EXCL_NORM    = {sr_norm(x) for x in raw_excludes if x and str(x).strip()}
    soft_excludes = bool(c.get("soft_excludes", True))

    def exclude_hits_from(text: str) -> list[str]:
        blob = sr_norm(text)
        return [x for x in EXCL_NORM if x and x in blob]

    def area_hits_from(text: str) -> list[str]:
        blob = sr_norm(text)
        return [a for a in NORM_AREAS if a and a in blob]

    def key_for(li: dict) -> str:
        return li.get("ad_id") or li["url"]

    def should_alert_by_id(li: dict) -> bool:
        """Cooldown / price-change gate using seen.yml."""
        key = key_for(li)
        prev = seen.get(key)
        if not prev:
            # First time seeing this ID: allow if price parsed when price_change_only=True
            return True if not price_change_only else (li.get("price_eur") is not None)

        # cooldown
        last_alert = None
        with contextlib.suppress(Exception):
            if prev.get("last_alerted"):
                last_alert = datetime.fromisoformat(prev["last_alerted"])
        if last_alert and (now_utc() - last_alert).days < cooldown_days:
            return False

        # price-change check
        pp = prev.get("price_eur"); cp = li.get("price_eur")
        if price_change_only:
            with contextlib.suppress(Exception):
                return (cp is not None) and (pp is None or float(cp) != float(pp))
            return cp is not None
        return True

    def mark_alerted(li: dict):
        seen[key_for(li)] = {"price_eur": li.get("price_eur"), "last_alerted": now_utc().isoformat()}

    # --- gather links (INITIALIZE ONCE, THEN MERGE) ---
    all_links: set[str] = set()
    window = int(c.get("page_window") or 5)
    window = max(1, min(window, 8))  # safety clamp

    async with new_client() as client:
        for url in urls:
            LOG.info(f"[SEARCH] {url}")
            links = await fetch_all_links_paged_windowed(
                client, url, max_pages=max_pages, sleep_s=delay_s, window=window
            )
            if not links:
                LOG.warning(f"[SEARCH] 0 links from {url}")
            all_links |= links

        LOG.info(f"TOTAL UNIQUE LINKS: {len(all_links)}")
        if not all_links:
            LOG.warning("No links found — exiting early.")
            save_seen(seen)
            return

        # --- crawl listings (bounded concurrency) ---
        matched: list[dict] = []
        misses:  list[dict] = []

        async def handle(url: str):
            try:
                li = await fetch_listing_async(client, url)
                fs = upsert_listing(con, li)
                li["first_seen"] = fs

                # AREA gate
                if NORM_AREAS:
                    ah = area_hits_from((li.get("title","") + " " + li.get("location","")))
                    area_ok = bool(ah)
                else:
                    ah = []; area_ok = True

                # EXCLUDES (soft by default)
                xh = exclude_hits_from((li.get("title","") + " " + li.get("location","")))
                has_excluded = bool(xh)

                # PRICE gates: hard cap or near-cap
                price_ok = F.price_ok(li.get("price_eur"))
                near_ok  = F.near_price_ok(li.get("price_eur"))
                allow_by_price = bool(price_ok or near_ok)

                LOG.info(
                    "FILTER | id=%s | area_ok=%s hits=%s | price_ok=%s near_ok=%s price=%s cap=%s | excl=%s | src=%s | loc=%r | %s",
                    li.get("ad_id") or "-", area_ok, ah, price_ok, near_ok, li.get("price_eur"),
                    price_cap, xh, li.get("_price_src"), (li.get("location") or "")[:120], li["url"]
                )

                if area_ok and allow_by_price:
                    if has_excluded and not soft_excludes:
                        reasons = ["area_excluded"]
                        if not price_ok and near_ok: reasons.append("near_cap")
                        LOG.warning("REJECT | id=%s | reasons=%s | price=%s cap=%s | excl=%s | loc=%r | title=%r",
                                    li.get("ad_id") or "-", ";".join(reasons), li.get("price_eur"), price_cap, xh,
                                    (li.get("location") or "")[:100], (li.get("title") or "")[:100])
                        misses.append({**li, "excluded_reasons": reasons, "_exclude_hits": xh})
                    else:
                        # allowed; annotate flags for email
                        li["_exclude_hits"] = xh
                        li["_nearcap"] = (not price_ok) and near_ok

                        if should_alert_by_id(li):
                            matched.append(li)
                            mark_alerted(li)
                            LOG.info("MATCH  | id=%s | price=%s | near=%s | excl=%s | %s",
                                     li.get("ad_id") or "-", li.get("price_eur"), li["_nearcap"], xh, url)
                        else:
                            LOG.info("SKIP   | id=%s | cooldown/unchanged (price_change_only=%s)",
                                     li.get("ad_id") or "-", price_change_only)
                            misses.append({**li, "excluded_reasons": ["cooldown_or_no_change"], "_exclude_hits": xh})
                else:
                    reasons = []
                    if not area_ok: reasons.append("area_not_allowed")
                    if not allow_by_price:
                        reasons.append("price_above_limit" if li.get('price_eur') is not None else "price_not_parsed")
                    LOG.warning(
                        "REJECT | id=%s | reasons=%s | price=%s cap=%s ok(price,area)=(%s,%s) hits=%s excl=%s | loc=%r | title=%r",
                        li.get("ad_id") or "-", ";".join(reasons), li.get("price_eur"), price_cap,
                        allow_by_price, area_ok, ah, xh, (li.get("location") or "")[:100], (li.get("title") or "")[:100]
                    )
                    misses.append({**li, "excluded_reasons": reasons, "_exclude_hits": xh})
            except Exception as e:
                LOG.warning(f"listing failed {url}: {e}")

        tasks = [asyncio.create_task(handle(u)) for u in sorted(all_links)]
        # tiny jitter so we don't spike all at once
        for _ in range(len(tasks)):
            await asyncio.sleep(0.02)
        await asyncio.gather(*tasks)

    # --- post-processing ---
    matched.sort(key=lambda x: (-x.get("desperation_score", 0), x.get("price_eur", 1e18)))

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dump_yaml(OUT / f"matches_{ts}.yml", matched, "matches(ts)")
    dump_yaml(OUT / f"non_matches_{ts}.yml", misses, "non_matches(ts)")
    dump_yaml(OUT / "matches.yml", matched, "matches(latest)")
    dump_yaml(OUT / "non_matches.yml", misses, "non_matches(latest)")

    # mirror richer email lines (heating/sewage + tags)
    (OUT / "matches_email.txt").write_text("\n".join([
        (
            f"{(int(m['price_eur'])):,}".replace(',', '.') +
            " EUR | " + m.get('location','?') +
            ((" | " + ", ".join([x for x in [m.get('heating'), m.get('sewage')] if x]))
             if (m.get('heating') or m.get('sewage')) else "") +
            " | id=" + (m.get('ad_id') or '-') +
            (" | NEARCAP" if m.get('_nearcap') else "") +
            ((" | EXCL:" + ",".join(m.get('_exclude_hits')[:3])) if m.get('_exclude_hits') else "") +
            " | " + m['url']
        )
        for m in matched if m.get('price_eur')
    ]), encoding="utf-8")

    # ---- END-OF-RUN VISIBILITY: counters by reason+opština ----
    reason_ctr = Counter()
    opstina_ctr = Counter()
    for m in misses:
        for r in m.get("excluded_reasons", []):
            reason_ctr[r] += 1
        loc = m.get("location") or ""
        mm = re.search(r"Opština\s+([^-\|]+)", loc)
        if mm:
            opstina_ctr[mm.group(1).strip()] += 1
    LOG.info("SUMMARY non-matches by reason: %s", dict(reason_ctr))
    LOG.info("SUMMARY non-matches by opština: %s", dict(opstina_ctr))

    # persist seen
    save_seen(seen)

    # ---- email dispatch (split here; no top-level use of `matched`) ----
    if not c.get("email"):
        LOG.warning("EMAIL: no 'email' block in config.yaml -> skipping send.")
        return

    prime_listings    = [m for m in matched if not m.get('_exclude_hits')]
    excluded_listings = [m for m in matched if m.get('_exclude_hits')]

    LOG.info(f"EMAIL BUCKETS → prime={len(prime_listings)} excluded={len(excluded_listings)}")

    if prime_listings:
        try:
            send_alert(c, prime_listings, subject_prefix="Prime Listings Found -")
        except Exception:
            LOG.exception("EMAIL: exception during prime listings send")

    if excluded_listings:
        try:
            send_alert(c, excluded_listings, subject_prefix="Excluded Listings (Distance) -")
        except Exception:
            LOG.exception("EMAIL: exception during excluded listings send")

















# ---------- seen/dedupe ----------
def load_seen() -> dict:
    if SEEN.exists():
        with contextlib.suppress(Exception):
            return yaml.safe_load(SEEN.read_text()) or {}
    return {}

def save_seen(seen: dict):
    SEEN.write_text(yaml.safe_dump(seen, allow_unicode=True, sort_keys=False), encoding="utf-8")

# ---------- entry ----------
if __name__ == "__main__":
    try:
        if len(sys.argv) == 2 and sys.argv[1] == "--diag":
            asyncio.run(diag_async()); sys.exit(0)
        if len(sys.argv) == 2 and sys.argv[1].startswith("http"):
            dump_url_raw(sys.argv[1]); sys.exit(0)
        asyncio.run(amain())
    except KeyboardInterrupt:
        sys.exit(130)
