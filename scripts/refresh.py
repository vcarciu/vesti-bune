# scripts/refresh.py
import os
import re
import json
import time
import random
import hashlib
import unicodedata
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import requests
import feedparser
import yaml

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CONFIG_PATH = os.path.join(ROOT_DIR, "config", "sources.yml")

OUT_NEWS = os.path.join(ROOT_DIR, "data", "news.json")
OUT_ITEMS = os.path.join(ROOT_DIR, "data", "items.json")
JOKES_PATH = os.path.join(ROOT_DIR, "data", "jokes_ro.txt")
TOP_IMAGE_STATE_PATH = os.path.join(ROOT_DIR, "data", "top_images_state.json")

USER_AGENT = "vesti-bune-bot/strict-ro (+https://vcarciu.github.io/vesti-bune/)"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})

# -----------------------------
# Utils
# -----------------------------
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def safe_mkdir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def load_yaml(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

def strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def normalize_text(s: str) -> str:
    s = (s or "").lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"\s+", " ", s).strip()
    return s

def parse_entry_datetime(entry: Dict[str, Any]) -> Optional[datetime]:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                return datetime.fromtimestamp(time.mktime(t), tz=timezone.utc)
            except Exception:
                pass
    return None

def get_entry_summary(entry: Dict[str, Any]) -> str:
    if entry.get("summary"):
        return strip_html(entry.get("summary") or "")
    content = entry.get("content")
    if isinstance(content, list) and content:
        val = content[0].get("value") or ""
        return strip_html(val)
    return ""

def canonicalize_url(url: str) -> str:
    """
    Reduce duplicatele: scoate tracking params (utm_*, fbclid, gclid etc.)
    """
    try:
        parts = urlsplit(url)
        q = parse_qsl(parts.query, keep_blank_values=True)
        drop_prefixes = ("utm_",)
        drop_keys = {"fbclid", "gclid", "yclid", "mc_cid", "mc_eid", "cmpid"}
        q2 = [(k, v) for (k, v) in q if not k.lower().startswith(drop_prefixes) and k.lower() not in drop_keys]
        new_query = urlencode(q2, doseq=True)
        clean = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), new_query, ""))  # drop fragment
        return clean
    except Exception:
        return url

def dedupe_key(link: str, title: str) -> str:
    base = canonicalize_url(link or "") + "||" + (title or "")
    return hashlib.sha1(base.encode("utf-8", errors="ignore")).hexdigest()

def write_json(path: str, payload: Dict[str, Any]) -> None:
    safe_mkdir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

def read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}

# -----------------------------
# HTTP / RSS
# -----------------------------
def fetch_url_with_final(url: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        r = SESSION.get(url, timeout=25, allow_redirects=True)
        if r.status_code == 200 and r.text:
            return r.text, r.url
    except Exception:
        return None, None
    return None, None

def fetch_rss(url: str) -> feedparser.FeedParserDict:
    try:
        r = SESSION.get(url, timeout=25, allow_redirects=True)
        if r.status_code != 200:
            return feedparser.FeedParserDict(entries=[])
        return feedparser.parse(r.content)
    except Exception:
        return feedparser.FeedParserDict(entries=[])

# -----------------------------
# DeepL (optional)
# -----------------------------
def deepl_translate(text: str, target_lang: str = "RO") -> Optional[str]:
    key = os.getenv("DEEPL_API_KEY", "").strip()
    if not key or not (text or "").strip():
        return None

    text = text.strip()
    if len(text) > 900:
        text = text[:900]

    url_env = os.getenv("DEEPL_API_URL", "").strip()
    candidates = [url_env] if url_env else [
        "https://api-free.deepl.com/v2/translate",
        "https://api.deepl.com/v2/translate",
    ]

    data = {"auth_key": key, "text": text, "target_lang": target_lang}

    for url in [u for u in candidates if u]:
        try:
            r = requests.post(url, data=data, timeout=25, headers={"User-Agent": USER_AGENT})
            if r.status_code == 200:
                js = r.json()
                tr = js.get("translations", [])
                if tr:
                    out = (tr[0].get("text") or "").strip()
                    return out or None
        except Exception:
            continue
    return None

# -----------------------------
# Images (RSS + og:image)
# -----------------------------
OG_IMAGE_RE_1 = re.compile(r'property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', re.I)
OG_IMAGE_RE_2 = re.compile(r'content=["\']([^"\']+)["\']\s+property=["\']og:image["\']', re.I)
IMG_SRC_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)
PROMO_TAG_RE = re.compile(r'(^|\W)\(p\)(\W|$)', re.I)

def extract_og_image(html: str) -> Optional[str]:
    if not html:
        return None
    m = OG_IMAGE_RE_1.search(html) or OG_IMAGE_RE_2.search(html)
    if m:
        return (m.group(1) or "").strip() or None
    return None

def extract_image_url(e: dict) -> Optional[str]:
    mc = e.get("media_content")
    if isinstance(mc, list) and mc:
        for item in mc:
            url = (item.get("url") or "").strip()
            if url:
                return url

    mt = e.get("media_thumbnail")
    if isinstance(mt, list) and mt:
        for item in mt:
            url = (item.get("url") or "").strip()
            if url:
                return url

    links = e.get("links")
    if isinstance(links, list):
        for l in links:
            rel = (l.get("rel") or "").lower()
            href = (l.get("href") or "").strip()
            if rel == "enclosure" and href:
                return href

    html = e.get("summary") or ""
    if isinstance(html, str) and html:
        m = IMG_SRC_RE.search(html)
        if m:
            return (m.group(1) or "").strip() or None

    link = (e.get("link") or "").strip()
    if link:
        page_html, _final = fetch_url_with_final(link)
        img = extract_og_image(page_html or "")
        if img:
            return img.strip()

    return None

def is_valid_image_url(url: str) -> bool:
    u = (url or "").strip().lower()
    if not (u.startswith("http://") or u.startswith("https://")):
        return False
    bad = (".swf", "stewart.swf")
    if any(x in u for x in bad):
        return False
    good_ext = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")
    if any(u.split("?")[0].endswith(ext) for ext in good_ext):
        return True
    return ("staticflickr.com/" in u) or ("upload.wikimedia.org/" in u)

def is_top_photo_candidate(tag: str, title: str, summary: str, link: str) -> bool:
    text = normalize_text(f"{title} {summary} {link}")
    if tag == "space":
        good = _mk_norm_list(["space", "astronomy", "nebula", "galaxy", "milky way", "nasa", "telescope", "star"])
        bad = _mk_norm_list(["office", "cowork", "workspace", "interior", "meeting room", "desk"])
        return any(k in text for k in good) and not any(k in text for k in bad)
    if tag == "landscape":
        bad_animals = _mk_norm_list(["cat", "dog", "bird", "eagle", "hawk", "tiger", "lion", "wolf", "fox", "bear", "animal"])
        good_land = _mk_norm_list(["landscape", "nature", "mountain", "valley", "forest", "lake", "river", "sunset", "sunrise", "waterfall"])
        return any(k in text for k in good_land) and not any(k in text for k in bad_animals)
    if tag == "animals":
        good_animals = _mk_norm_list([
            "animal", "animals", "wildlife", "mammal", "bird", "cat", "dog", "fox", "wolf", "bear",
            "lion", "tiger", "elephant", "deer", "otter", "seal", "whale", "dolphin", "owl", "eagle",
            "toucan", "penguin", "koala", "panda"
        ])
        bad = _mk_norm_list([
            "space", "astronomy", "nebula", "galaxy", "planet", "milky way",
            "landscape", "mountain", "valley", "city", "urban", "architecture", "skyline",
        ])
        return any(k in text for k in good_animals) and not any(k in text for k in bad)
    if tag == "humans":
        bad = _mk_norm_list(["nude", "lingerie", "swimwear", "bikini", "nsfw", "erotic"])
        return not any(k in text for k in bad)
    if tag == "cities":
        good = _mk_norm_list(["city", "urban", "street", "skyline", "architecture", "night city"])
        bad = _mk_norm_list([
            "animal", "cat", "dog", "bird",
            "car", "cars", "vehicle", "vehicles", "truck", "automobile", "road",
            "honda", "toyota", "bmw", "audi", "mercedes", "fiat",
            "sedan", "suv", "turbo", "engine", "motor",
        ])
        return any(k in text for k in good) and not any(k in text for k in bad)
    if tag == "microcosmos":
        good = _mk_norm_list(["microscopy", "micrograph", "microscopic", "cell", "bacteria", "amoeba", "plankton"])
        bad = _mk_norm_list(["mountain", "landscape", "city", "street", "skyline", "forest", "lake"])
        return any(k in text for k in good) and not any(k in text for k in bad)
    return True

def fallback_image_url(section_id: str, title: str, link: str) -> str:
    """
    Fallback dinamic, determinist per articol, ca sa evitam aceeasi poza repetata.
    """
    seed_src = f"{section_id}|{title}|{link}"
    seed = hashlib.sha1(seed_src.encode("utf-8", errors="ignore")).hexdigest()[:16]
    return f"https://picsum.photos/seed/vb-{seed}/480/320"

# -----------------------------
# Filtering (RO)
# -----------------------------
def _mk_norm_list(words: List[str]) -> List[str]:
    return [normalize_text(x) for x in words if (x or "").strip()]

RAW_RO_HARD_BLOCK = [
    # politică / geo-politică / război (ex: UE + Ucraina)
    "scandal", "ue", "uniunea europeana", "uniunea europeană",
    "ucraina", "rusia", "putin", "zelenski", "nato",
    "razboi", "război", "war",
    "parlament", "guvern", "aleger", "ministr", "premier", "presed",
    "politica", "politică", "prefect", "prefectur", "amenzi",
    "psd", "pnl", "aur", "udmr",
    "trump", "biden", "stalin",
    "germania", "belgia", "ungaria",

    # justiție / penal (ex: iorgulescu)
    "condamn", "condamnare", "pedeaps", "inchiso", "închiso",
    "procuror", "procurorii", "parchet", "instanta", "instanță",
    "tribunal", "judec", "sentinta", "sentință", "apel", "recurs",
    "dna", "diicot", "inculpat", "penal",
    "iorgulescu",

    # armata / defense (nu vrei deloc)
    "armata", "armată", "militar", "military", "defense", "defence",
    "arma", "arme", "weapon", "weapons", "munitie", "muniție",
    "rachet", "missile", "drona", "drone", "tanc", "tank",

    # crime / violență / accidente / dezastre
    "crim", "ucis", "omor", "viol", "agresi", "drog", "arest", "retinut", "reținut",
    "accident", "traged", "dezastru", "exploz", "incend", "cutremur", "inunda",

    # alarmism
    "minim istoric", "record negativ", "crestere alarmanta", "creștere alarmantă",
    "alarmant", "explozie a cazurilor", "alerta", "alertă",

    # economie “rece” / scăderi statistice
    "ins", "institutul national de statistica", "institutul național de statistică",
    "cifra de afaceri", "volumul cifrei de afaceri", "serie bruta", "serie brută",
    "a scazut", "a scăzut", "scadere", "scădere", "in scadere", "în scădere",
    "indicator", "indice", "serie ajustata", "serie ajustată",
    "concedier", "disponibiliz", "somaj", "șomaj", "insolvent", "faliment",
    "contrabanda", "contrabandă", "tigarete de contrabanda", "tigarete de contrabandă",
    "fara canalizare", "fără canalizare", "asistenta sociala", "asistență socială", "invaliditate",
    "comisia europeana", "comisia europeană", "investigheaz", "investigatie", "investigație", "ajutor de stat",
    "israel", "gaza", "hamas", "hezbollah", "iran",
    "ambasada", "consulat", "repatrier", "evacuar", "alerta de calatorie", "alertă de călătorie",
    "conflict armat", "tensiuni militare",

    # comunicate/anunțuri (fix linkul tău Stiripozitive)
    "anunta publicul interesat", "anunță publicul interesat",
    "depunerii solicitarii", "depunerii solicitării",
    "acordului de mediu", "acord de mediu",
    "comunicat de presa", "comunicat de presă",
]
RO_HARD_BLOCK = _mk_norm_list(RAW_RO_HARD_BLOCK)

RAW_RO_POSITIVE_HINTS_STRICT = [
    "premiu", "a castigat", "a câștigat", "medalie", "record",
    "inaugur", "s-a deschis", "finalizat", "modernizat",
    "invest", "finant", "grant", "fonduri",
    "tratament nou", "terapie nou", "aprobat", "screening gratuit", "gratuit",
    "salvat", "donat", "campanie", "reabilitat",
    "scade", "reducere", "imbunatat", "îmbunatat",
    "adopt", "adopție", "adoptie", "salvare", "recuperat", "vindec",
    "inovatie", "inovație", "startup", "tehnologie", "descoperire",
    "educatie", "educație", "amuzant", "gluma", "glumă", "umor", "satira", "satiră",
]
RO_POSITIVE_HINTS_STRICT = _mk_norm_list(RAW_RO_POSITIVE_HINTS_STRICT)

RAW_RO_POSITIVE_HINTS_RELAXED = [
    "poveste", "happy end", "iubire", "dragoste", "logod", "nunta",
    "animale", "caine", "câine", "pisica", "pisică", "adopt", "salvare",
    "educatie", "educație", "scoala", "școala", "elev", "student", "universitate",
    "cultura", "teatru", "film", "festival", "muzeu",
    "sport", "victorie", "campion", "turneu",
    "startup", "inov", "tehnolog", "digital",
    "comunitate", "caritate", "strangere de fonduri", "strângere de fonduri",
    "amuzant", "funny", "gluma", "glumă",
]
RO_POSITIVE_HINTS_RELAXED = _mk_norm_list(RAW_RO_POSITIVE_HINTS_RELAXED)
MAINSTREAM_RO_SOURCES = {"Digi24", "HotNews"}
RO_MAINSTREAM_POSITIVE_GATE = _mk_norm_list([
    "salvat", "salvare", "eroi", "erou", "pompier", "smurd", "paramedic", "politist", "polițist",
    "medic", "operatie reusita", "operație reușită", "transplant reusit", "transplant reușit",
    "vindec", "recuperat", "adopt", "adoptie", "adopție",
    "premiu", "medalie", "record", "olimpiada", "olimpiadă",
    "inovatie", "inovație", "descoperire", "startup",
    "educatie", "educație", "profesor", "elev", "student",
    "amuzant", "umor", "satira", "satiră", "gluma", "glumă", "distractiv", "miracol",
])

def ro_hard_block(title: str, summary: str) -> bool:
    text = normalize_text(f"{title} {summary}")
    return any(kw in text for kw in RO_HARD_BLOCK if kw)

def ro_allow(title: str, summary: str, relaxed: bool = False) -> bool:
    if ro_hard_block(title, summary):
        return False

    text = normalize_text(f"{title} {summary}")

    for kw in RO_POSITIVE_HINTS_STRICT:
        if kw and kw in text:
            return True

    if not relaxed:
        return False

    for kw in RO_POSITIVE_HINTS_RELAXED:
        if kw and kw in text:
            return True

    return False

def ro_positive_hits(title: str, summary: str, relaxed: bool = False) -> int:
    text = normalize_text(f"{title} {summary}")
    hits = sum(1 for kw in RO_POSITIVE_HINTS_STRICT if kw and kw in text)
    if relaxed:
        hits += sum(1 for kw in RO_POSITIVE_HINTS_RELAXED if kw and kw in text)
    return hits

def ro_mainstream_allow(title: str, summary: str) -> bool:
    text = normalize_text(f"{title} {summary}")
    return any(kw in text for kw in RO_MAINSTREAM_POSITIVE_GATE if kw)

def is_satire_source(source_name: str, link: str) -> bool:
    name = (source_name or "").lower()
    ln = (link or "").lower()
    return ("times new roman" in name) or ("timesnewroman.ro" in ln)

PROMO_HINTS = _mk_norm_list([
    "publicitate", "advertorial", "sponsorizat", "parteneriat", "cod promo",
    "oferta", "ofertă", "reclama", "reclamă",
])

def is_promotional_item(title: str, summary: str) -> bool:
    text = normalize_text(f"{title} {summary}")
    if any(kw in text for kw in PROMO_HINTS if kw):
        return True
    raw = f"{title} {summary}".lower()
    return bool(PROMO_TAG_RE.search(raw))

def source_item_cap(section_id: str, source_name: str) -> int:
    if section_id != "romania":
        return 999
    if is_satire_source(source_name, ""):
        return 3
    if source_name in MAINSTREAM_RO_SOURCES:
        return 6
    return 10

FUNNY_HINTS = _mk_norm_list(["funny", "amuzant", "umor", "gluma", "glumă", "satira", "satiră", "distractiv"])
HEROIC_HINTS = _mk_norm_list(["salvat", "salvare", "politist", "polițist", "pompier", "medic", "smurd", "operatie reusita", "operație reușită", "miracol"])

def is_fun_or_hero_item(item: Dict[str, Any]) -> bool:
    text = normalize_text(f"{item.get('title','')} {item.get('summary','')} {item.get('source','')}")
    return any(kw in text for kw in FUNNY_HINTS + HEROIC_HINTS if kw)

def apply_fun_boost(items: List[Dict[str, Any]], top_k: int = 20, max_boost: int = 4, min_satire: int = 2) -> List[Dict[str, Any]]:
    if not items:
        return items
    boosted: List[Dict[str, Any]] = []
    seen = set()

    # 1) Prioritize satire in front (up to min_satire), if available.
    for it in items:
        if len(boosted) >= min_satire:
            break
        if is_satire_source(it.get("source", ""), it.get("link", "")):
            key = dedupe_key(it.get("link", ""), it.get("title", ""))
            if key not in seen:
                boosted.append(it)
                seen.add(key)

    # 2) Fill remaining boosted slots with fun/hero items from top_k.
    for idx, it in enumerate(items):
        if len(boosted) >= max_boost:
            break
        if idx >= top_k:
            break
        if is_fun_or_hero_item(it):
            key = dedupe_key(it.get("link", ""), it.get("title", ""))
            if key not in seen:
                boosted.append(it)
                seen.add(key)
    out = list(boosted)
    for it in items:
        key = dedupe_key(it.get("link", ""), it.get("title", ""))
        if key in seen:
            continue
        out.append(it)
    return out

# -----------------------------
# Global scoring (EN) – nu omorâm environment pe “crisis”
# -----------------------------
RAW_GLOBAL_HARD_NEG = [
    "war", "attack", "killed", "dead", "deaths", "shooting", "murder",
    "explosion", "earthquake", "flood", "wildfire", "terror", "hostage",
]
RAW_GLOBAL_SOFT_NEG = [
    "crisis", "risk", "disease", "outbreak", "shortage", "pollution", "emissions",
    "decline", "threat", "danger", "deadly", "fatal",
]
RAW_GLOBAL_POSITIVE = [
    "breakthrough", "promising", "improves", "improved", "improvement",
    "reduces risk", "reduced risk", "effective", "successful",
    "approved", "approval", "new treatment", "new therapy", "new method", "new approach",
    "discovery", "discover",
    "saved", "rescued", "reunited", "adoption",
    "clean energy", "renewable", "reforestation", "conservation", "cleanup", "restoration",
    "award", "wins",
]

GLOBAL_HARD_NEG = _mk_norm_list(RAW_GLOBAL_HARD_NEG)
GLOBAL_SOFT_NEG = _mk_norm_list(RAW_GLOBAL_SOFT_NEG)
GLOBAL_POSITIVE = _mk_norm_list(RAW_GLOBAL_POSITIVE)

def score_global(section_id: str, title: str, summary: str) -> int:
    text = normalize_text(f"{title} {summary}")

    for kw in GLOBAL_HARD_NEG:
        if kw and kw in text:
            return -999

    score = 0
    for kw in GLOBAL_POSITIVE:
        if kw and kw in text:
            score += 2

    if section_id in ("medical", "science"):
        for kw in _mk_norm_list(["study", "trial", "researchers", "clinical", "peer-reviewed", "meta-analysis"]):
            if kw and kw in text:
                score += 1

    if section_id == "environment":
        for kw in _mk_norm_list(["renewable", "solar", "wind", "reforestation", "conservation", "restoration", "recycling"]):
            if kw and kw in text:
                score += 1

    for kw in GLOBAL_SOFT_NEG:
        if kw and kw in text:
            score -= 1

    return score

# -----------------------------
# Banc + Satiră (fără Wikipedia)
# -----------------------------
JOKES_FALLBACK = [
    "De ce a trecut programatorul strada? Ca sa ajunga pe celalalt branch.",
    "Mi-am facut lista de prioritati. Primul punct: cafeaua.",
    "Azi am fost foarte productiv: am inchis 17 tab-uri.",
    "Am zis ca merg la sala. Am mers in bucatarie. A avut si ea greutati.",
    "Nu procrastinez. Doar imi las ideile sa se coaca lent.",
]

def load_jokes_from_file(path: str) -> List[str]:
    if not os.path.exists(path):
        return []

    jokes: List[str] = []
    seen = set()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            t = line.strip()
            if not t or t.startswith("#") or len(t) < 12:
                continue
            key = normalize_text(t)
            if key in seen:
                continue
            seen.add(key)
            jokes.append(t)
    return jokes

SATIRE_FALLBACK = [
    {"title": "Românul a descoperit secretul fericirii: a oprit notificările și a pornit viața", "link": "#"},
    {"title": "Bucureșteanul a făcut 10.000 de pași: 9.800 au fost căutând loc de parcare", "link": "#"},
    {"title": "Un cetățean a cerut „doar vești bune”; internetul a intrat în concediu medical", "link": "#"},
    {"title": "Specialiștii confirmă: 80% din stres dispare când nu mai răspunzi la „ai un minut?”", "link": "#"},
    {"title": "Un optimist a spus „se rezolvă”; problema s-a speriat și a plecat singură", "link": "#"},
]

def build_joke() -> Dict[str, Any]:
    jokes = load_jokes_from_file(JOKES_PATH) or JOKES_FALLBACK
    now_utc = datetime.now(timezone.utc)
    day = now_utc.strftime("%Y-%m-%d")
    slot = now_utc.hour // 6  # rotate every 6 hours
    seed = f"{day}-{slot}"
    idx = int(hashlib.sha1(seed.encode("utf-8")).hexdigest(), 16) % len(jokes)
    source = "data/jokes_ro.txt" if jokes is not JOKES_FALLBACK else "fallback"
    return {"date_utc": day, "text": jokes[idx], "source": source}

def build_satire() -> Dict[str, Any]:
    """
    Încearcă TimesNewRoman RSS. Dacă nu merge (uneori dă 400), folosește fallback.
    """
    feed_url = "https://www.timesnewroman.ro/feed/"
    f = fetch_rss(feed_url)
    if getattr(f, "entries", None):
        e = f.entries[0]
        title = (e.get("title") or "").strip()
        link = (e.get("link") or "").strip()
        if title and link:
            return {"title": title, "link": link, "source": "timesnewroman.ro"}
    # fallback local
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    idx = int(hashlib.sha1(("satire-" + day).encode("utf-8")).hexdigest(), 16) % len(SATIRE_FALLBACK)
    item = SATIRE_FALLBACK[idx]
    return {"title": item["title"], "link": item["link"], "source": "fallback"}

# -----------------------------
# Top images (Flickr RSS)
# -----------------------------
SAFE_ANIMALS_IMAGES = [
    "https://upload.wikimedia.org/wikipedia/commons/3/3a/Cat03.jpg",
    "https://upload.wikimedia.org/wikipedia/commons/6/6e/Golde33443.jpg",
    "https://upload.wikimedia.org/wikipedia/commons/4/40/Siberischer_tiger_de_edit02.jpg",
]
ANIMALS_IMAGE_LOCAL_FALLBACK = "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 1200 800'><defs><linearGradient id='g' x1='0' x2='1'><stop offset='0' stop-color='%23122b1f'/><stop offset='1' stop-color='%230f172a'/></linearGradient></defs><rect width='1200' height='800' fill='url(%23g)'/><text x='600' y='420' text-anchor='middle' fill='%23e5f3ea' font-size='86' font-family='Arial, sans-serif'>Animals</text></svg>"
TOP_IMAGE_DEFAULTS = {
    "space": [
        "https://upload.wikimedia.org/wikipedia/commons/e/e5/NGC_4414_%28NASA-med%29.jpg",
        "https://upload.wikimedia.org/wikipedia/commons/0/00/Crab_Nebula.jpg",
        "https://upload.wikimedia.org/wikipedia/commons/c/c7/The_Pillars_of_Creation_%28NIRCam_Image%29.jpg",
        "https://upload.wikimedia.org/wikipedia/commons/1/1a/PIA12235_HIRISE_view_of_Mars.jpg",
        "https://upload.wikimedia.org/wikipedia/commons/9/95/ESO_-_Milky_Way.jpg",
        "https://live.staticflickr.com/65535/55118553617_ccb01fe436_b.jpg",
    ],
    "animals": [
        "https://upload.wikimedia.org/wikipedia/commons/4/40/Siberischer_tiger_de_edit02.jpg",
        "https://upload.wikimedia.org/wikipedia/commons/3/3a/Cat03.jpg",
        "https://upload.wikimedia.org/wikipedia/commons/6/6e/Golde33443.jpg",
        "https://upload.wikimedia.org/wikipedia/commons/5/56/Toco_Toucan_RWD.jpg",
        "https://upload.wikimedia.org/wikipedia/commons/3/37/African_Bush_Elephant.jpg",
        "https://upload.wikimedia.org/wikipedia/commons/1/15/Red_fox_-_november_2011.jpg",
    ],
    "landscape": [
        "https://upload.wikimedia.org/wikipedia/commons/7/7c/Mount_Rainier_from_above_Myrtle_Falls_in_August.JPG",
        "https://upload.wikimedia.org/wikipedia/commons/3/3f/Fronalpstock_big.jpg",
        "https://upload.wikimedia.org/wikipedia/commons/2/2c/Moraine_Lake_17092005.jpg",
        "https://upload.wikimedia.org/wikipedia/commons/8/88/Torres_del_Paine_%28Chile%29.jpg",
        "https://upload.wikimedia.org/wikipedia/commons/6/6f/YosemitePark2_amk.jpg",
        "https://upload.wikimedia.org/wikipedia/commons/2/27/Swiss_Alps_Jungfrau-Aletsch.jpg",
    ],
    "humans": [
        "https://upload.wikimedia.org/wikipedia/commons/d/d3/Albert_Einstein_Head.jpg",
    ],
    "cities": [
        "https://upload.wikimedia.org/wikipedia/commons/3/3f/NYC_Midtown_Skyline_at_night_-_Jan_2006_edit1.jpg",
    ],
    "microcosmos": [
        "https://upload.wikimedia.org/wikipedia/commons/b/bc/SEM_blood_cells.jpg",
        "https://live.staticflickr.com/3820/9664730246_192ecc1c9e_b.jpg",
    ],
}
STATIC_TOP_TAGS = {"space", "animals", "landscape"}
TOP_IMAGE_LINKS = {
    "space": "https://commons.wikimedia.org/wiki/Category:Astronomy",
    "animals": "https://commons.wikimedia.org/wiki/Category:Animals",
    "landscape": "https://commons.wikimedia.org/wiki/Category:Landscapes",
    "humans": "https://commons.wikimedia.org/wiki/Category:People",
    "cities": "https://commons.wikimedia.org/wiki/Category:Cities",
    "microcosmos": "https://commons.wikimedia.org/wiki/Category:Microscopy",
}
TOP_IMAGE_HISTORY_LIMIT = 18
TOP_IMAGE_ROTATION_HOURS = 12
TOP_IMAGE_USED_LIMIT = 20000
TOP_IMAGE_SLOT_CACHE_LIMIT = 1200

def _normalize_history_map(raw: Any) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    if not isinstance(raw, dict):
        return out
    for tag, arr in raw.items():
        if not isinstance(tag, str) or not isinstance(arr, list):
            continue
        cleaned: List[str] = []
        seen = set()
        for x in arr:
            u = (x or "").strip() if isinstance(x, str) else ""
            if not u or u in seen:
                continue
            seen.add(u)
            cleaned.append(u)
        if cleaned:
            out[tag] = cleaned[:TOP_IMAGE_HISTORY_LIMIT]
    return out

def _current_rotation_slot(hours: int = TOP_IMAGE_ROTATION_HOURS) -> int:
    now_ts = datetime.now(timezone.utc).timestamp()
    return int(now_ts // max(1, hours * 3600))

def _load_top_image_state() -> Dict[str, Any]:
    raw = read_json(TOP_IMAGE_STATE_PATH)
    if not isinstance(raw, dict):
        raw = {}
    used = raw.get("used_by_tag")
    if not isinstance(used, dict):
        used = {}
    chosen = raw.get("chosen_by_slot")
    if not isinstance(chosen, dict):
        chosen = {}
    return {"used_by_tag": used, "chosen_by_slot": chosen}

def _save_top_image_state(state: Dict[str, Any]) -> None:
    write_json(TOP_IMAGE_STATE_PATH, state)

def _fallback_unique_image(tag: str, slot: int, nonce: int = 0) -> str:
    lock = abs(int(hashlib.sha1(f"{tag}|{slot}|{nonce}".encode("utf-8")).hexdigest(), 16)) % 1000000
    queries = {
        "space": "space,galaxy,nebula,astronomy",
        "animals": "animals,wildlife,bird,mammal",
        "landscape": "landscape,mountains,nature,valley",
    }
    q = queries.get(tag, "nature,photo")
    return f"https://loremflickr.com/1600/900/{q}?lock={lock}"

def _top_tag_feeds(tag: str) -> List[str]:
    if tag == "space":
        return [
            "https://www.flickr.com/services/feeds/photos_public.gne?format=rss2&tags=astronomy,nebula,galaxy,nasa,telescope&tagmode=any",
            "https://www.flickr.com/services/feeds/photos_public.gne?format=rss2&tags=space,stars,milkyway,cosmos&tagmode=any",
        ]
    if tag == "animals":
        return [
            "https://www.flickr.com/services/feeds/photos_public.gne?format=rss2&tags=animals,wildlife,nature&tagmode=any",
            "https://www.flickr.com/services/feeds/photos_public.gne?format=rss2&tags=animal,bird,mammal&tagmode=any",
        ]
    if tag == "landscape":
        return [
            "https://www.flickr.com/services/feeds/photos_public.gne?format=rss2&tags=landscape,nature,mountains,forest,lake&tagmode=any",
            "https://www.flickr.com/services/feeds/photos_public.gne?format=rss2&tags=scenery,valley,waterfall,sunset&tagmode=any",
        ]
    return []

def _fetch_top_tag_candidates(tag: str) -> List[str]:
    out: List[str] = []
    seen = set()
    for url in _top_tag_feeds(tag):
        try:
            f = fetch_rss(url)
            entries = list((f.entries or [])[:120])
            random.shuffle(entries)
            for e in entries:
                title = (e.get("title") or "").strip()
                link = (e.get("link") or "").strip()
                summary = get_entry_summary(e)
                img = extract_image_url(e) or None
                if not img or not is_valid_image_url(img):
                    continue
                if not is_top_photo_candidate(tag, title, summary, link):
                    continue
                if img in seen:
                    continue
                seen.add(img)
                out.append(img)
        except Exception:
            continue
    return out

def _pick_unique_for_slot(tag: str, slot: int, state: Dict[str, Any], history: Dict[str, List[str]]) -> Optional[str]:
    slot_key = str(slot)
    chosen_by_slot = state.get("chosen_by_slot") or {}
    used_by_tag = state.get("used_by_tag") or {}
    existing_slot = chosen_by_slot.get(slot_key)
    if isinstance(existing_slot, dict):
        url = (existing_slot.get(tag) or "").strip()
        if url:
            return url

    used_list = used_by_tag.get(tag)
    if not isinstance(used_list, list):
        used_list = []
    used_set = set(str(x).strip() for x in used_list if isinstance(x, str))

    candidates = _fetch_top_tag_candidates(tag)
    defaults = TOP_IMAGE_DEFAULTS.get(tag) or []
    candidates.extend([x for x in defaults if x])

    chosen = None
    for c in candidates:
        if c not in used_set:
            chosen = c
            break
    if not chosen:
        nonce = len(used_list) + 1
        chosen = _fallback_unique_image(tag, slot, nonce=nonce)

    used_list.append(chosen)
    if len(used_list) > TOP_IMAGE_USED_LIMIT:
        used_list = used_list[-TOP_IMAGE_USED_LIMIT:]
    used_by_tag[tag] = used_list

    bucket = chosen_by_slot.get(slot_key)
    if not isinstance(bucket, dict):
        bucket = {}
    bucket[tag] = chosen
    chosen_by_slot[slot_key] = bucket

    if len(chosen_by_slot) > TOP_IMAGE_SLOT_CACHE_LIMIT:
        keys = sorted((int(k), k) for k in chosen_by_slot.keys() if str(k).isdigit())
        remove_n = len(chosen_by_slot) - TOP_IMAGE_SLOT_CACHE_LIMIT
        for _i, k in keys[:remove_n]:
            chosen_by_slot.pop(k, None)

    state["used_by_tag"] = used_by_tag
    state["chosen_by_slot"] = chosen_by_slot

    _update_history(history, tag, chosen)
    return chosen

def _pick_for_slot(candidates: List[str], tag: str, slot: int) -> Optional[str]:
    unique = []
    seen = set()
    for c in candidates or []:
        u = (c or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        unique.append(u)
    if not unique:
        return None
    basis = f"{tag}|{slot}"
    idx = int(hashlib.sha1(basis.encode("utf-8")).hexdigest(), 16) % len(unique)
    return unique[idx]

def _pick_with_history(candidates: List[str], recent: List[str], tag: str = "", slot: Optional[int] = None) -> Optional[str]:
    if not candidates:
        return None
    unique = []
    seen = set()
    for c in candidates:
        u = (c or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        unique.append(u)
    if not unique:
        return None

    recent_set = set(recent or [])
    fresh = [u for u in unique if u not in recent_set]
    if fresh:
        pool = fresh
    elif recent and len(unique) > 1:
        # Evita repetarea imediata chiar daca toate candidatele sunt deja in istoric.
        pool = [u for u in unique if u != recent[0]] or unique
    else:
        pool = unique
    if not pool:
        return None
    if slot is None:
        return random.choice(pool)
    basis = f"{tag}|{slot}"
    idx = int(hashlib.sha1(basis.encode("utf-8")).hexdigest(), 16) % len(pool)
    return pool[idx]

def _update_history(history: Dict[str, List[str]], tag: str, image_url: str) -> None:
    if not tag or not image_url:
        return
    existing = list(history.get(tag) or [])
    existing = [x for x in existing if x != image_url]
    existing.insert(0, image_url)
    history[tag] = existing[:TOP_IMAGE_HISTORY_LIMIT]

def pick_flickr_images(limit: int = 3, prev_payload: Optional[Dict[str, Any]] = None) -> Tuple[List[Dict[str, Any]], Dict[str, List[str]]]:
    prev_payload = prev_payload or {}
    history = _normalize_history_map(prev_payload.get("top_image_history"))
    top_state = _load_top_image_state()
    prev_top = prev_payload.get("top_images")
    if isinstance(prev_top, list):
        for x in prev_top:
            if not isinstance(x, dict):
                continue
            tag = (x.get("tag") or "").strip()
            img = (x.get("image") or "").strip()
            if tag and img:
                _update_history(history, tag, img)
    slot = _current_rotation_slot(TOP_IMAGE_ROTATION_HOURS)

    feeds = [
        ("space", "https://www.flickr.com/services/feeds/photos_public.gne?format=rss2&tags=astronomy,nebula,galaxy,nasa,telescope&tagmode=any"),
        ("humans", "https://www.flickr.com/services/feeds/photos_public.gne?format=rss2&tags=portrait,people,human&tagmode=all"),
        ("cities", "https://www.flickr.com/services/feeds/photos_public.gne?format=rss2&tags=city,urban,street,skyline,architecture&tagmode=any"),
        ("landscape", "https://www.flickr.com/services/feeds/photos_public.gne?format=rss2&tags=landscape,nature,mountains,forest,lake&tagmode=any"),
        ("microcosmos", "https://www.flickr.com/services/feeds/photos_public.gne?format=rss2&tags=microscopy,micrograph,cell,bacteria&tagmode=all"),
    ]

    out: List[Dict[str, Any]] = []
    for tag, url in feeds:
        recent = history.get(tag, [])
        if tag in STATIC_TOP_TAGS:
            picked = _pick_unique_for_slot(tag, slot, top_state, history)
            if picked:
                out.append({
                    "tag": tag,
                    "title": tag.title(),
                    "link": TOP_IMAGE_LINKS.get(tag, "#"),
                    "image": picked,
                })
                _update_history(history, tag, picked)
            continue
        try:
            f = fetch_rss(url)
            if not f.entries:
                raise RuntimeError("empty feed")
            entries = list(f.entries[:60])
            random.shuffle(entries)
            candidates: List[Dict[str, Any]] = []
            for e in entries:
                title = (e.get("title") or "").strip()
                link = (e.get("link") or "").strip()
                summary = get_entry_summary(e)
                img = extract_image_url(e) or None
                if not img or not is_valid_image_url(img):
                    continue
                if not is_top_photo_candidate(tag, title, summary, link):
                    continue
                candidates.append({"tag": tag, "title": title, "link": link, "image": img})
            if not candidates:
                raise RuntimeError("no valid candidates")

            img_candidates = [c["image"] for c in candidates]
            picked_img = _pick_with_history(img_candidates, recent, tag=tag, slot=slot)
            chosen = next((c for c in candidates if c["image"] == picked_img), candidates[0])
            out.append(chosen)
            _update_history(history, tag, chosen["image"])
        except Exception:
            defaults = TOP_IMAGE_DEFAULTS.get(tag) or []
            picked = _pick_with_history(defaults, recent, tag=tag, slot=slot)
            if picked:
                out.append({
                    "tag": tag,
                    "title": tag.title(),
                    "link": TOP_IMAGE_LINKS.get(tag, "#"),
                    "image": picked,
                })
                _update_history(history, tag, picked)
    required = ["space", "animals", "landscape"]
    by_tag = {x.get("tag"): x for x in out if x and x.get("tag")}
    final: List[Dict[str, Any]] = []
    for tag in required:
        if tag in by_tag:
            final.append(by_tag[tag])
            continue
        if tag in STATIC_TOP_TAGS:
            picked = _pick_unique_for_slot(tag, slot, top_state, history)
        else:
            defaults = TOP_IMAGE_DEFAULTS.get(tag) or []
            picked = _pick_for_slot(defaults, tag=tag, slot=slot)
        if not picked:
            continue
        chosen = {
            "tag": tag,
            "title": tag.title(),
            "link": TOP_IMAGE_LINKS.get(tag, "#"),
            "image": picked,
        }
        final.append(chosen)
        _update_history(history, tag, chosen["image"])
    _save_top_image_state(top_state)
    return final[:limit], history

def build_mix_items(sections: Dict[str, List[Dict[str, Any]]], ro_head: int = 10, max_items: int = 120) -> List[Dict[str, Any]]:
    ro = list(sections.get("romania") or [])
    en = list((sections.get("medical") or []) + (sections.get("science") or []) + (sections.get("environment") or []))

    ro.sort(key=lambda x: x.get("published_utc", ""), reverse=True)
    en.sort(key=lambda x: x.get("published_utc", ""), reverse=True)

    out: List[Dict[str, Any]] = []
    out.extend(ro[:ro_head])

    ro_tail = ro[ro_head:]
    i = 0
    j = 0

    while len(out) < max_items and (i < len(ro_tail) or j < len(en)):
        for _ in range(2):
            if len(out) >= max_items:
                break
            if i < len(ro_tail):
                out.append(ro_tail[i])
                i += 1
        if len(out) >= max_items:
            break
        if j < len(en):
            out.append(en[j])
            j += 1

    leftovers = ro_tail[i:] + en[j:]
    leftovers.sort(key=lambda x: x.get("published_utc", ""), reverse=True)
    for it in leftovers:
        if len(out) >= max_items:
            break
        out.append(it)

    return apply_fun_boost(out, top_k=8, max_boost=1, min_satire=0)

def build_emergency_sections() -> Dict[str, List[Dict[str, Any]]]:
    """
    Fallback local folosit doar daca toate feed-urile returneaza 0 articole.
    Pastreaza site-ul functional si pozitiv chiar si cand RSS-urile pica.
    """
    now = utc_now_iso()
    return {
        "romania": [
            {
                "section": "romania",
                "kind": "ro",
                "source": "Vesti Bune",
                "title": "Tineri voluntari planteaza copaci in mai multe orase din Romania",
                "summary": "Actiuni locale de reimpadurire au atras sute de participanti si sprijin din comunitate.",
                "link": "https://www.wwf.ro/ce_facem/paduri/",
                "published_utc": now,
                "score": 3,
                "image": "https://upload.wikimedia.org/wikipedia/commons/0/0f/Forest_ecosystem.jpg",
            },
            {
                "section": "romania",
                "kind": "ro",
                "source": "Vesti Bune",
                "title": "Elevi romani premiati la concursuri internationale de stiinta",
                "summary": "Loturile de elevi continua sa obtina rezultate foarte bune la olimpiade si competitii STEM.",
                "link": "https://www.edu.ro/",
                "published_utc": now,
                "score": 3,
                "image": "https://upload.wikimedia.org/wikipedia/commons/1/1d/Students_in_a_science_lab.jpg",
            },
            {
                "section": "romania",
                "kind": "ro",
                "source": "Vesti Bune",
                "title": "Medicii raporteaza tot mai multe interventii minim invazive reusite",
                "summary": "Noile tehnici reduc timpul de recuperare si cresc confortul pacientilor.",
                "link": "https://insp.gov.ro/",
                "published_utc": now,
                "score": 3,
                "image": "https://upload.wikimedia.org/wikipedia/commons/d/d6/Doctor_and_patient.jpg",
            },
        ],
        "medical": [
            {
                "section": "medical",
                "kind": "global",
                "source": "ScienceDaily Health",
                "title": "Researchers report promising results for early disease screening",
                "summary": "A new method may help detect disease earlier and improve treatment outcomes.",
                "link": "https://www.sciencedaily.com/news/health_medicine/",
                "published_utc": now,
                "score": 2,
                "image": "https://upload.wikimedia.org/wikipedia/commons/8/8f/Microscope.jpg",
            }
        ],
        "science": [
            {
                "section": "science",
                "kind": "global",
                "source": "NASA",
                "title": "New space images reveal details about distant galaxies",
                "summary": "Fresh observations help scientists better understand galaxy evolution.",
                "link": "https://www.nasa.gov/news/all-news/",
                "published_utc": now,
                "score": 2,
                "image": "https://upload.wikimedia.org/wikipedia/commons/e/e5/NGC_4414_%28NASA-med%29.jpg",
            }
        ],
        "environment": [
            {
                "section": "environment",
                "kind": "global",
                "source": "Positive News",
                "title": "Local conservation projects restore habitats for wildlife",
                "summary": "Community-led restoration efforts are improving biodiversity in several regions.",
                "link": "https://www.positive.news/environment/",
                "published_utc": now,
                "score": 2,
                "image": "https://upload.wikimedia.org/wikipedia/commons/3/3f/Fronalpstock_big.jpg",
            }
        ],
    }

# -----------------------------
# Build sections
# -----------------------------
def build_sections(cfg: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    rss_sources = cfg.get("rss_sources") or {}
    sections_def = cfg.get("sections") or []
    filters_cfg = cfg.get("filters") or {}

    thresholds = (filters_cfg.get("thresholds") or {})
    ro_min_items = int(filters_cfg.get("ro_min_items", 16))
    ro_try_relaxed = bool(filters_cfg.get("ro_try_relaxed", True))
    ro_max_age_days = int(filters_cfg.get("ro_max_age_days", 45))
    now_utc = datetime.now(timezone.utc)

    max_items_map = {s["id"]: int(s.get("max_items", 20)) for s in sections_def if "id" in s}

    out: Dict[str, List[Dict[str, Any]]] = {}
    seen: set = set()
    seen_titles: set = set()
    ro_candidates: List[Dict[str, Any]] = []

    for section_id, sources in rss_sources.items():
        items: List[Dict[str, Any]] = []

        for src in sources:
            name = src.get("name", section_id)
            url = (src.get("url") or "").strip()
            if not url:
                continue
            cap = source_item_cap(section_id, name)
            kept_from_source = 0

            feed = fetch_rss(url)
            entries = list((feed.entries or [])[:90])
            if is_satire_source(name, url):
                random.shuffle(entries)
            for e in entries:
                if kept_from_source >= cap:
                    break
                title = (e.get("title") or "").strip()
                link = (e.get("link") or "").strip()
                if not title or not link:
                    continue

                link = canonicalize_url(link)
                summary = get_entry_summary(e)
                if is_promotional_item(title, summary):
                    continue
                dt = parse_entry_datetime(e)
                published = (dt or datetime.now(timezone.utc)).replace(microsecond=0)

                kind = "ro" if section_id == "romania" else "global"

                if kind == "ro":
                    satire_source = is_satire_source(name, url)
                    if satire_source:
                        if ro_hard_block(title, summary):
                            continue
                        score = 3
                    else:
                        strict_hits = ro_positive_hits(title, summary, relaxed=False)
                        if name in MAINSTREAM_RO_SOURCES and strict_hits < 1 and not ro_mainstream_allow(title, summary):
                            continue
                        if ro_max_age_days > 0:
                            age_days = (now_utc - published).days
                            if age_days > ro_max_age_days:
                                continue
                        if not ro_allow(title, summary, relaxed=False):
                            if ro_try_relaxed and not ro_hard_block(title, summary):
                                if name in MAINSTREAM_RO_SOURCES:
                                    continue
                                ro_candidates.append({
                                    "section": section_id,
                                    "kind": "ro",
                                    "source": name,
                                    "title": title,
                                    "summary": summary,
                                    "link": link,
                                    "published_utc": published.isoformat(),
                                    "score": 1,
                                })
                            continue
                        score = 3
                else:
                    score = score_global(section_id, title, summary)
                    if score < 0:
                        continue

                thr = int(thresholds.get(section_id, 0))
                if kind == "global":
                    # Keep global constructive articles even when they score neutral.
                    thr = min(thr, 0)
                if score < thr:
                    continue

                key = dedupe_key(link, title)
                if key in seen:
                    continue
                title_key = normalize_text(title)
                if title_key in seen_titles:
                    continue
                seen.add(key)
                seen_titles.add(title_key)

                item: Dict[str, Any] = {
                    "section": section_id,
                    "kind": kind,
                    "source": name,
                    "title": title,
                    "summary": summary,
                    "link": link,
                    "published_utc": published.isoformat(),
                    "score": score,
                }
                if is_satire_source(name, link):
                    item["title"] = f"😂 {item['title']}"

                img = extract_image_url(e)
                item["image"] = img or fallback_image_url(section_id, title, link)

                if kind == "global":
                    tr_title = deepl_translate(title) or None
                    tr_sum = deepl_translate(summary) or None
                    if tr_title:
                        item["title_ro"] = tr_title
                    if tr_sum:
                        item["summary_ro"] = tr_sum

                items.append(item)
                kept_from_source += 1

        items.sort(key=lambda x: x.get("published_utc", ""), reverse=True)
        items = items[: max_items_map.get(section_id, 20)]
        out[section_id] = items

    # Relaxed RO fallback
    if "romania" in out and ro_try_relaxed and len(out["romania"]) < ro_min_items:
        needed = ro_min_items - len(out["romania"])
        relaxed_ok = [c for c in ro_candidates if ro_allow(c["title"], c["summary"], relaxed=True)]
        relaxed_ok.sort(key=lambda x: x.get("published_utc", ""), reverse=True)

        already = set(dedupe_key(it["link"], it["title"]) for it in out["romania"])
        added = 0
        for c in relaxed_ok:
            if added >= needed:
                break
            k = dedupe_key(c["link"], c["title"])
            if k in already:
                continue
            already.add(k)

            html, _final = fetch_url_with_final(c["link"])
            img = extract_og_image(html or "")
            c["image"] = img or fallback_image_url(c["section"], c["title"], c["link"])

            out["romania"].append(c)
            added += 1

        out["romania"].sort(key=lambda x: x.get("published_utc", ""), reverse=True)
        out["romania"] = out["romania"][: max_items_map.get("romania", 40)]

    return out

# -----------------------------
# Main
# -----------------------------
def main() -> None:
    cfg = load_yaml(CONFIG_PATH)
    sections = build_sections(cfg)
    total_items = sum(len(v or []) for v in (sections or {}).values())

    prev = read_json(OUT_NEWS)
    prev_sections = prev.get("sections") if isinstance(prev, dict) else {}
    prev_total = sum(len(v or []) for v in (prev_sections or {}).values()) if isinstance(prev_sections, dict) else 0

    effective_sections = sections
    effective_mix = build_mix_items(sections)
    if total_items == 0 and prev_total > 0:
        effective_sections = prev_sections
        effective_mix = list(prev.get("mix_items") or [])
        print("[WARN] current refresh returned 0 items; keeping previous news sections")
    elif total_items == 0:
        effective_sections = build_emergency_sections()
        effective_mix = build_mix_items(effective_sections)
        print("[WARN] current refresh returned 0 items; using emergency positive fallback")

    top_images, top_image_history = pick_flickr_images(limit=3, prev_payload=prev if isinstance(prev, dict) else None)

    payload: Dict[str, Any] = {
        "generated_utc": utc_now_iso(),
        "sections": effective_sections,
        "mix_items": effective_mix,
        "joke_ro": build_joke(),
        "satire_ro": build_satire(),
        "top_images": top_images,
        "top_image_history": top_image_history,
    }
    write_json(OUT_NEWS, payload)
    write_json(OUT_ITEMS, payload)
    print("[OK] wrote data/news.json and data/items.json")

if __name__ == "__main__":
    main()
