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
PUBLISHED_STATE_PATH = os.path.join(ROOT_DIR, "data", "published_state.json")

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

def normalized_title_key(title: str) -> str:
    """
    Normalizeaza titlurile agresiv pentru deduplicare intre surse:
    elimina emoji/punctuatie, prefixe decorative si separatori de tip
    " - ", " | ", ":" folositi frecvent in feed-uri.
    """
    text = normalize_text(title)
    text = re.sub(r"^[^a-z0-9]+", "", text)
    text = re.sub(r"\s*(?:\||-|:)\s+", " ", text)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\b(romania|romanian|video|foto|live|update|breaking)\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def parse_entry_datetime(entry: Dict[str, Any]) -> Optional[datetime]:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                return datetime.fromtimestamp(time.mktime(t), tz=timezone.utc)
            except Exception:
                pass
    return None

def parse_iso_datetime_safe(s: str) -> Optional[datetime]:
    raw = (s or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
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

def parse_iso_datetime(s: str) -> Optional[datetime]:
    return parse_iso_datetime_safe(s)

def load_published_state(path: str = PUBLISHED_STATE_PATH) -> Dict[str, Dict[str, str]]:
    raw = read_json(path)
    by_key = raw.get("by_key") if isinstance(raw, dict) else {}
    by_title = raw.get("by_title") if isinstance(raw, dict) else {}
    if not isinstance(by_key, dict):
        by_key = {}
    if not isinstance(by_title, dict):
        by_title = {}
    return {"by_key": by_key, "by_title": by_title}

def prune_published_state(state: Dict[str, Dict[str, str]], keep_days: int = 400, max_entries: int = 60000) -> Dict[str, Dict[str, str]]:
    now = datetime.now(timezone.utc)
    out = {"by_key": {}, "by_title": {}}
    for bucket in ("by_key", "by_title"):
        src = state.get(bucket) or {}
        if not isinstance(src, dict):
            continue
        kept: List[Tuple[datetime, str, str]] = []
        for k, iso in src.items():
            if not isinstance(k, str) or not isinstance(iso, str):
                continue
            dt = parse_iso_datetime(iso)
            if not dt:
                continue
            age = (now - dt).days
            if age > keep_days:
                continue
            kept.append((dt, k, iso))
        kept.sort(key=lambda x: x[0], reverse=True)
        for _dt, k, iso in kept[:max_entries]:
            out[bucket][k] = iso
    return out

def is_recently_published(state: Dict[str, Dict[str, str]], key: str, title_key: str, cooldown_days: int, now_utc: datetime) -> bool:
    if cooldown_days <= 0:
        return False
    latest: Optional[datetime] = None
    for bucket, k in (("by_key", key), ("by_title", title_key)):
        iso = ((state.get(bucket) or {}).get(k) or "").strip()
        dt = parse_iso_datetime(iso)
        if not dt:
            continue
        if latest is None or dt > latest:
            latest = dt
    if latest is None:
        return False
    return (now_utc - latest).days < cooldown_days

def mark_published(state: Dict[str, Dict[str, str]], key: str, title_key: str, when_iso: str) -> None:
    state.setdefault("by_key", {})[key] = when_iso
    state.setdefault("by_title", {})[title_key] = when_iso

def compute_freshness_boost(published: datetime, now_utc: datetime, cfg: Dict[str, Any]) -> int:
    fresh_cfg = cfg.get("freshness") if isinstance(cfg, dict) else {}
    if not isinstance(fresh_cfg, dict):
        fresh_cfg = {}
    enabled = bool(fresh_cfg.get("enabled", True))
    if not enabled:
        return 0
    hot_hours = int(fresh_cfg.get("hot_hours", 24))
    warm_hours = int(fresh_cfg.get("warm_hours", 72))
    hot_points = int(fresh_cfg.get("hot_points", 3))
    warm_points = int(fresh_cfg.get("warm_points", 1))
    age_h = max(0.0, (now_utc - published).total_seconds() / 3600.0)
    if age_h <= hot_hours:
        return hot_points
    if age_h <= warm_hours:
        return warm_points
    return 0

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

def extract_image_url(e: dict, allow_page_fetch: bool = False) -> Optional[str]:
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
    if allow_page_fetch and link:
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
        bad = _mk_norm_list([
            "office", "cowork", "workspace", "interior", "meeting room", "desk",
            "people", "person", "portrait", "human", "man", "woman", "child",
            "car", "vehicle", "truck", "motorcycle", "bike", "bicycle", "road", "street",
        ])
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
    "deficit", "datoria publica", "datoria publică", "buget", "taxe", "impozit", "fiscal",
    "contrabanda", "contrabandă", "tigarete de contrabanda", "tigarete de contrabandă",
    "fara canalizare", "fără canalizare", "asistenta sociala", "asistență socială", "invaliditate",
    "comisia europeana", "comisia europeană", "investigheaz", "investigatie", "investigație", "ajutor de stat",
    "petrol", "pretul petrolului", "prețul petrolului", "rezervele de urgenta", "rezervele de urgență",
    "sectorului 3", "negoita", "negoiță", "primaria condusa", "primăria condusă",
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

RAW_RO_LOW_SIGNAL_BLOCK = [
    "apocalips",
    "socant", "șocant",
    "incredibil",
    "versiunea platita", "versiunea plătită",
    "review ",
    "black friday",
    "cod promo",
    "sloturi", "casino", "cazinou", "pariuri",
    "horoscop",
    "test de cultura generala", "test de cultură generală",
    "boxe noi",
    "whatsapp plus",
    "dlss ",
    "rtx ",
    "sonos ",
    "program director",
    "communications",
    "communication director",
    "pr manager",
    "ceo build",
    "apel pentru depunerea candidaturilor",
    "pnrr",
    "oppo ",
    "find x",
    "implant cerebral",
    "beijingului", "beijing",
    "cultura generala", "cultură generală",
]
RO_LOW_SIGNAL_BLOCK = _mk_norm_list(RAW_RO_LOW_SIGNAL_BLOCK)

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
    "voluntar", "voluntari", "burse", "bursa", "olimpiada", "olimpiadă", "performanta", "performanță",
    "festival", "expozitie", "expoziție", "plantare", "impadurire", "împădurire", "renovare",
]
RO_POSITIVE_HINTS_STRICT = _mk_norm_list(RAW_RO_POSITIVE_HINTS_STRICT)

RAW_RO_POSITIVE_HINTS_RELAXED = [
    "poveste", "happy end", "iubire", "dragoste", "logod", "nunta",
    "animale", "caine", "câine", "pisica", "pisică", "adopt", "salvare",
    "educatie", "educație", "scoala", "școala", "elev", "student", "universitate",
    "cultura", "teatru", "film", "festival", "muzeu",
    "expozitie", "expoziție", "fotografie", "atelier", "centru cultural", "arta", "artiști", "artisti",
    "concert", "documentar", "bursieri", "tinere talente", "generatii", "generații",
    "sport", "victorie", "campion", "turneu",
    "startup", "inov", "tehnolog", "digital",
    "comunitate", "caritate", "strangere de fonduri", "strângere de fonduri",
    "seniori", "vârstnici", "varstnici", "solidaritate", "sprijiniti", "sprijiniți",
    "fundatie", "fundație", "centre sociale", "educatie intergenerationala", "educație intergenerațională",
    "participanti", "participanți", "eveniment", "editia", "ediția",
    "amuzant", "funny", "gluma", "glumă",
    "voluntar", "copii", "familie", "tabara", "tabără", "parc", "atelier", "biblioteca", "bibliotecă",
]
RO_POSITIVE_HINTS_RELAXED = _mk_norm_list(RAW_RO_POSITIVE_HINTS_RELAXED)
MAINSTREAM_RO_SOURCES = {"Digi24", "HotNews"}
CURATED_RO_SOURCES = {
    "Good News From Romania",
    "Start-Up Romania",
    "Green Start-Up Romania",
    "Mindcraft Stories",
    "Descopera (Stiinta)",
    "Life.ro (Lifestyle)",
    "B365 Lifestyle",
    "Turism Romania",
    "StiriPozitive - Toate",
    "StiriPozitive - Povesti de viata",
    "StiriPozitive - Animale",
    "StiriPozitive - Fapte bune",
    "StiriPozitive - Relatii",
    "StiriPozitive - Sanatate",
    "StiriPozitive - Educatie",
    "StiriPozitive - Comunitate",
    "StiriPozitive - Ecologie",
    "StiriPozitive - Cariera",
    "StiriPozitive - CSR",
    "StiriPozitive - Social",
    "StiriPozitive - Sport",
    "StiriPozitive - Timp liber",
    "StiriPozitive - Cultura",
    "RomaniaPozitiva",
    "RomaniaPozitiva - Sanatate",
    "RomaniaPozitiva - Educatie",
    "RomaniaPozitiva - Lifestyle",
    "Fundatia Regala Margareta",
    "Hope and Homes for Children Romania",
    "Fundatia Motivation Romania",
    "United Way Romania",
    "Asociatia Zi de BINE",
    "Fundatia Vodafone Romania",
    "Teach for Romania",
    "Fundatia Inocenti",
    "World Vision Romania",
    "SOS Satele Copiilor Romania",
}
SOURCE_NATIVE_POSITIVE_RO = {
    "StiriPozitive - Toate",
    "StiriPozitive - Povesti de viata",
    "StiriPozitive - Animale",
    "StiriPozitive - Fapte bune",
    "StiriPozitive - Relatii",
    "StiriPozitive - Sanatate",
    "StiriPozitive - Educatie",
    "StiriPozitive - Comunitate",
    "StiriPozitive - Ecologie",
    "StiriPozitive - Cariera",
    "StiriPozitive - CSR",
    "StiriPozitive - Social",
    "StiriPozitive - Sport",
    "StiriPozitive - Timp liber",
    "StiriPozitive - Cultura",
    "RomaniaPozitiva",
    "Fundatia Regala Margareta",
    "Hope and Homes for Children Romania",
    "Fundatia Motivation Romania",
    "United Way Romania",
    "Asociatia Zi de BINE",
    "Fundatia Vodafone Romania",
    "Teach for Romania",
    "Fundatia Inocenti",
    "World Vision Romania",
    "SOS Satele Copiilor Romania",
}
RO_MAINSTREAM_POSITIVE_GATE = _mk_norm_list([
    "salvat", "salvare", "eroi", "erou", "pompier", "smurd", "paramedic", "politist", "polițist",
    "medic", "operatie reusita", "operație reușită", "transplant reusit", "transplant reușit",
    "vindec", "recuperat", "adopt", "adoptie", "adopție",
    "premiu", "medalie", "record", "olimpiada", "olimpiadă",
    "inovatie", "inovație", "descoperire", "startup",
    "educatie", "educație", "profesor", "elev", "student",
    "amuzant", "umor", "satira", "satiră", "gluma", "glumă", "distractiv", "miracol",
    "salvamont", "interventie reusita", "intervenție reușită", "copil salvat", "persoana salvata", "persoană salvată",
])

def ro_hard_block(title: str, summary: str) -> bool:
    text = normalize_text(f"{title} {summary}")
    return any(kw in text for kw in RO_HARD_BLOCK if kw)

def ro_low_signal_block(title: str, summary: str, source_name: str = "") -> bool:
    text = normalize_text(f"{title} {summary}")
    source = normalize_text(source_name)
    if any(kw in text for kw in RO_LOW_SIGNAL_BLOCK if kw):
        return True
    if "cancero" in text and any(tag in source for tag in ("descopera", "life.ro", "b365")):
        return True
    if any(tag in source for tag in ("descopera", "start-up", "startup")):
        if any(kw in text for kw in _mk_norm_list([
            "test de cultura generala", "test de cultură generală",
            "nvidia", "dlss", "rtx", "sonos", "samsung",
            "boxe noi", "tehnologia siliciu carbon", "tehnologia siliciu-carbon",
        ])):
            return True
    # Blocheaza review-urile comerciale si "ce functii..." pe surse tech/lifestyle,
    # fara sa taiem stirile despre startup-uri sau inovatie reala.
    if any(tag in source for tag in ("start-up", "startup", "descopera", "life.ro", "b365")):
        if "review" in text:
            return True
        if "ce noi functii" in text or "ce noi funcții" in text:
            return True
        if any(brand in text for brand in ("iphone", "pixel", "sonos", "samsung", "whatsapp plus")):
            return True
    return False

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

def ro_curated_allow(title: str, summary: str) -> bool:
    if ro_hard_block(title, summary):
        return False
    return ro_positive_hits(title, summary, relaxed=True) >= 1

def ro_source_native_allow(title: str, summary: str) -> bool:
    if ro_hard_block(title, summary):
        return False
    text = normalize_text(f"{title} {summary}")
    blocked = _mk_norm_list([
        "angajam", "angajeaza", "angajează", "job", "joburi", "candidatur", "inscrieri", "înscrieri",
        "calendar", "webinar", "apel deschis", "apel pentru",
        "mihaela pene", "mihaela penes",
        "corporate fundraiser", "fundraiser", "ingrijitor", "îngrijitor", "pozitia oficiala", "poziția oficială",
    ])
    if any(kw in text for kw in blocked):
        return False
    return ro_positive_hits(title, summary, relaxed=True) >= 1

def satire_ro_allow(title: str, summary: str) -> bool:
    text = normalize_text(f"{title} {summary}")
    blocked = _mk_norm_list([
        "iran", "iranieni", "bombard", "rusia", "ucraina", "zelenski", "putin",
        "motorina", "motorină", "geografie",
    ])
    return not any(kw in text for kw in blocked)

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
    trusted_rescue = {"DSU Romania", "IGSU Romania", "Salvamont Romania", "Politia Romana"}
    if source_name in trusted_rescue:
        return 8
    if source_name in CURATED_RO_SOURCES:
        return 12
    if source_name in MAINSTREAM_RO_SOURCES:
        return 8
    return 10

FUNNY_HINTS = _mk_norm_list(["funny", "amuzant", "umor", "gluma", "glumă", "satira", "satiră", "distractiv"])
HEROIC_HINTS = _mk_norm_list([
    "salvat", "salvare", "politist", "polițist", "pompier", "medic", "smurd",
    "operatie reusita", "operație reușită", "miracol", "salvamont", "descarcerare",
    "interventie reusita", "intervenție reușită", "copil recuperat", "adoptie reusita", "adopție reușită",
])

def is_fun_or_hero_item(item: Dict[str, Any]) -> bool:
    text = normalize_text(f"{item.get('title','')} {item.get('summary','')} {item.get('source','')}")
    return any(kw in text for kw in FUNNY_HINTS + HEROIC_HINTS if kw)

def apply_fun_boost(items: List[Dict[str, Any]], top_k: int = 20, max_boost: int = 4, min_satire: int = 2) -> List[Dict[str, Any]]:
    if not items:
        return items
    boosted: List[Dict[str, Any]] = []
    seen = set()

    # 1) Collect satire candidates first (up to min_satire).
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

    # Base list without boosted items.
    out: List[Dict[str, Any]] = []
    for it in items:
        key = dedupe_key(it.get("link", ""), it.get("title", ""))
        if key in seen:
            continue
        out.append(it)

    if not boosted:
        return out

    # Place boosted items randomly in the first 10 slots (or less if list shorter).
    top_window = min(10, len(out) + len(boosted))
    random.shuffle(boosted)
    place_count = min(len(boosted), top_window)
    slots = set(random.sample(range(top_window), k=place_count)) if place_count > 0 else set()

    top: List[Dict[str, Any]] = []
    base_idx = 0
    boost_idx = 0
    for pos in range(top_window):
        if pos in slots and boost_idx < place_count:
            top.append(boosted[boost_idx])
            boost_idx += 1
            continue
        if base_idx < len(out):
            top.append(out[base_idx])
            base_idx += 1

    tail = out[base_idx:]
    remaining_boost = boosted[boost_idx:]
    return top + remaining_boost + tail

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
            t = re.sub(r"^\s*[-*•]+\s*", "", t)
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

def build_mix_items(
    sections: Dict[str, List[Dict[str, Any]]],
    ro_head: int = 10,
    max_items: int = 120,
    fun_boost_top_k: int = 20,
    fun_boost_max: int = 2,
    min_satire: int = 1,
) -> List[Dict[str, Any]]:
    ro = list(sections.get("romania") or [])
    en = list((sections.get("medical") or []) + (sections.get("science") or []) + (sections.get("environment") or []))

    def rank_key(it: Dict[str, Any]) -> Tuple[int, str]:
        fresh = int(it.get("freshness_boost", 0))
        return (fresh, it.get("published_utc", ""))
    ro.sort(key=rank_key, reverse=True)
    en.sort(key=rank_key, reverse=True)

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

    return apply_fun_boost(out, top_k=fun_boost_top_k, max_boost=fun_boost_max, min_satire=min_satire)

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
                "source": "Vesti Bune (Fallback)",
                "synthetic": True,
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
                "source": "Vesti Bune (Fallback)",
                "synthetic": True,
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
                "source": "Vesti Bune (Fallback)",
                "synthetic": True,
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
                "source": "Vesti Bune (Fallback)",
                "synthetic": True,
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
                "source": "Vesti Bune (Fallback)",
                "synthetic": True,
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
                "source": "Vesti Bune (Fallback)",
                "synthetic": True,
                "title": "Local conservation projects restore habitats for wildlife",
                "summary": "Community-led restoration efforts are improving biodiversity in several regions.",
                "link": "https://www.positive.news/environment/",
                "published_utc": now,
                "score": 2,
                "image": "https://upload.wikimedia.org/wikipedia/commons/3/3f/Fronalpstock_big.jpg",
            }
        ],
    }

def normalize_legacy_fallback_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    known_links = {
        "https://www.wwf.ro/ce_facem/paduri",
        "https://www.edu.ro",
        "https://insp.gov.ro",
        "https://www.sciencedaily.com/news/health_medicine",
        "https://www.nasa.gov/news/all-news",
        "https://www.positive.news/environment",
    }
    out: List[Dict[str, Any]] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        source = (it.get("source") or "").strip()
        link = canonicalize_url((it.get("link") or "").strip())
        if link in known_links:
            it = dict(it)
            it["source"] = "Vesti Bune (Fallback)"
            it["synthetic"] = True
        out.append(it)
    return out

def merge_sections_with_emergency(
    base_sections: Dict[str, List[Dict[str, Any]]],
    emergency_sections: Dict[str, List[Dict[str, Any]]],
    per_section: int = 2,
    max_items_map: Optional[Dict[str, int]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Injecteaza cateva iteme pozitive "fresh" in snapshotul anterior cand feed-urile
    nu aduc nimic nou mult timp. Pastreaza deduplicarea pe URL canonic + titlu.
    """
    out: Dict[str, List[Dict[str, Any]]] = {}
    max_items_map = max_items_map or {}
    section_ids = set((base_sections or {}).keys()) | set((emergency_sections or {}).keys())

    for sec_id in section_ids:
        base_items = list((base_sections or {}).get(sec_id) or [])
        fresh_candidates = list((emergency_sections or {}).get(sec_id) or [])[: max(0, per_section)]

        existing_keys = set(dedupe_key(it.get("link", ""), it.get("title", "")) for it in base_items)
        existing_titles = set(normalized_title_key(it.get("title", "")) for it in base_items)

        injected: List[Dict[str, Any]] = []
        for it in fresh_candidates:
            k = dedupe_key(it.get("link", ""), it.get("title", ""))
            t = normalized_title_key(it.get("title", ""))
            if k in existing_keys or t in existing_titles:
                continue
            injected.append(it)
            existing_keys.add(k)
            existing_titles.add(t)

        merged = injected + base_items
        limit = int(max_items_map.get(sec_id, len(merged)))
        out[sec_id] = merged[:limit]
    return out

def has_fresh_items(items: List[Dict[str, Any]], now_utc: datetime, within_hours: int = 24) -> bool:
    for it in items or []:
        dt = parse_iso_datetime_safe((it.get("published_utc") or "").strip())
        if not dt:
            continue
        if (now_utc - dt).total_seconds() <= max(1, within_hours) * 3600:
            return True
    return False

# -----------------------------
# Build sections
# -----------------------------
def build_sections(
    cfg: Dict[str, Any],
    published_state: Optional[Dict[str, Dict[str, str]]] = None,
    publish_cooldown_override: Optional[int] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    rss_sources = cfg.get("rss_sources") or {}
    sections_def = cfg.get("sections") or []
    filters_cfg = cfg.get("filters") or {}

    thresholds = (filters_cfg.get("thresholds") or {})
    ro_min_items = int(filters_cfg.get("ro_min_items", 16))
    ro_try_relaxed = bool(filters_cfg.get("ro_try_relaxed", True))
    ro_max_age_days = int(filters_cfg.get("ro_max_age_days", 45))
    publish_cooldown_days = int(
        publish_cooldown_override
        if publish_cooldown_override is not None
        else filters_cfg.get("publish_cooldown_days", 30)
    )
    now_utc = datetime.now(timezone.utc)
    published_state = published_state or {"by_key": {}, "by_title": {}}

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
                satire_source = is_satire_source(name, url) if kind == "ro" else False

                if kind == "ro":
                    if satire_source:
                        # Satire is intentionally allowed to keep variety in mix.
                        if not satire_ro_allow(title, summary):
                            continue
                        score = 3
                    else:
                        if ro_low_signal_block(title, summary, name):
                            continue
                        if name in {"DSU Romania", "IGSU Romania", "Salvamont Romania", "Politia Romana"} and not ro_mainstream_allow(title, summary):
                            continue
                        strict_hits = ro_positive_hits(title, summary, relaxed=False)
                        if name in MAINSTREAM_RO_SOURCES and strict_hits < 1 and not ro_mainstream_allow(title, summary):
                            continue
                        if ro_max_age_days > 0:
                            age_days = (now_utc - published).days
                            if age_days > ro_max_age_days:
                                continue
                        curated_relaxed_ok = name in CURATED_RO_SOURCES and ro_curated_allow(title, summary)
                        source_native_ok = name in SOURCE_NATIVE_POSITIVE_RO and ro_source_native_allow(title, summary)
                        if not ro_allow(title, summary, relaxed=False) and not curated_relaxed_ok and not source_native_ok:
                            if ro_try_relaxed and not ro_hard_block(title, summary):
                                relaxed_hits = ro_positive_hits(title, summary, relaxed=True)
                                if name in MAINSTREAM_RO_SOURCES:
                                    if relaxed_hits < 2 or not ro_mainstream_allow(title, summary):
                                        continue
                                elif name in CURATED_RO_SOURCES and relaxed_hits < 1:
                                    continue
                                ro_candidates.append({
                                    "section": section_id,
                                    "kind": "ro",
                                    "source": name,
                                    "title": title,
                                    "summary": summary,
                                    "link": link,
                                    "published_utc": published.isoformat(),
                                    "score": 2 if name in MAINSTREAM_RO_SOURCES else 1,
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
                title_key = normalized_title_key(title)
                if title_key in seen_titles:
                    continue
                cooldown_exempt = satire_source or (kind == "ro" and name in SOURCE_NATIVE_POSITIVE_RO)
                if (not cooldown_exempt) and is_recently_published(published_state, key, title_key, publish_cooldown_days, now_utc):
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
                    "freshness_boost": compute_freshness_boost(published, now_utc, filters_cfg),
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
                mark_published(published_state, key, title_key, now_utc.replace(microsecond=0).isoformat())
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
        already_titles = set(normalized_title_key(it["title"]) for it in out["romania"])
        added = 0
        for c in relaxed_ok:
            if added >= needed:
                break
            k = dedupe_key(c["link"], c["title"])
            title_key = normalized_title_key(c["title"])
            if k in already:
                continue
            if title_key in already_titles:
                continue
            already.add(k)
            already_titles.add(title_key)

            c["image"] = fallback_image_url(c["section"], c["title"], c["link"])

            out["romania"].append(c)
            mark_published(published_state, k, title_key, now_utc.replace(microsecond=0).isoformat())
            added += 1

        out["romania"].sort(key=lambda x: x.get("published_utc", ""), reverse=True)
        out["romania"] = out["romania"][: max_items_map.get("romania", 40)]

    return out

# -----------------------------
# Main
# -----------------------------
def main() -> None:
    cfg = load_yaml(CONFIG_PATH)
    published_state = load_published_state(PUBLISHED_STATE_PATH)
    sections = build_sections(cfg, published_state=published_state)
    total_items = sum(len(v or []) for v in (sections or {}).values())
    if total_items == 0:
        retry_state = load_published_state(PUBLISHED_STATE_PATH)
        sections = build_sections(cfg, published_state=retry_state, publish_cooldown_override=0)
        retry_total = sum(len(v or []) for v in (sections or {}).values())
        if retry_total > 0:
            published_state = retry_state
            total_items = retry_total
            print("[WARN] publish cooldown blocked all items; retried refresh with cooldown disabled")
    filters_cfg = cfg.get("filters") or {}
    sections_def = cfg.get("sections") or []
    max_items_map = {s.get("id"): int(s.get("max_items", 20)) for s in sections_def if isinstance(s, dict) and s.get("id")}
    mix_cfg = filters_cfg.get("mix") if isinstance(filters_cfg, dict) else {}
    if not isinstance(mix_cfg, dict):
        mix_cfg = {}
    mix_ro_head = int(mix_cfg.get("ro_head", 10))
    mix_max_items = int(mix_cfg.get("max_items", 120))
    mix_fun_top_k = int(mix_cfg.get("fun_boost_top_k", 20))
    mix_fun_max = int(mix_cfg.get("fun_boost_max", 2))
    mix_min_satire = int(mix_cfg.get("min_satire", 1))

    prev = read_json(OUT_NEWS)
    prev_sections = prev.get("sections") if isinstance(prev, dict) else {}
    if isinstance(prev_sections, dict):
        prev_sections = {k: normalize_legacy_fallback_items(list(v or [])) for k, v in prev_sections.items()}
    prev_mix_items = normalize_legacy_fallback_items(list(prev.get("mix_items") or [])) if isinstance(prev, dict) else []
    prev_total = sum(len(v or []) for v in (prev_sections or {}).values()) if isinstance(prev_sections, dict) else 0

    effective_sections = sections
    effective_mix = build_mix_items(
        sections,
        ro_head=mix_ro_head,
        max_items=mix_max_items,
        fun_boost_top_k=mix_fun_top_k,
        fun_boost_max=mix_fun_max,
        min_satire=mix_min_satire,
    )
    if total_items == 0 and prev_total > 0:
        effective_sections = prev_sections
        effective_mix = prev_mix_items
        print("[WARN] current refresh returned 0 items; keeping previous news sections")
        stale_refresh_hours = int(filters_cfg.get("stale_refresh_hours", 48))
        stale_emergency_per_section = int(filters_cfg.get("stale_emergency_per_section", 2))
        now_utc = datetime.now(timezone.utc)
        prev_generated = parse_iso_datetime_safe(prev.get("generated_utc", "")) if isinstance(prev, dict) else None
        stale_by_generated = False
        if prev_generated:
            age_h = (now_utc - prev_generated).total_seconds() / 3600.0
            stale_by_generated = age_h >= max(1, stale_refresh_hours)
        stale_by_content = not has_fresh_items(effective_mix if isinstance(effective_mix, list) else [], now_utc, within_hours=24)
        if stale_by_generated or stale_by_content:
            emergency_sections = build_emergency_sections()
            effective_sections = merge_sections_with_emergency(
                effective_sections if isinstance(effective_sections, dict) else {},
                emergency_sections,
                per_section=max(1, stale_emergency_per_section),
                max_items_map=max_items_map,
            )
            effective_mix = build_mix_items(
                effective_sections,
                ro_head=mix_ro_head,
                max_items=mix_max_items,
                fun_boost_top_k=mix_fun_top_k,
                fun_boost_max=mix_fun_max,
                min_satire=mix_min_satire,
            )
            reason = "generated-age" if stale_by_generated else "stale-content"
            print(f"[WARN] stale snapshot detected ({reason}); injected emergency fresh positives")
    elif total_items == 0:
        effective_sections = build_emergency_sections()
        effective_mix = build_mix_items(
            effective_sections,
            ro_head=mix_ro_head,
            max_items=mix_max_items,
            fun_boost_top_k=mix_fun_top_k,
            fun_boost_max=mix_fun_max,
            min_satire=mix_min_satire,
        )
        print("[WARN] current refresh returned 0 items; using emergency positive fallback")
    elif isinstance(prev_sections, dict):
        # If one refresh partially fails (e.g., only RO survives), keep previous section
        # content for emptied sections so EN buckets don't disappear from the site.
        patched = False
        for sec_id in ("medical", "science", "environment"):
            cur_len = len((effective_sections.get(sec_id) or []))
            prev_len = len((prev_sections.get(sec_id) or []))
            if cur_len == 0 and prev_len > 0:
                effective_sections[sec_id] = list(prev_sections.get(sec_id) or [])
                patched = True
        if patched:
            effective_mix = build_mix_items(
                effective_sections,
                ro_head=mix_ro_head,
                max_items=mix_max_items,
                fun_boost_top_k=mix_fun_top_k,
                fun_boost_max=mix_fun_max,
                min_satire=mix_min_satire,
            )
            print("[WARN] partial refresh detected; restored previous EN sections")
        # Guard against gradual EN erosion across multiple partial runs.
        min_en_floor = {"medical": 8, "science": 8, "environment": 8}
        floor_patched = False
        for sec_id, floor in min_en_floor.items():
            cur_items = list(effective_sections.get(sec_id) or [])
            prev_items = list(prev_sections.get(sec_id) or [])
            if len(cur_items) < floor and len(prev_items) > len(cur_items):
                effective_sections[sec_id] = prev_items
                floor_patched = True
        if floor_patched:
            effective_mix = build_mix_items(
                effective_sections,
                ro_head=mix_ro_head,
                max_items=mix_max_items,
                fun_boost_top_k=mix_fun_top_k,
                fun_boost_max=mix_fun_max,
                min_satire=mix_min_satire,
            )
            print("[WARN] EN floor restore applied from previous snapshot")

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
    write_json(PUBLISHED_STATE_PATH, prune_published_state(published_state))
    write_json(OUT_NEWS, payload)
    write_json(OUT_ITEMS, payload)
    print("[OK] wrote data/news.json and data/items.json")

if __name__ == "__main__":
    main()
