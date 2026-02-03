import json
import re
import time
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path

import feedparser
from dateutil import parser as dtparser

# ---------------- Paths ----------------

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
OUT_JSON = DATA_DIR / "news.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ---------------- RSS SOURCES ----------------

# ROMÂNIA – intră direct, NU se traduc
RSS_RO = [
    {"name": "HotNews", "url": "https://rss.hotnews.ro/"},
    {"name": "Digi24", "url": "https://www.digi24.ro/rss"},
    {"name": "Agerpres", "url": "https://www.agerpres.ro/rss"},
    {"name": "Spotmedia", "url": "https://spotmedia.ro/feed"},
    {"name": "News.ro", "url": "https://www.news.ro/rss"},
]

# GLOBALE – puține, vor fi traduse ulterior
RSS_GLOBAL = [
    {"name": "BBC – Science & Environment", "url": "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml"},
    {"name": "BBC – Health", "url": "https://feeds.bbci.co.uk/news/health/rss.xml"},
    {"name": "Our World in Data", "url": "https://ourworldindata.org/feeds/latest.xml"},
]

# Limită globale / rulare (DeepL Free friendly)
MAX_GLOBAL_PER_RUN = 3

# ---------------- FILTERING ----------------

NEGATIVE_PATTERNS = [
    # EN
    r"\bwar\b", r"\battack\b", r"\bkilled\b", r"\bdead\b", r"\bdeath\b",
    r"\bshooting\b", r"\bterror\b", r"\bexplosion\b", r"\bbomb\b",
    r"\bhostage\b", r"\bcrisis\b", r"\bscandal\b", r"\bfraud\b",
    r"\bcorruption\b", r"\bviolence\b", r"\bmurder\b", r"\bsuicide\b",

    # RO (cu / fără diacritice)
    r"\br(a|ă)zboi\b", r"\batac\b", r"\bucis\b", r"\bomor(a|â)t\b",
    r"\bmor(t|ti|ți)\b", r"\bdeces\b", r"\bcrim(a|ă)\b",
    r"\bviolen(t|ț)(a|ă)\b", r"\bexploz(ie|ii)\b",
    r"\bbomb(a|ă)\b", r"\bteror\b", r"\bostatic\b",
    r"\bcutremur\b", r"\bincendiu\b", r"\baccident\b",
    r"\bfraud(a|ă)\b", r"\bcorup(t|ț)(ie|i(e|ă))\b",
    r"\bsinuc(id|idere)\b",
]

POSITIVE_PATTERNS = [
    # EN
    r"\bbreakthrough\b", r"\bdiscovery\b", r"\bimproves?\b",
    r"\breduces?\b", r"\bsuccess\b", r"\baward\b",
    r"\bprogress\b", r"\brecord\b", r"\bvaccine\b",
    r"\btreatment\b", r"\btrial\b",
    r"\bclean energy\b", r"\bsolar\b", r"\bwind\b",
    r"\bemissions?\b", r"\bconservation\b",
    r"\breforest\b", r"\beducation\b",

    # RO (le păstrăm, dar NU le folosim pentru RO acum)
    r"\bdescoper(ire|it)\b", r"\breu(s|ș)it(a|ă)\b",
    r"\bsucces\b", r"\bpremiu\b", r"\bprogres\b",
    r"\brecord\b", r"\bscade\b", r"\ba sc(a|ă)zut\b",
    r"\bcre(s|ș)te\b", r"\ba crescut\b",
    r"\bvaccin\b", r"\btratament\b", r"\bstudiu\b",
    r"\benergie verde\b", r"\benergie curat(a|ă)\b",
    r"\bsolar\b", r"\beolian\b", r"\bprotejat\b",
    r"\brestaurat\b", r"\beduca(t|ț)ie\b",
]

NEG_RE = re.compile("|".join(NEGATIVE_PATTERNS), re.IGNORECASE)
POS_RE = re.compile("|".join(POSITIVE_PATTERNS), re.IGNORECASE)

# ---------------- HELPERS ----------------

def clean(text: str) -> str:
    text = (text or "").strip()
    return re.sub(r"\s+", " ", text)

def is_negative(text: str) -> bool:
    return bool(NEG_RE.search(text))

def is_positive(text: str) -> bool:
    return bool(POS_RE.search(text))

def parse_date(entry) -> str:
    for key in ("published", "updated", "created"):
        if key in entry and entry.get(key):
            try:
                dt = dtparser.parse(entry.get(key))
                if not dt.tzinfo:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc).isoformat()
            except Exception:
                pass
    return datetime.now(timezone.utc).isoformat()

def fingerprint(title: str, link: str) -> str:
    raw = f"{title}|{link}".encode("utf-8", errors="ignore")
    return sha1(raw).hexdigest()

# ---------------- MAIN ----------------

def process_sources(sources, kind, items, seen, global_left):
    for src in sources:
        try:
            feed = feedparser.parse(src["url"])
            for e in feed.entries[:30]:
                title = clean(getattr(e, "title", ""))
                link = getattr(e, "link", "")
                summary = clean(getattr(e, "summary", ""))

                if not title or not link:
                    continue

                blob = f"{title} {summary}"

                # 1) Mereu filtrăm negativ (RO + Global)
                if is_negative(blob):
                    continue

                # 2) Doar pentru GLOBAL cerem "pozitiv" (RO e prea neutru în titluri)
                if kind != "ro":
                    if not is_positive(blob):
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
                    "kind": kind,  # ro / global
                }

                # limită pentru globale (DeepL free)
                if kind == "global":
                    if global_left <= 0:
                        continue
                    global_left -= 1

                items.append(item)

            time.sleep(0.3)
        except Exception:
            continue

    return global_left

def main():
    items = []
    seen = set()
    global_left = MAX_GLOBAL_PER_RUN

    # RO: multe, doar "negative filter"
    global_left = process_sources(RSS_RO, "ro", items, seen, global_left)

    # Global: puține, negative+positive + limită
    process_sources(RSS_GLOBAL, "global", items, seen, global_left)

    # sort by newest
    def sort_key(x):
        try:
            return dtparser.parse(x["published_utc"])
        except Exception:
            return datetime.now(timezone.utc)

    items.sort(key=sort_key, reverse=True)

    payload = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "count": len(items),
        "items": items[:80],  # poți lăsa 60 dacă vrei
    }

    OUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    print(f"Generated {payload['count']} items -> {OUT_JSON}")

if __name__ == "__main__":
    main()
