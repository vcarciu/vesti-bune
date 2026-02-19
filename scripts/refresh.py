import os
import re
import json
import hashlib
import unicodedata
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

import requests
import feedparser
import yaml


ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CONFIG_PATH = os.path.join(ROOT_DIR, "config", "sources.yml")

OUT_NEWS = os.path.join(ROOT_DIR, "data", "news.json")
OUT_ITEMS = os.path.join(ROOT_DIR, "data", "items.json")

USER_AGENT = "vesti-bune-bot/2.0"


# --------------------------------------------------
# Utils
# --------------------------------------------------

def utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_text(s: str) -> str:
    s = (s or "").lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", s).strip()


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "").strip()


# --------------------------------------------------
# STRICT RO FILTER
# --------------------------------------------------

RAW_RO_HARD_BLOCK = [
    # medical alarmism
    "minim istoric",
    "crestere alarmanta",
    "creștere alarmantă",
    "alarmant",
    "cazuri de",
    "focar",
    "epidem",
    "alerta",
    "alertă",
    "criza",
    "criză",
    "decese",
    "mort",
    "morti",
    "morți",
    "victime",
    "ranit",
    "răni",
    "infect",
    "spitaliz",

    # accidente / tragedii
    "accident",
    "traged",
    "dezastru",
    "exploz",
    "incend",
    "cutremur",

    # politică
    "guvern",
    "parlament",
    "aleger",
    "ministr",
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
    "somaj",
]

RO_BLOCK = [normalize_text(x) for x in RAW_RO_HARD_BLOCK]


POSITIVE_HINTS = [
    "a castigat",
    "a câștigat",
    "medalie",
    "record",
    "inaugur",
    "spital nou",
    "tratament nou",
    "terapie nou",
    "aprobat",
    "invest",
    "finant",
    "program",
    "gratuit",
    "salvat",
    "voluntar",
    "donat",
]

POSITIVE_HINTS = [normalize_text(x) for x in POSITIVE_HINTS]


def allow_ro(title: str, summary: str) -> bool:
    text = normalize_text(title + " " + summary)

    # HARD BLOCK
    for kw in RO_BLOCK:
        if kw in text:
            return False

    # must contain positive signal
    for kw in POSITIVE_HINTS:
        if kw in text:
            return True

    return False


# --------------------------------------------------
# RSS
# --------------------------------------------------

def fetch_rss(url: str):
    return feedparser.parse(url, agent=USER_AGENT)


def build_sections(cfg: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    rss_sources = cfg.get("rss_sources", {})
    sections = {}

    for section_id, sources in rss_sources.items():
        items = []
        seen = set()

        for src in sources:
            feed = fetch_rss(src["url"])

            for e in feed.entries[:50]:
                title = (e.get("title") or "").strip()
                link = (e.get("link") or "").strip()
                summary = strip_html(e.get("summary") or "")

                if not title or not link:
                    continue

                if section_id == "romania":
                    if not allow_ro(title, summary):
                        continue

                key = hashlib.sha1((link + title).encode()).hexdigest()
                if key in seen:
                    continue
                seen.add(key)

                items.append({
                    "section": section_id,
                    "title": title,
                    "summary": summary,
                    "link": link,
                    "published_utc": utc_now_iso()
                })

        sections[section_id] = items[:20]

    return sections


# --------------------------------------------------
# MAIN
# --------------------------------------------------

def main():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    payload = {
        "generated_utc": utc_now_iso(),
        "sections": build_sections(cfg)
    }

    os.makedirs(os.path.dirname(OUT_NEWS), exist_ok=True)

    # write BOTH files (fix crash)
    with open(OUT_NEWS, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    with open(OUT_ITEMS, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print("OK - JSON generated")


if __name__ == "__main__":
    main()
