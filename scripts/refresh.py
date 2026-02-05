# scripts/refresh.py
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


def score_item(section_id: str, title: str, summary: str, filters_cfg: Dict[str, Any]) -> Tuple[int, bool]:
    thresholds = (filters_cfg.get("thresholds") or {})
    hard_blacklist = filters_cfg.get("hard_blacklist") or []
    scoring_cfg = filters_cfg.get("scoring") or {}
    pos = scoring_cfg.get("positive_keywords") or []
    neg = scoring_cfg.get("negative_keywords") or []
    medical_extra = filters_cfg.get("medical_extra_blacklist") or []

    text = normalize_text(f"{title} {summary}")

    for w in hard_blacklist:
        if normalize_text(w) in text:
            return (-999, False)

    if section_id == "medical":
        for w in medical_extra:
            if normalize_text(w) in text:
                return (-999, False)

    score = 0

    for w in pos:
        ww = normalize_text(w)
        if ww and ww in text:
            score += 1

    for w in neg:
        ww = normalize_text(w)
        if ww and ww in text:
            score -= 1

    if len(summary.strip()) >= 120:
        score += 1
    if len(summary.strip()) == 0:
        score -= 1

    if section_id in ("medical", "science", "environment"):
        score += 1

    _ = thresholds.get(section_id, 0)
    return (score, True)


def fetch_rss(url: str) -> feedparser.FeedParserDict:
    return feedparser.parse(url, agent=USER_AGENT)


def dedupe_key(link: str, title: str) -> str:
    base = (link or title or "").strip()
    return hashlib.sha1(base.encode("utf-8", errors="ignore")).hexdigest()


def extract_image_url(entry: Dict[str, Any]) -> Optional[str]:
    # 1) media_content
    mc = entry.get("media_content")
    if isinstance(mc, list):
        for m in mc:
            u = (m.get("url") or "").strip()
            if u:
                return u

    # 2) media_thumbnail
    mt = entry.get("media_thumbnail")
    if isinstance(mt, list):
        for m in mt:
            u = (m.get("url") or "").strip()
            if u:
                return u

    # 3) links (enclosures)
    links = entry.get("links")
    if isinstance(links, list):
        for l in links:
            href = (l.get("href") or "").strip()
            ltype = (l.get("type") or "").lower()
            rel = (l.get("rel") or "").lower()
            if href and ("image" in ltype or rel == "enclosure"):
                return href

    # 4) <img src="..."> in summary/content
    raw = (entry.get("summary") or "")
    content = entry.get("content")
    if isinstance(content, list) and content:
        raw = raw + " " + (content[0].get("value") or "")
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', raw, flags=re.IGNORECASE)
    if m:
        return m.group(1).strip()

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

                score, allowed = score_item(section_id, title, summary, filters_cfg)
                if not allowed:
                    continue

                thr = int(thresholds.get(section_id, 0))
                if score < thr:
                    continue

                key = dedupe_key(link, title)
                if key in seen:
                    continue
                seen.add(key)

                kind = "ro" if section_id == "romania" else "global"

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

    return {
        "date_utc": day,
        "text": jokes[idx]
    }
    
def fetch_url_with_final(url: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20, allow_redirects=True)
        if r.status_code == 200:
            return r.text, r.url
    except Exception:
        return None, None
    return None, None


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



def build_photos(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    3 poze: space + animals + landscapes.
    Dacă feed-ul nu dă imagine, luăm og:image din pagina articolului.
    Dacă tot nu, folosim fallback POTD (și dacă nici atunci, punem un placeholder safe).
    """
    photo_sources = cfg.get("photo_sources") or {}
    fallback = cfg.get("photo_fallback") or {}

    categories = [
        ("space", "🚀 Spațiu"),
        ("animals", "🐾 Animale"),
        ("landscapes", "🏞️ Peisaj"),
    ]

    out: List[Dict[str, Any]] = []
    used_images: set = set()

    def pick_one(sources, cat_id, cat_label) -> Optional[Dict[str, Any]]:
        nonlocal used_images
        for src in sources:
            name = src.get("name", cat_label)
            url = (src.get("url") or "").strip()
            if not url:
                continue

            feed = fetch_rss(url)
            for e in feed.entries[:60]:
                title = (e.get("title") or "").strip()
                link = (e.get("link") or "").strip()
                if not link:
                    continue

                img = extract_image_url(e)

                # OG fallback dacă feed-ul nu are imagine
                if not img:
                    html, _final = fetch_url_with_final(link)
                    img = extract_og_image(html or "")

                if not img:
                    continue

                if img in used_images:
                    continue

                dt = parse_entry_datetime(e)
                published = (dt or datetime.now(timezone.utc)).replace(microsecond=0)

                item = {
                    "category_id": cat_id,
                    "category_label": cat_label,
                    "source": name,
                    "title": title or cat_label,
                    "link": link,
                    "image_url": img,
                    "published_utc": published.isoformat(),
                }
                return item
        return None

    # 1) pick per category
    for cat_id, cat_label in categories:
        picked = pick_one(photo_sources.get(cat_id) or [], cat_id, cat_label)
        if picked:
            used_images.add(picked["image_url"])
            out.append(picked)

    # 2) fill missing with fallback POTD
    missing = [c for c in categories if c[0] not in {p["category_id"] for p in out}]
    if missing and fallback.get("url"):
        fb_sources = [{"name": fallback.get("name", "Wikimedia POTD"), "url": fallback["url"]}]
        for cat_id, cat_label in missing:
            picked = pick_one(fb_sources, cat_id, cat_label)
            if picked:
                used_images.add(picked["image_url"])
                out.append(picked)

    # 3) LAST resort: dacă tot lipsesc, punem placeholder (ca să fie 3 rânduri în UI)
    # (UI-ul tău va afișa tot 3; la click se deschide link-ul)
    while len(out) < 3:
        cat_id, cat_label = categories[len(out)]
        out.append({
            "category_id": cat_id,
            "category_label": cat_label,
            "source": "—",
            "title": "Nu am găsit o poză acum (refresh următor)",
            "link": "https://vcarciu.github.io/vesti-bune/",
            "image_url": "https://vcarciu.github.io/vesti-bune/og.jpg",
            "published_utc": utc_now_iso(),
        })

    return out[:3]


def fetch_url(url: str) -> Optional[str]:
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
        if r.status_code == 200:
            return r.text
    except Exception:
        return None
    return None


def pick_timesnewroman_article(homepage_html: str) -> Optional[str]:
    # Find first article-like link on homepage
    # Keep it simple & robust: pick first href that looks like a post and is on timesnewroman.ro
    # Exclude obvious non-article paths.
    hrefs = re.findall(r'href=["\'](https?://www\.timesnewroman\.ro/[^"\']+)["\']', homepage_html, flags=re.I)
    if not hrefs:
        hrefs = re.findall(r'href=["\'](/[^"\']+)["\']', homepage_html, flags=re.I)
        hrefs = ["https://www.timesnewroman.ro" + h for h in hrefs if h.startswith("/")]

    for h in hrefs:
        if not h.startswith("https://www.timesnewroman.ro/"):
            continue
        # reject some paths
        if any(bad in h for bad in ["/category/", "/tag/", "/author/", "/page/", "/wp-", "feed", "rss", "#"]):
            continue
        # looks like content page
        if len(h.split("/")) >= 5:
            return h
    return None


def get_page_title(html: str) -> str:
    m = re.search(r"<title>\s*(.*?)\s*</title>", html, flags=re.I | re.S)
    if m:
        t = strip_html(m.group(1))
        t = t.replace(" - Times New Roman", "").replace(" | Times New Roman", "").strip()
        return t
    return "TimesNewRoman"


def is_premium_tnr(html: str) -> bool:
    """
    Detectare premium *specifică*, ca să nu blocăm pagini normale unde 'abonament' apare în footer.
    """
    if not html:
        return False
    h = normalize_text(html)

    # dacă nu există deloc "tnr premium", considerăm non-premium
    if "tnr premium" not in h:
        return False

    # semnale de paywall / abonare (mai specifice)
    paywall_markers = [
        "doar pentru abonati",
        "continua cu abonament",
        "aboneaza-te",
        "devino abonat",
        "continut premium",
        "articol premium",
        "acest articol este disponibil",
    ]
    return any(m in h for m in paywall_markers)


def looks_like_paywall(final_url: str, html: str) -> bool:
    u = (final_url or "").lower()
    h = normalize_text(html or "")

    # URL final suspect (redirect la premium/abonare/login)
    if any(x in u for x in ["premium", "abon", "subscribe", "login", "cont"]):
        return True

    # markeri specifici de paywall (nu generici)
    markers = [
        "continut premium",
        "doar pentru abonati",
        "devino abonat",
        "continua cu abonament",
        "aboneaza-te pentru a citi",
    ]
    return any(m in h for m in markers)


def build_satire(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    satire_cfg = cfg.get("satire") or {}
    if not satire_cfg.get("enabled", False):
        return []

    listing_urls = [
        satire_cfg.get("listing_url") or "https://www.timesnewroman.ro/monden/",
        "https://www.timesnewroman.ro/",
        "https://www.timesnewroman.ro/life-death/",
        "https://www.timesnewroman.ro/sport/",
        "https://www.timesnewroman.ro/politic/",
    ]
    listing_urls = [u.strip() for u in listing_urls if u]

    candidates = []
    seen = set()

    for lu in listing_urls:
        html, _ = fetch_url_with_final(lu)
        if not html:
            continue

        hrefs = re.findall(r'href=["\'](https?://www\.timesnewroman\.ro/[^"\']+)["\']', html, flags=re.I)
        for h in hrefs:
            if not h.startswith("https://www.timesnewroman.ro/"):
                continue
            if any(bad in h for bad in ["/category/", "/tag/", "/author/", "/page/", "/wp-", "feed", "rss", "#"]):
                continue
            if h in seen:
                continue
            seen.add(h)
            if len(h.split("/")) >= 5:
                candidates.append(h)

    picked_link = None
    picked_title = None

    # încercăm până la 60 articole
    for link in candidates[:60]:
        art_html, final_url = fetch_url_with_final(link)
        if not art_html or not final_url:
            continue

        if looks_like_paywall(final_url, art_html):
            continue

        picked_link = final_url
        picked_title = get_page_title(art_html) or "TimesNewRoman"
        break

    # dacă chiar TOT e paywall azi (rar), alegem primul articol oricum (dar e articol direct)
    if not picked_link and candidates:
        art_html, final_url = fetch_url_with_final(candidates[0])
        picked_link = final_url or candidates[0]
        picked_title = get_page_title(art_html or "") or "TimesNewRoman"

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return [{
        "date_utc": day,
        "source": "TimesNewRoman",
        "title": picked_title or "TimesNewRoman",
        "link": picked_link or "https://www.timesnewroman.ro/",
        "note": "Satiră — nu este știre reală."
    }]




def flatten_items(sections: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    # Keep backwards compatibility for any older UI paths: merge main news sections only
    keep = []
    for sec in ("romania", "medical", "science", "environment"):
        keep.extend(sections.get(sec, []))
    keep.sort(key=lambda x: x.get("published_utc", ""), reverse=True)
    return keep[:60]


def main() -> None:
    if not os.path.exists(CONFIG_PATH):
        raise SystemExit(f"Missing config: {CONFIG_PATH}")

    cfg = load_yaml(CONFIG_PATH)
    safe_mkdir(os.path.dirname(OUT_PATH))

    sections = build_sections(cfg)

    # Add photos/joke/satire
    sections["photos"] = build_photos(cfg)

    joke = build_joke()
    sections["joke"] = [joke] if joke else []

    sections["satire"] = build_satire(cfg)

    flat_items = flatten_items(sections)

    payload = {
        "generated_utc": utc_now_iso(),
        "count": len(flat_items),
        "items": flat_items,      # compat
        "sections": sections,     # new UI
    }

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"Wrote {OUT_PATH} with {len(flat_items)} items; photos={len(sections.get('photos', []))} joke={len(sections.get('joke', []))} satire={len(sections.get('satire', []))}")


if __name__ == "__main__":
    main()
