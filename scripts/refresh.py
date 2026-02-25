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

import requests
import feedparser
import yaml

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CONFIG_PATH = os.path.join(ROOT_DIR, "config", "sources.yml")

OUT_NEWS = os.path.join(ROOT_DIR, "data", "news.json")
OUT_ITEMS = os.path.join(ROOT_DIR, "data", "items.json")
JOKES_PATH = os.path.join(ROOT_DIR, "data", "jokes_ro.txt")

USER_AGENT = "vesti-bune-bot (+https://vcarciu.github.io/vesti-bune/)"

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

def write_json(path: str, payload: Dict[str, Any]) -> None:
    safe_mkdir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

def dedupe_key(link: str, title: str) -> str:
    base = (link or "") + "||" + (title or "")
    return hashlib.sha1(base.encode("utf-8", errors="ignore")).hexdigest()


# -----------------------------
# HTTP / RSS
# -----------------------------
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})

def fetch_url_with_final(url: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        r = SESSION.get(url, timeout=25, allow_redirects=True)
        if r.status_code == 200 and r.text:
            return r.text, r.url
    except Exception:
        return None, None
    return None, None

def fetch_rss(url: str) -> feedparser.FeedParserDict:
    """
    feedparser.parse(url) uneori e instabil (timeout/UA).
    Folosim requests + feedparser.parse(bytes) pentru control.
    """
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

    text = (text or "").strip()
    # Nu exagerăm cu costul: limităm un pic input-ul
    if len(text) > 800:
        text = text[:800]

    url_env = os.getenv("DEEPL_API_URL", "").strip()
    candidates = [url_env] if url_env else [
        "https://api-free.deepl.com/v2/translate",
        "https://api.deepl.com/v2/translate",
    ]

    headers = {"User-Agent": USER_AGENT}
    data = {"auth_key": key, "text": text, "target_lang": target_lang}

    for url in [u for u in candidates if u]:
        try:
            r = requests.post(url, data=data, headers=headers, timeout=25)
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
# Image extraction (RSS + og:image)
# -----------------------------
OG_IMAGE_RE_1 = re.compile(r'property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', re.I)
OG_IMAGE_RE_2 = re.compile(r'content=["\']([^"\']+)["\']\s+property=["\']og:image["\']', re.I)
IMG_SRC_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)

def extract_og_image(html: str) -> Optional[str]:
    if not html:
        return None
    m = OG_IMAGE_RE_1.search(html) or OG_IMAGE_RE_2.search(html)
    if m:
        return (m.group(1) or "").strip() or None
    return None

def extract_image_url(entry: dict) -> Optional[str]:
    # media:content
    mc = entry.get("media_content")
    if isinstance(mc, list) and mc:
        for item in mc:
            url = (item.get("url") or "").strip()
            if url:
                return url

    # media:thumbnail
    mt = entry.get("media_thumbnail")
    if isinstance(mt, list) and mt:
        for item in mt:
            url = (item.get("url") or "").strip()
            if url:
                return url

    # enclosure
    links = entry.get("links")
    if isinstance(links, list):
        for l in links:
            rel = (l.get("rel") or "").lower()
            href = (l.get("href") or "").strip()
            if rel == "enclosure" and href:
                return href

    # <img> in summary/content
    html = entry.get("summary") or ""
    if isinstance(html, str) and html:
        m = IMG_SRC_RE.search(html)
        if m:
            return (m.group(1) or "").strip() or None

    # og:image from article page
    link = (entry.get("link") or "").strip()
    if link:
        page_html, _final = fetch_url_with_final(link)
        img = extract_og_image(page_html or "")
        if img:
            return img.strip()
    return None


# -----------------------------
# Filtering & scoring
# -----------------------------
def _mk_norm_list(words: List[str]) -> List[str]:
    return [normalize_text(w) for w in words if (w or "").strip()]

# Hard-block (alarmism / crime / politics / disasters) - applied to ALL
RAW_HARD_BLOCK_COMMON = [
    # RO/EN mixed (normalize_text removes diacritics)
    "alerta", "alert", "alarm", "alarmant", "breaking", "soc", "șoc", "groaza", "groază", "panica", "panic",
    "traged", "dezastru", "catastrof", "criza", "crisis", "exploz", "explosion", "incend", "fire", "wildfire",
    "cutremur", "earthquake", "inunda", "flood",
    "razboi", "război", "war", "attack", "atac", "bomb", "teror", "terror",
    "killed", "dead", "deaths", "murder", "shooting",
    "crima", "crim", "ucis", "omor", "viol", "agresi", "drog", "arest", "retinut", "anchet", "dosar",
    # politics (you can relax later if you want, but for “vești bune” it’s usually noise)
    "guvern", "parlament", "aleger", "ministr", "premier", "presed", "psd", "pnl", "aur", "udmr",
    "scandal",
    # health panic words
    "epidem", "focar", "explozie a cazurilor", "crestere alarmanta", "minim istoric", "record negativ",
]
HARD_BLOCK_COMMON = _mk_norm_list(RAW_HARD_BLOCK_COMMON)

# RO: strict positive hints
RAW_RO_POSITIVE_STRICT = [
    "premiu", "a castigat", "medalie", "record", "inaugur", "s-a deschis", "finalizat", "modernizat",
    "invest", "finant", "grant", "fonduri", "proiect", "program",
    "tratament nou", "terapie nou", "aprobat", "screening gratuit", "gratuit",
    "salvat", "voluntar", "donat", "campanie", "reabilitat",
    "a scazut", "scade", "reducere", "mai putin", "imbunatat", "îmbunatat",
]
RO_POSITIVE_STRICT = _mk_norm_list(RAW_RO_POSITIVE_STRICT)

# RO: relaxed constructive (still good vibe, more volume)
RAW_RO_POSITIVE_RELAX = [
    "educatie", "scoala", "elev", "student", "universitate", "cercet", "inov",
    "cultura", "teatru", "film", "festival", "muzeu",
    "sport", "victorie", "campion", "meci", "turneu",
    "startup", "tehnolog", "digital", "aplicatie",
    "comunitate", "volunt", "caritate", "don", "campanie",
    "spital", "clinica", "sanatate", "sănătate",
    "infrastruct", "moderniz", "transport", "tramvai", "metrou",
]
RO_POSITIVE_RELAX = _mk_norm_list(RAW_RO_POSITIVE_RELAX)

# EN progress/solutions keywords (weighted)
RAW_GLOBAL_POSITIVE = [
    "breakthrough", "promising", "improves", "improved", "improvement",
    "reduces risk", "reduced risk", "reduction", "effective", "success", "successful",
    "approved", "approval", "new treatment", "new therapy", "new method", "new approach",
    "discovery", "discover", "restores", "recovery", "safer",
    "clean energy", "renewable", "reforestation", "conservation", "cleanup", "restoration",
    "protects", "protected", "saved", "saves", "award", "wins",
]
GLOBAL_POSITIVE = _mk_norm_list(RAW_GLOBAL_POSITIVE)

# EN/Global soft-negative words (not always fatal, but we downscore)
RAW_GLOBAL_SOFT_NEG = [
    "risk", "disease", "outbreak", "shortage", "pollution", "emissions",
    "decline", "threat", "crisis", "danger", "deadly", "fatal",
]
GLOBAL_SOFT_NEG = _mk_norm_list(RAW_GLOBAL_SOFT_NEG)

def hard_block(title: str, summary: str) -> bool:
    """
    True => must drop
    """
    text = normalize_text(f"{title} {summary}")
    for kw in HARD_BLOCK_COMMON:
        if kw and kw in text:
            return True
    return False

def ro_allow(title: str, summary: str, relaxed: bool = False) -> bool:
    """
    RO gate:
    - always hard-block common alarmism/politics/crime
    - strict mode: must contain a strict positive hint
    - relaxed mode: allow constructive topics too
    """
    if hard_block(title, summary):
        return False

    text = normalize_text(f"{title} {summary}")

    # strict positive required unless relaxed
    for kw in RO_POSITIVE_STRICT:
        if kw and kw in text:
            return True

    if not relaxed:
        return False

    # relaxed constructive topics
    for kw in RO_POSITIVE_RELAX:
        if kw and kw in text:
            return True

    return False

def score_global(section_id: str, title: str, summary: str) -> int:
    """
    Global score per section.
    Hard-block kills immediately.
    Otherwise:
      + points for positive/progress keywords
      - points for soft-negative keywords
    Threshold per section is applied later from config.
    """
    if hard_block(title, summary):
        return -999

    text = normalize_text(f"{title} {summary}")

    score = 0
    for kw in GLOBAL_POSITIVE:
        if kw and kw in text:
            score += 2

    # section-specific tiny boosts
    if section_id in ("medical", "science"):
        # Research-ish boosters
        for kw in _mk_norm_list(["study finds", "trial", "peer-reviewed", "researchers", "clinical"]):
            if kw and kw in text:
                score += 1

    if section_id in ("environment",):
        for kw in _mk_norm_list(["renewable", "solar", "wind", "reforestation", "conservation", "restoration", "protected area"]):
            if kw and kw in text:
                score += 1

    # soft negatives: subtract but do NOT kill
    for kw in GLOBAL_SOFT_NEG:
        if kw and kw in text:
            score -= 1

    return score


# -----------------------------
# Builder
# -----------------------------
def build_sections(cfg: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    rss_sources = cfg.get("rss_sources") or {}
    sections_def = cfg.get("sections") or []
    filters_cfg = cfg.get("filters") or {}

    thresholds = (filters_cfg.get("thresholds") or {})
    # Optional: target minimum RO items before we relax
    ro_min_items = int(filters_cfg.get("ro_min_items", 10))  # default 10
    ro_try_relaxed = bool(filters_cfg.get("ro_try_relaxed", True))

    max_items_map = {s["id"]: int(s.get("max_items", 20)) for s in sections_def if "id" in s}

    out: Dict[str, List[Dict[str, Any]]] = {}
    seen: set = set()

    # First pass strict
    candidate_bank: Dict[str, List[Dict[str, Any]]] = {sid: [] for sid in rss_sources.keys()}

    for section_id, sources in rss_sources.items():
        items: List[Dict[str, Any]] = []

        for src in sources:
            name = src.get("name", section_id)
            url = (src.get("url") or "").strip()
            if not url:
                continue

            feed = fetch_rss(url)
            entries = (feed.entries or [])[:80]  # take more, we filter later

            for e in entries:
                title = (e.get("title") or "").strip()
                link = (e.get("link") or "").strip()
                if not title or not link:
                    continue

                summary = get_entry_summary(e)
                dt = parse_entry_datetime(e)
                published = (dt or datetime.now(timezone.utc)).replace(microsecond=0)

                kind = "ro" if section_id == "romania" else "global"

                if kind == "ro":
                    if not ro_allow(title, summary, relaxed=False):
                        # Keep for possible relaxed second pass
                        if ro_try_relaxed and not hard_block(title, summary):
                            candidate_bank[section_id].append({
                                "section": section_id,
                                "kind": kind,
                                "source": name,
                                "title": title,
                                "summary": summary,
                                "link": link,
                                "published_utc": published.isoformat(),
                            })
                        continue
                    score = 3  # strict RO pass => high confidence
                else:
                    score = score_global(section_id, title, summary)
                    if score < 0:
                        continue

                thr = int(thresholds.get(section_id, 0))
                if score < thr:
                    continue

                key = dedupe_key(link, title)
                if key in seen:
                    continue
                seen.add(key)

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

                img = extract_image_url(e)
                if img:
                    item["image"] = img

                # Optional translate (title+summary)
                if kind == "global":
                    tr_title = deepl_translate(title) or None
                    tr_sum = deepl_translate(summary) or None
                    if tr_title:
                        item["title_ro"] = tr_title
                    if tr_sum:
                        item["summary_ro"] = tr_sum

                items.append(item)

        items.sort(key=lambda x: x.get("published_utc", ""), reverse=True)
        items = items[: max_items_map.get(section_id, 20)]
        out[section_id] = items

    # Second pass: relax RO if too few
    if "romania" in out and ro_try_relaxed and len(out["romania"]) < ro_min_items:
        needed = ro_min_items - len(out["romania"])
        relaxed_candidates = []

        # Re-check candidates with relaxed allow (still no alarmism/politics/crime)
        for c in candidate_bank.get("romania", []):
            if ro_allow(c["title"], c["summary"], relaxed=True):
                relaxed_candidates.append(c)

        # Deduplicate vs already selected
        already = set(dedupe_key(it["link"], it["title"]) for it in out["romania"])
        add = []
        for c in sorted(relaxed_candidates, key=lambda x: x.get("published_utc", ""), reverse=True):
            k = dedupe_key(c["link"], c["title"])
            if k in already:
                continue
            already.add(k)

            # Build full item with score and image
            item = dict(c)
            item["score"] = 1  # relaxed RO score lower

            # Try image now (requires entry, but we only have link/title/summary);
            # we'll fetch og:image as a fallback.
            if item.get("link"):
                html, _final = fetch_url_with_final(item["link"])
                img = extract_og_image(html or "")
                if img:
                    item["image"] = img

            add.append(item)
            if len(add) >= needed:
                break

        out["romania"].extend(add)
        out["romania"].sort(key=lambda x: x.get("published_utc", ""), reverse=True)
        out["romania"] = out["romania"][: max_items_map.get("romania", 35)]

    return out


# -----------------------------
# Joke + top images
# -----------------------------
def build_joke() -> Optional[Dict[str, Any]]:
    if not os.path.exists(JOKES_PATH):
        return None
    with open(JOKES_PATH, "r", encoding="utf-8") as f:
        lines = [ln.strip() for ln in f.readlines()]
    jokes = [j for j in lines if j and not j.startswith("#")]
    if not jokes:
        return None

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    idx = int(hashlib.sha1(day.encode("utf-8")).hexdigest(), 16) % len(jokes)
    return {"date_utc": day, "text": jokes[idx]}

def pick_flickr_images(limit: int = 3) -> List[Dict[str, Any]]:
    feeds = [
        ("space", "https://www.flickr.com/services/feeds/photos_public.gne?format=rss2&tags=space,nebula,galaxy&tagmode=any"),
        ("animals", "https://www.flickr.com/services/feeds/photos_public.gne?format=rss2&tags=animals,dog,cat,wildlife&tagmode=any"),
        ("landscape", "https://www.flickr.com/services/feeds/photos_public.gne?format=rss2&tags=landscape,mountains,lake,sunset&tagmode=any"),
    ]

    out: List[Dict[str, Any]] = []
    for tag, url in feeds:
        try:
            f = fetch_rss(url)
            if not f.entries:
                continue
            e = random.choice(f.entries[:25])
            title = (e.get("title") or "").strip()
            link = (e.get("link") or "").strip()
            img = extract_image_url(e) or None
            if img:
                out.append({"tag": tag, "title": title, "link": link, "image": img})
        except Exception:
            continue
    return out[:limit]


# -----------------------------
# Main
# -----------------------------
def main() -> None:
    cfg = load_yaml(CONFIG_PATH)

    payload: Dict[str, Any] = {
        "generated_utc": utc_now_iso(),
        "sections": build_sections(cfg),
        "joke_ro": build_joke(),
        "top_images": pick_flickr_images(limit=3),
    }

    write_json(OUT_NEWS, payload)
    write_json(OUT_ITEMS, payload)
    print("[OK] wrote data/news.json and data/items.json")

if __name__ == "__main__":
    main()
