# scripts/refresh.py
import random
import os
import re
import json
import time
import hashlib
import unicodedata
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

import requests
import feedparser
import yaml


ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CONFIG_PATH = os.path.join(ROOT_DIR, "config", "sources.yml")
OUT_PATH = os.path.join(ROOT_DIR, "data", "news.json")
JOKES_PATH = os.path.join(ROOT_DIR, "data", "jokes_ro.txt")

USER_AGENT = "vesti-bune-bot/1.0 (+https://vcarciu.github.io/vesti-bune/)"


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
    if "summary" in entry and entry["summary"]:
        return strip_html(entry["summary"])
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
                    out = tr[0].get("text", "").strip()
                    return out or None
        except Exception:
            continue
    return None


# --- Keywords (normalize once) -----------------------------------------------
RAW_NEGATIVE_KEYWORDS = [
    # conflict / război (folosim rădăcini)
    "razbo", "invaz", "atac", "bombard", "rachet", "dron", "front", "armat", "soldat",
    "nato", "ucrain", "rus", "moscov", "kiev", "zelensk", "putin",
    "israel", "gaza", "palestin", "hamas", "iran", "siria", "yemen",

    # crimă / violență / ordine publică
    "crim", "omor", "ucis", "injungh", "impusc", "violent", "agresi", "viol", "rapit",
    "talhar", "jaf", "furt", "drog", "arest", "retinut", "perchez", "anchet",
    "procuror", "parchet", "dna", "diicot", "dosar", "inculpat",
    "trimis in judecata", "condamnat", "sentin",

    # accidente / dezastre
    "accident", "colizi", "exploz", "incend", "inunda", "cutremur", "dezastr",
    "uragan", "tornad", "victim", "morti", "ranit",

    # corupție / scandal / fraudă
    "corup", "mita", "spag", "fraud", "scandal",

    # economie nasoală
    "faliment", "colaps", "criz", "scumpir", "infl", "reces", "somaj",
]

RAW_SOFT_NEGATIVE = [
    # politică light (mai ales RO) — (la RO strict le hard-block-uim separat)
    "politic", "guvern", "parlament", "aleger", "ministr", "presed",

    # controverse / scandal (toate)
    "controvers", "scandal", "dezinform", "fake",

    # climate doom (NU climate research)
    "catastrof", "apocalips", "panic", "dezastru", "colaps", "mort", "morti", "victim",
]

RAW_POSITIVE_STRONG = [
    "medalie de aur", "medalie de argint", "medalie de bronz",
    "locul intai", "campion", "olimpiad", "record", "a castigat",
    "vindec", "tratament nou", "terapie nou",
    "vaccin", "imuniz",
    "rezultate promitatoare", "remisie", "supravietuire",
    "salvat", "au salvat",
    "adopt", "voluntar", "donat",
    "spital nou", "sectie nou",
    "inaugur",
]

RAW_POSITIVE_WEAK = [
    "inova",
    "descoper",
    "cercet",
    "studiu",
    "test clinic",
    "aprobat",
    "startup",
    "invest",
    "finant",
    "parteneriat",
    "festival",
    "eveniment",
    "concert",
    "expozit",
    "muzeu",
    "dragut",
    "simpatic",
    "amuzant",
    "funny",
    "viral",
    "natura",
    "recicl",
    "plant",
    "padur",
    "energie verde",
]

# RO STRICT: dacă apar în titlu/rezumat => respinge instant (indiferent de scor)
# (prinde alarmism medical, crime/accidente, politică, clickbait negativ etc.)
RAW_RO_HARD_BLOCK = [
    # alarmism / epidemiologic
    "minim istoric",
    "record negativ",
    "crestere alarmanta",
    "creștere alarmantă",
    "alarmant",
    "explozie a cazurilor",
    "val de",
    "focar",
    "epidem",
    "alerta",
    "alertă",
    "criza",
    "criză",
    "cazuri de",
    "infectari",
    "infectări",
    "imbulnav",
    "îmbolnăv",
    "spitaliz",
    "decese",
    "mort",
    "morti",
    "morți",
    "victime",
    "ranit",
    "răni",

    # accidente / tragedii (extra)
    "accident mortal",
    "traged",
    "dezastru",
    "soc",
    "șoc",
    "groaza",
    "groază",

    # politică / scandal (scoatem complet la RO strict)
    "politic",
    "guvern",
    "parlament",
    "aleger",
    "ministr",
    "presed",
    "prim-ministr",
    "premier",
    "psd",
    "pnl",
    "aur",
    "udmr",

    # economie negativă
    "scumpir",
    "infl",
    "reces",
    "faliment",
    "insolvent",
    "concedier",
    "somaj",
    "șomaj",

    # clickbait negativ
    "nu o sa-ti vina sa crezi",
    "nu o să-ți vină să crezi",
    "halucinant",
    "ingrozitor",
    "îngrozitor",
    "cosmar",
    "coșmar",
    "de groaza",
    "de groază",
]

NEGATIVE_KEYWORDS = [normalize_text(k) for k in RAW_NEGATIVE_KEYWORDS]
SOFT_NEGATIVE = [normalize_text(k) for k in RAW_SOFT_NEGATIVE]
POSITIVE_STRONG = [normalize_text(k) for k in RAW_POSITIVE_STRONG]
POSITIVE_WEAK = [normalize_text(k) for k in RAW_POSITIVE_WEAK]
RO_HARD_BLOCK = [normalize_text(k) for k in RAW_RO_HARD_BLOCK]


def score_item(title: str, summary: str, kind: str) -> int:
    # normalize text once (fără diacritice, lowercase, spații curate)
    text = normalize_text(f"{title} {summary}")

    # RO STRICT hard-block (înainte de orice scorare)
    if kind == "ro":
        for kw in RO_HARD_BLOCK:
            if kw and kw in text:
                return -999

    # HARD reject (global + ro)
    for kw in NEGATIVE_KEYWORDS:
        if kw and kw in text:
            return -999

    score = 0
    for kw in POSITIVE_STRONG:
        if kw and kw in text:
            score += 3  # mai “tare” decât înainte

    for kw in POSITIVE_WEAK:
        if kw and kw in text:
            score += 1

    for kw in SOFT_NEGATIVE:
        if kw and kw in text:
            score -= 1

    # RO strict: trebuie să aibă măcar 1 semnal pozitiv (score >= 1)
    if kind == "ro" and score < 1:
        return -999

    # GLOBAL (science / medical / environment):
    # respingem doar dacă e clar negativ
    if kind == "global" and score < 0:
        return -999

    # GLOBAL: permis și score 0 (dar trece doar dacă nu e hard-negative)
    return score


def fetch_rss(url: str) -> feedparser.FeedParserDict:
    return feedparser.parse(url, agent=USER_AGENT)


def dedupe_key(link: str, title: str) -> str:
    base = (link or title or "").strip()
    return hashlib.sha1(base.encode("utf-8", errors="ignore")).hexdigest()


def extract_og_image(html: str) -> Optional[str]:
    if not html:
        return None
    # og:image
    m = re.search(r'property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html, flags=re.I)
    if m:
        return m.group(1).strip()
    # uneori e invers ordinea atributelor
    m = re.search(r'content=["\']([^"\']+)["\']\s+property=["\']og:image["\']', html, flags=re.I)
    if m:
        return m.group(1).strip()
    return None


def extract_image_url(e: dict) -> Optional[str]:
    """
    Încearcă, în ordine:
      1) media_content / media_thumbnail (feedparser)
      2) links/enclosures (rel=enclosure)
      3) <img src="..."> din summary/content
      4) fallback: og:image din pagina linkului (dacă există link)
    """
    # 1) media:content
    mc = e.get("media_content")
    if isinstance(mc, list) and mc:
        for item in mc:
            url = (item.get("url") or "").strip()
            if url:
                return url

    # 2) media:thumbnail
    mt = e.get("media_thumbnail")
    if isinstance(mt, list) and mt:
        for item in mt:
            url = (item.get("url") or "").strip()
            if url:
                return url

    # 3) enclosures in links
    links = e.get("links")
    if isinstance(links, list):
        for l in links:
            rel = (l.get("rel") or "").lower()
            href = (l.get("href") or "").strip()
            if rel == "enclosure" and href:
                return href

    # 4) HTML in summary/content
    html = (e.get("summary") or e.get("content") or "")
    if isinstance(html, list) and html:
        html = html[0].get("value", "")
    if isinstance(html, dict):
        html = html.get("value", "")
    if isinstance(html, str) and html:
        m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html, flags=re.I)
        if m:
            return m.group(1).strip()

    # 5) og:image fallback (dacă avem link)
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

            for e in feed.entries[:60]:
                title = (e.get("title") or "").strip()
                link = (e.get("link") or "").strip()
                summary = get_entry_summary(e)
                if not title or not link:
                    continue

                dt = parse_entry_datetime(e)
                published = (dt or datetime.now(timezone.utc)).replace(microsecond=0)

                # kind trebuie stabilit înainte de scoring
                kind = "ro" if section_id == "romania" else "global"

                # strict filtering (RO hard + must-positive, global doar hard-negative)
                score = score_item(title, summary, kind)
                if score < 0:
                    continue

                # praguri opționale per secțiune (dacă există în config)
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

                # imagine (dacă găsim)
                img = extract_image_url(e)
                if img:
                    item["image"] = img

                # traduceri (opțional, doar dacă ai key; dacă nu, nu face nimic)
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

    # deterministic: one per UTC day
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    idx = int(hashlib.sha1(day.encode("utf-8")).hexdigest(), 16) % len(jokes)
    return {"date_utc": day, "text": jokes[idx]}


def extract_meta_description(html: str) -> str:
    if not html:
        return ""

    # og:description
    m = re.search(r'property=["\']og:description["\']\s+content=["\']([^"\']+)["\']', html, flags=re.I)
    if m:
        return strip_html(m.group(1)).strip()

    # meta description
    m = re.search(r'name=["\']description["\']\s+content=["\']([^"\']+)["\']', html, flags=re.I)
    if m:
        return strip_html(m.group(1)).strip()

    return ""


def pick_flickr_images(limit: int = 3) -> List[Dict[str, Any]]:
    """
    Exact 3: space + animals + landscapes, din Flickr tag feeds.
    """
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


def main() -> None:
    cfg = load_yaml(CONFIG_PATH)

    payload: Dict[str, Any] = {
        "generated_utc": utc_now_iso(),
        "sections": build_sections(cfg),
        "joke_ro": build_joke(),
        "top_images": pick_flickr_images(limit=3),
    }

    safe_mkdir(os.path.dirname(OUT_PATH))
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"[OK] wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
