import json
import os
import re
import time
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path

import feedparser
import requests
from dateutil import parser as dtparser

# ---------------- Paths ----------------

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
OUT_JSON = DATA_DIR / "news.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ---------------- RSS SOURCES ----------------

RSS_RO = [
    {"name": "HotNews", "url": "https://rss.hotnews.ro/"},
    {"name": "Digi24", "url": "https://www.digi24.ro/rss"},
    {"name": "Agerpres", "url": "https://www.agerpres.ro/rss"},
    {"name": "Spotmedia", "url": "https://spotmedia.ro/feed"},
    {"name": "News.ro", "url": "https://www.news.ro/rss"},
]

RSS_GLOBAL = [
    {"name": "BBC – Science & Environment", "url": "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml"},
    {"name": "BBC – Health", "url": "https://feeds.bbci.co.uk/news/health/rss.xml"},
    {"name": "Our World in Data", "url": "https://ourworldindata.org/feeds/latest.xml"},
]

MAX_GLOBAL_PER_RUN = 3

# ---------------- FILTERING ----------------

NEGATIVE_PATTERNS = [
    r"\bwar\b", r"\battack\b", r"\bkilled\b", r"\bdead\b", r"\bdeath\b",
    r"\bshooting\b", r"\bterror\b", r"\bexplosion\b", r"\bbomb\b",
    r"\bcrisis\b", r"\bscandal\b", r"\bfraud\b", r"\bcorruption\b",
    r"\bviolence\b", r"\bmurder\b", r"\bsuicide\b",
    r"\br(a|ă)zboi\b", r"\batac\b", r"\bucis\b", r"\bdeces\b",
    r"\bcrim(a|ă)\b", r"\bviolen(t|ț)(a|ă)\b", r"\baccident\b",
]

POSITIVE_PATTERNS = [
    r"\bbreakthrough\b", r"\bdiscovery\b", r"\bprogress\b",
    r"\bsuccess\b", r"\baward\b", r"\brecord\b",
    r"\bvaccine\b", r"\btreatment\b",
    r"\bclean energy\b", r"\bsolar\b", r"\bwind\b",
    r"\bdescoper(ire|it)\b", r"\breu(s|ș)it(a|ă)\b",
    r"\bsucces\b", r"\bprogres\b", r"\bvaccin\b",
]

NEG_RE = re.compile("|".join(NEGATIVE_PATTERNS), re.IGNORECASE)
POS_RE = re.compile("|".join(POSITIVE_PATTERNS), re.IGNORECASE)

# ---------------- HELPERS ----------------

def clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())

def is_negative(text: str) -> bool:
    return bool(NEG_RE.search(text))

def is_positive(text: str) -> bool:
    return bool(POS_RE.search(text))

def parse_date(entry) -> str:
    for key in ("published", "updated", "created"):
        if entry.get(key):
            try:
                dt = dtparser.parse(entry.get(key))
                if not dt.tzinfo:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc).isoformat()
            except Exception:
                pass
    return datetime.now(timezone.utc).isoformat()

def fingerprint(title: str, link: str) -> str:
    return sha1(f"{title}|{link}".encode("utf-8")).hexdigest()

def deepl_translate_ro(text: str) -> str:
    text = clean(text)
    if not text:
        return ""

    api_key = os.getenv("DEEPL_API_KEY")
    if not api_key:
        print("DeepL: missing API key")
        return text

    for endpoint in [
        "https://api-free.deepl.com/v2/translate",
        "https://api.deepl.com/v2/translate",
    ]:
        try:
            r = requests.post(
                endpoint,
                headers={"Authorization": f"DeepL-Auth-Key {api_key}"},
                data={
                    "text": text,
                    "source_lang": "EN",
                    "target_lang": "RO",
                },
                timeout=20,
            )
            if r.status_code == 200:
                return r.json()["translations"][0]["text"]
            else:
                print(f"DeepL {endpoint} -> {r.status_code}")
        except Exception as e:
            print(f"DeepL exception: {e}")

    return text

# ---------------- MAIN ----------------

def process_sources(sources, kind, items, seen, global_left):
    for src in sources:
        feed = feedparser.parse(src["url"])
        for e in feed.entries[:30]:
            title = clean(getattr(e, "title", ""))
            link = getattr(e, "link", "")
            summary = clean(getattr(e, "summary", ""))

            if not title or not link:
                continue

            blob = f"{title} {summary}"
            if is_negative(blob):
                continue

            if kind == "global" and not is_positive(blob):
                continue

            fp = fingerprint(title, link)
            if fp in seen:
                continue
            seen.add(fp)

            item = {
                "id": fp[:12],
                "title": title,
                "summary": summary[:280],
                "link": link,
                "source": src["name"],
                "published_utc": parse_date(e),
                "kind": kind,
            }

            if kind == "global":
                if global_left <= 0:
                    continue
                global_left -= 1
                item["title_ro"] = deepl_translate_ro(title)
                item["summary_ro"] = deepl_translate_ro(summary[:280])

            items.append(item)

        time.sleep(0.3)

    return global_left

def main():
    items = []
    seen = set()
    global_left = MAX_GLOBAL_PER_RUN

    global_left = process_sources(RSS_RO, "ro", items, seen, global_left)
    process_sources(RSS_GLOBAL, "global", items, seen, global_left)

    items.sort(key=lambda x: x["published_utc"], reverse=True)

    payload = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "count": len(items),
        "items": items[:80],
    }

    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Generated {payload['count']} items")

if __name__ == "__main__":
    main()
