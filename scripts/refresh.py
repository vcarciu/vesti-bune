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
    "invest", "finant", "grant", "fonduri", "program", "proiect",
    "tratament nou", "terapie nou", "aprobat", "screening gratuit", "gratuit",
    "salvat", "voluntar", "donat", "campanie", "reabilitat",
    "scade", "reducere", "imbunatat", "îmbunatat",
    "adopt", "adopție", "adoptie", "salvare", "recuperat", "vindec",
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
JOKES = [
    "— De ce ai deschis frigiderul de 10 ori? — Ca să văd dacă s-a răzgândit prăjitura.",
    "Când am zis „vreau să fiu fit”, corpul meu a înțeles „vreau să fiu… fitil” și a cerut somn.",
    "Am început dieta. Azi am mâncat doar lucruri care nu se văd pe cântar: nervi și speranțe.",
    "— Ai pierdut ceva? — Da, motivația. — Unde? — Între „mâine” și „de luni”.",
    "Am făcut sport azi: am ridicat moralul prietenilor cu glume proaste.",
    "— Ce planuri ai? — Să fiu productiv. — Și? — Am produs o cafea.",
    "Dacă râsul ar arde calorii, deja eram pe modul avion.",
    "Nu sunt leneș. Sunt pe „modul economisire energie”.",
    "Azi am fost la sală. N-am intrat, dar am trecut pe lângă. Se pune?",
    "Mi-am setat alarmă să mă trezesc devreme. Alarma s-a trezit. Eu… nu.",
    "— Ce faci? — Mă organizez. — Cum? — Îmi mut haosul dintr-un colț în altul.",
    "M-am apucat de citit. După 2 pagini m-am simțit cult și am închis cartea.",
    "Am încercat să fiu zen. Apoi mi-a picat Wi-Fi-ul.",
    "Când zici „o să fac rapid”, universul aude „o să dureze 3 ore”.",
    "Nu sunt indecis. Încă mă gândesc.",
]

SATIRE_FALLBACK = [
    {"title": "Românul a descoperit secretul fericirii: a oprit notificările și a pornit viața", "link": "#"},
    {"title": "Bucureșteanul a făcut 10.000 de pași: 9.800 au fost căutând loc de parcare", "link": "#"},
    {"title": "Un cetățean a cerut „doar vești bune”; internetul a intrat în concediu medical", "link": "#"},
    {"title": "Specialiștii confirmă: 80% din stres dispare când nu mai răspunzi la „ai un minut?”", "link": "#"},
    {"title": "Un optimist a spus „se rezolvă”; problema s-a speriat și a plecat singură", "link": "#"},
]

def build_joke() -> Dict[str, Any]:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    idx = int(hashlib.sha1(day.encode("utf-8")).hexdigest(), 16) % len(JOKES)
    return {"date_utc": day, "text": JOKES[idx], "source": "vesti-bune"}

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
def pick_flickr_images(limit: int = 3) -> List[Dict[str, Any]]:
    feeds = [
        ("space", "https://www.flickr.com/services/feeds/photos_public.gne?format=rss2&tags=space,stars,nebula&tagmode=any"),
        ("animals", "https://www.flickr.com/services/feeds/photos_public.gne?format=rss2&tags=animals,cute,dog,cat&tagmode=any"),
        ("landscape", "https://www.flickr.com/services/feeds/photos_public.gne?format=rss2&tags=landscape,nature,mountains&tagmode=any"),
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
# Build sections
# -----------------------------
def build_sections(cfg: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    rss_sources = cfg.get("rss_sources") or {}
    sections_def = cfg.get("sections") or []
    filters_cfg = cfg.get("filters") or {}

    thresholds = (filters_cfg.get("thresholds") or {})
    ro_min_items = int(filters_cfg.get("ro_min_items", 16))
    ro_try_relaxed = bool(filters_cfg.get("ro_try_relaxed", True))

    max_items_map = {s["id"]: int(s.get("max_items", 20)) for s in sections_def if "id" in s}

    out: Dict[str, List[Dict[str, Any]]] = {}
    seen: set = set()
    ro_candidates: List[Dict[str, Any]] = []

    for section_id, sources in rss_sources.items():
        items: List[Dict[str, Any]] = []

        for src in sources:
            name = src.get("name", section_id)
            url = (src.get("url") or "").strip()
            if not url:
                continue

            feed = fetch_rss(url)
            for e in (feed.entries or [])[:90]:
                title = (e.get("title") or "").strip()
                link = (e.get("link") or "").strip()
                if not title or not link:
                    continue

                link = canonicalize_url(link)
                summary = get_entry_summary(e)
                dt = parse_entry_datetime(e)
                published = (dt or datetime.now(timezone.utc)).replace(microsecond=0)

                kind = "ro" if section_id == "romania" else "global"

                if kind == "ro":
                    if not ro_allow(title, summary, relaxed=False):
                        if ro_try_relaxed and not ro_hard_block(title, summary):
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
            if img:
                c["image"] = img

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
    payload: Dict[str, Any] = {
        "generated_utc": utc_now_iso(),
        "sections": build_sections(cfg),
        "joke_ro": build_joke(),
        "satire_ro": build_satire(),
        "top_images": pick_flickr_images(limit=3),
    }
    write_json(OUT_NEWS, payload)
    write_json(OUT_ITEMS, payload)
    print("[OK] wrote data/news.json and data/items.json")

if __name__ == "__main__":
    main()
