import json
import re
import time
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path

import feedparser
from dateutil import parser as dtparser

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
OUT_JSON = DATA_DIR / "news.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)

RSS_SOURCES = [
    {"name": "BBC News - Science & Environment", "url": "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml"},
    {"name": "BBC News - Health", "url": "https://feeds.bbci.co.uk/news/health/rss.xml"},
    {"name": "AP News - Health", "url": "https://apnews.com/hub/health?output=rss"},
    {"name": "AP News - Science", "url": "https://apnews.com/hub/science?output=rss"},
    {"name": "Our World in Data", "url": "https://ourworldindata.org/feeds/latest.xml"},
]

NEGATIVE_PATTERNS = [
    r"\bwar\b", r"\battack\b", r"\bkilled\b", r"\bdead\b", r"\bdeath\b", r"\bshooting\b",
    r"\bterror\b", r"\bexplosion\b", r"\bbomb\b", r"\bhostage\b", r"\bcrisis\b",
    r"\bscandal\b", r"\bfraud\b", r"\bcorruption\b", r"\bviolence\b", r"\bmurder\b",
]
NEG_RE = re.compile("|".join(NEGATIVE_PATTERNS), re.IGNORECASE)

POSITIVE_HINTS = [
    r"\bbreakthrough\b", r"\bdiscovery\b", r"\bimproves?\b", r"\breduces?\b",
    r"\bsuccess\b", r"\baward\b", r"\bprogress\b", r"\brecord low\b",
    r"\bvaccine\b", r"\btreatment\b", r"\btrial\b",
    r"\bclean energy\b", r"\bsolar\b", r"\bwind\b", r"\bemissions?\b",
    r"\bconservation\b", r"\breforest\b", r"\beducation\b",
]
POS_RE = re.compile("|".join(POSITIVE_HINTS), re.IGNORECASE)

def normalize_text(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s

def safe_parse_date(entry) -> str:
    for key in ("published", "updated", "created"):
        if key in entry and entry.get(key):
            try:
                dt = dtparser.parse(entry.get(key))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc).isoformat()
            except Exception:
                pass
    return datetime.now(timezone.utc).isoformat()

def is_negative(text: str) -> bool:
    return bool(NEG_RE.search(text))

def is_positive(text: str) -> bool:
    return bool(POS_RE.search(text))

def fingerprint(title: str, link: str) -> str:
    raw = (normalize_text(title) + "|" + (link or "")).encode("utf-8", errors="ignore")
    return sha1(raw).hexdigest()

def main():
    items = []
    seen = set()
    now_iso = datetime.now(timezone.utc).isoformat()

    for src in RSS_SOURCES:
        try:
            feed = feedparser.parse(src["url"])
            for e in feed.entries[:30]:
                title = normalize_text(getattr(e, "title", ""))
                link = getattr(e, "link", "")
                summary = normalize_text(getattr(e, "summary", ""))

                if not title or not link:
                    continue

                text_blob = f"{title} {summary}"

                if is_negative(text_blob):
                    continue
                if not is_positive(text_blob):
                    continue

                fp = fingerprint(title, link)
                if fp in seen:
                    continue
                seen.add(fp)

                items.append({
                    "id": fp[:12],
                    "title": title,
                    "summary": summary[:280],
                    "link": link,
                    "source": src["name"],
                    "published_utc": safe_parse_date(e),
                })

            time.sleep(0.3)
        except Exception:
            # nu crapă tot dacă o sursă pică
            continue

    # cele mai noi sus
    def sort_key(x):
        try:
            return dtparser.parse(x["published_utc"])
        except Exception:
            return datetime.now(timezone.utc)

    items.sort(key=sort_key, reverse=True)

    payload = {
        "generated_utc": now_iso,
        "count": len(items),
        "items": items[:60],
    }

    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {OUT_JSON} with {payload['count']} items.")

if __name__ == "__main__":
    main()
