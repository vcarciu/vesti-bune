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

USER_AGENT = "vesti-bune-bot/strict-ro (+https://vcarciu.github.io/vesti-bune/)"

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def safe_mkdir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def load_yaml(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

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

def fetch_url_with_final(url: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20, allow_redirects=True)
        if r.status_code == 200:
            return r.text, r.url
    except Exception:
        return None, None
    return None, None

def deepl_translate(text: str, target_lang: str = "RO") -> Optional[str]:
    key = os.getenv("DEEPL_API_KEY", "").strip()
    if not key or not text.strip():
        return None

    url_env = os.getenv("DEEPL_API_URL", "").strip()
    candidates = [url_env] if url_env else [
        "https://api-free.deepl.com/v2/translate",
        "https://api.deepl.com/v2/translate",
    ]

    headers = {"User-Agent": USER_AGENT}
    data = {"auth_key": key, "text": text, "target_lang": target_lang}

    for url in [u for u in candidates if u]:
        try:
            r = requests.post(url, data=data, headers=headers, timeout=20)
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
# STRICT RO filtering
# -----------------------------
RAW_RO_HARD_BLOCK = [
    "minim istoric", "record negativ",
    "crestere alarmanta", "creștere alarmantă", "alarmant",
    "explozie a cazurilor", "val de",
    "cazuri de", "focar", "epidem", "alerta", "alertă", "criza", "criză",
    "infect", "imbulnav", "îmbolnăv", "spitaliz", "decese", "mort", "morti", "morți", "victime", "ranit", "răni",
    "crim", "ucis", "omor", "viol", "agresi", "talhar", "jaf", "furt", "drog", "arest", "retinut", "perchez", "anchet", "dosar", "dna", "diicot",
    "accident", "traged", "dezastru", "exploz", "incend", "cutremur", "inunda",
    "politic", "guvern", "parlament", "aleger", "ministr", "premier", "presed",
    "psd", "pnl", "aur", "udmr",
    "scumpir", "infl", "reces", "faliment", "insolvent", "concedier", "somaj", "șomaj",
    "nu o sa-ti vina sa crezi", "nu o să-ți vină să crezi",
    "halucinant", "ingrozitor", "îngrozitor", "cosmar", "coșmar", "de groaza", "de groază",
]
RO_HARD_BLOCK = [normalize_text(x) for x in RAW_RO_HARD_BLOCK]

RAW_RO_POSITIVE_HINTS = [
    "premiu", "a castigat", "a câștigat", "medalie", "record",
    "inaugur", "s-a deschis", "finalizat", "modernizat",
    "invest", "finant", "grant", "bursa", "bursă", "program",
    "tratament nou", "terapie nou", "aprobat", "screening gratuit", "gratuit",
    "salvat", "voluntar", "donat", "campanie", "reabilitat",
]
RO_POSITIVE_HINTS = [normalize_text(x) for x in RAW_RO_POSITIVE_HINTS]

def ro_allow(title: str, summary: str) -> bool:
    text = normalize_text(f"{title} {summary}")
    for kw in RO_HARD_BLOCK:
        if kw and kw in text:
            return False
    for kw in RO_POSITIVE_HINTS:
        if kw and kw in text:
            return True
    return False

# -----------------------------
# Global scoring (light)
# -----------------------------
RAW_NEGATIVE_KEYWORDS = [
    "war", "attack", "killed", "dead", "deaths", "shooting", "murder",
    "explosion", "earthquake", "flood", "wildfire", "crisis", "recession", "bankrupt",
    "terror", "hostage",
]
RAW_POSITIVE_STRONG = [
    "breakthrough", "wins", "award", "discover", "new treatment", "approved", "cure",
    "saved", "record",
]
NEGATIVE_KEYWORDS = [normalize_text(k) for k in RAW_NEGATIVE_KEYWORDS]
POSITIVE_STRONG = [normalize_text(k) for k in RAW_POSITIVE_STRONG]

def score_global(title: str, summary: str) -> int:
    text = normalize_text(f"{title} {summary}")
    for kw in NEGATIVE_KEYWORDS:
        if kw and kw in text:
            return -999
    score = 0
    for kw in POSITIVE_STRONG:
        if kw and kw in text:
            score += 2
    return score

def fetch_rss(url: str) -> feedparser.FeedParserDict:
    return feedparser.parse(url, agent=USER_AGENT)

def dedupe_key(link: str, title: str) -> str:
    base = (link or "") + "||" + (title or "")
    return hashlib.sha1(base.encode("utf-8", errors="ignore")).hexdigest()

def extract_og_image(html: str) -> Optional[str]:
    if not html:
        return None
    m = re.search(r'property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html, flags=re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r'content=["\']([^"\']+)["\']\s+property=["\']og:image["\']', html, flags=re.I)
    if m:
        return m.group(1).strip()
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
        m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html, flags=re.I)
        if m:
            return m.group(1).strip()

    link = (e.get("link") or "").strip()
    if link:
        page_html, _final = fetch_url_with_final(link)
        img = extract_og_image(page_html or "")
        if img:
            return img.strip()
    return None

def build_sections(cfg: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    rss_sources = cfg.get("rss_sources") or {}
    sections_def = cfg.get("sections") or []
    filters_cfg = cfg.get("filters") or {}
    thresholds = (filters_cfg.get("thresholds") or {})
    max_items_map = {s["id"]: int(s.get("max_items", 20)) for s in sections_def if "id" in s}

    out: Dict[str, List[Dict[str, Any]]] = {}
    seen: set = set()

    for section_id, sources in rss_sources.items():
        items: List[Dict[str, Any]] = []
        for src in sources:
            name = src.get("name", section_id)
            url = (src.get("url") or "").strip()
            if not url:
                continue

            feed = fetch_rss(url)
            for e in (feed.entries or [])[:60]:
                title = (e.get("title") or "").strip()
                link = (e.get("link") or "").strip()
                summary = get_entry_summary(e)
                if not title or not link:
                    continue

                dt = parse_entry_datetime(e)
                published = (dt or datetime.now(timezone.utc)).replace(microsecond=0)

                kind = "ro" if section_id == "romania" else "global"
                if kind == "ro":
                    if not ro_allow(title, summary):
                        continue
                    score = 1
                else:
                    score = score_global(title, summary)
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

    return out

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
        ("space", "https://www.flickr.com/services/feeds/photos_public.gne?format=rss2&tags=space"),
        ("animals", "https://www.flickr.com/services/feeds/photos_public.gne?format=rss2&tags=animals"),
        ("landscape", "https://www.flickr.com/services/feeds/photos_public.gne?format=rss2&tags=landscape"),
    ]
    out: List[Dict[str, Any]] = []
    for tag, url in feeds:
        try:
            f = fetch_rss(url)
            if not f.entries:
                continue
            e = random.choice(f.entries[:20])
            title = (e.get("title") or "").strip()
            link = (e.get("link") or "").strip()
            img = extract_image_url(e) or None
            if img:
                out.append({"tag": tag, "title": title, "link": link, "image": img})
        except Exception:
            continue
    return out[:limit]

def write_json(path: str, payload: Dict[str, Any]) -> None:
    safe_mkdir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

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
