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

OUT_NEWS = os.path.join(ROOT_DIR, "data", "news.json")
OUT_ITEMS = os.path.join(ROOT_DIR, "data", "items.json")

USER_AGENT = "vesti-bune-bot/3.0 (+https://vcarciu.github.io/vesti-bune/)"

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

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

def get_entry_summary(entry: Dict[str, Any]) -> str:
    if entry.get("summary"):
        return strip_html(entry.get("summary") or "")
    content = entry.get("content")
    if isinstance(content, list) and content:
        return strip_html(content[0].get("value") or "")
    return ""

def parse_entry_datetime(entry: Dict[str, Any]) -> Optional[datetime]:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                return datetime.fromtimestamp(time.mktime(t), tz=timezone.utc)
            except Exception:
                pass
    return None

def fetch_rss(url: str) -> feedparser.FeedParserDict:
    return feedparser.parse(url, agent=USER_AGENT)

def safe_mkdir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def dedupe_key(link: str, title: str) -> str:
    base = (link or "") + "||" + (title or "")
    return hashlib.sha1(base.encode("utf-8", errors="ignore")).hexdigest()

# -------------------------
# STRICT RO filtering
# -------------------------
RAW_RO_HARD_BLOCK = [
    # alarmism medical / epidemiologic
    "minim istoric", "record negativ",
    "crestere alarmanta", "creștere alarmantă", "alarmant",
    "explozie a cazurilor", "val de",
    "cazuri de", "focar", "epidem", "alerta", "alertă", "criza", "criză",
    "infect", "imbulnav", "îmbolnăv", "spitaliz", "decese", "mort", "morti", "morți", "victime", "ranit", "răni",

    # crime / violenta
    "crim", "ucis", "omor", "viol", "agresi", "talhar", "jaf", "furt", "drog", "arest", "retinut", "perchez", "anchet", "dosar", "dna", "diicot",

    # accidente / dezastre
    "accident", "traged", "dezastru", "exploz", "incend", "cutremur", "inunda",

    # politica (strict)
    "politic", "guvern", "parlament", "aleger", "ministr", "premier", "presed",
    "psd", "pnl", "aur", "udmr",

    # economie negativă
    "scumpir", "infl", "reces", "faliment", "insolvent", "concedier", "somaj", "șomaj",

    # clickbait negativ
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

    # hard block
    for kw in RO_HARD_BLOCK:
        if kw and kw in text:
            return False

    # must have a positive signal
    for kw in RO_POSITIVE_HINTS:
        if kw and kw in text:
            return True

    return False

# -------------------------
# Build
# -------------------------
def build_sections(cfg: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    rss_sources = cfg.get("rss_sources") or {}
    sections_def = cfg.get("sections") or []
    max_items_map = {s["id"]: int(s.get("max_items", 20)) for s in sections_def if "id" in s}

    out: Dict[str, List[Dict[str, Any]]] = {}
    seen = set()

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

                if section_id == "romania":
                    if not ro_allow(title, summary):
                        continue

                dt = parse_entry_datetime(e)
                published = (dt or datetime.now(timezone.utc)).replace(microsecond=0)

                key = dedupe_key(link, title)
                if key in seen:
                    continue
                seen.add(key)

                items.append({
                    "section": section_id,
                    "source": name,
                    "title": title,
                    "summary": summary,
                    "link": link,
                    "published_utc": published.isoformat(),
                })

        items.sort(key=lambda x: x.get("published_utc", ""), reverse=True)
        out[section_id] = items[: max_items_map.get(section_id, 20)]

    return out

def main() -> None:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    payload = {
        "generated_utc": utc_now_iso(),
        "sections": build_sections(cfg),
    }

    safe_mkdir(os.path.dirname(OUT_NEWS))
    with open(OUT_NEWS, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    with open(OUT_ITEMS, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print("[OK] wrote data/news.json and data/items.json")

if __name__ == "__main__":
    main()
