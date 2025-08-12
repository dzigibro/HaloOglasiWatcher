# --- filters.py (new) -------------------------------------------------
import re
import unicodedata

# Normalize text to plain lower ASCII (handles Serbian diacritics crudely)
def norm(s: str) -> str:
    if not s: return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return s.lower()

# Areas of Belgrade to allow (edit to your taste)
ALLOWED_AREAS = {
    "zemun", "novi beograd", "vracar", "zvezdara", "palilula", "vozdovac",
    "stari grad", "savski venac", "rakovica", "zarkovo", "mirijevo", "banovo brdo"
}

MAX_PRICE_EUR = 65000

# Desperation keywords (both EN + SR). Tweak freely.
# 'nije hitno' is the opposite, we handle it as an anti-hit later.
_DESP_PATTERNS = [
    r"\bhitno\b",
    r"\bbrzo\b",
    r"\bagencij[ae]\s+prodaje\s+brzo\b",
    r"\bdeal\b", r"\bdogovor\b", r"\bdogovor\s*(mozg?uc|possible)\b",
    r"\bfast\b", r"\bno\s*time\b", r"\bany\s*deal\b",
    r"\bsamo\s*danas\b", r"\bspustam\b", r"\bspustanje\b", r"\bpopust\b",
]
# Negative patterns that *reduce* desperation score
_ANTI_PATTERNS = [r"\bnije\s+hitno\b", r"\bno\s*rush\b"]

DESP_REGEX = [re.compile(p, re.I) for p in _DESP_PATTERNS]
ANTI_REGEX = [re.compile(p, re.I) for p in _ANTI_PATTERNS]

def desperation_score(text: str) -> tuple[int, list[str]]:
    t = norm(text)
    hits = [p.pattern for p in DESP_REGEX if p.search(t)]
    score = len(hits)
    anti = [p.pattern for p in ANTI_REGEX if p.search(t)]
    score -= len(anti)
    return max(score, 0), hits

def in_allowed_area(title: str, location: str) -> bool:
    t = norm(title) + " " + norm(location)
    return any(area in t for area in ALLOWED_AREAS)

def price_ok(price_eur: int | float | None) -> bool:
    try:
        return price_eur is not None and float(price_eur) <= MAX_PRICE_EUR
    except Exception:
        return False

def listing_passes(listing: dict) -> dict | None:
    """
    Expect listing dict like:
      {
        "title": "...",
        "location": "...",
        "price_eur": 59999,
        "url": "...",
        "description": "..."
      }
    """
    title = listing.get("title","")
    location = listing.get("location","")
    desc = listing.get("description","")

    if not in_allowed_area(title, location): 
        return None
    if not price_ok(listing.get("price_eur")): 
        return None

    score, hits = desperation_score(" ".join([title, desc]))
    listing["desperation_score"] = score
    listing["desperation_hits"]  = hits
    return listing
# ----------------------------------------------------------------------
