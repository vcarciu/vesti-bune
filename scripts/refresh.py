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
    # crude but ok for RSS summaries
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_text(s: str) -> str:
    """
    Lowercase + remove diacritics so "împușc" matches robustly.
    """
    s = (s or "").lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"\s+", " ", s).strip()
    return s


def parse_entry_datetime(entry: Dict[str, Any]) -> Optional[datetime]:
    # feedparser gives structured_time in fields like published_parsed/updated_parsed
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
    # sometimes content[0].value exists
    content = entry.get("content")
    if isinstance(content, list) and content:
        val = content[0].get("value") or ""
        return strip_html(val)
    return ""


def deepl_translate(text: str, target_lang: str = "RO") -> Optional[str]:
    """
    Returns translated text or None if not available.
    Works with DEEPL_API_KEY in env.
    """
    key = os.getenv("DEEPL_API_KEY", "").strip()
    if not key or not text.strip():
        return None

    # Prefer explicit URL if set
    url_env = os.getenv("DEEPL_API_URL", "").strip()
    candidates = [url_env] if url_env else [
        "https://api-free.deepl.com/v2/translate",
        "https://api.deepl.com/v2/translate",
    ]

    headers = {"User-Agent": USER_AGENT}
    data = {
        "auth_key": key,
        "text": text,
        "target_lang": target_lang,
    }

    for url in [u for u in candidates if u]:
        try:
            r = requests.post(url, data=data, headers=headers, timeout=20)
            if r.status_code == 200:
                js = r.json()
                tr = js.get("translations", [])
                if tr:
                    out = tr[0].get("text", "").strip()
                    return out or None
            # If key is not for this endpoint, try next
        except Exception:
            continue

    return None


def score_item(
    section_id: str,
    title: str,
    summary: str,
    filters_cfg: Dict[str, Any]
) -> Tuple[int, bool]:
    """
    Returns (score, allowed).
    allowed=False if hard blacklist triggers.
    """
    thresholds = (filters_cfg.get("thresholds") or {})
    hard_blacklist = filters_cfg.get("hard_blacklist") or []
    scoring_cfg = filters_cfg.get("scoring") or {}
    pos = scoring_cfg.get("positive_keywords") or []
    neg = scoring_cfg.get("negative_keywords") or []
    medical_extra = filters_cfg.get("medical_extra_blacklist") or []

    text = normalize_text(f"{title} {summary}")

    # Hard blacklist (global)
    for w in hard_blacklist:
        if normalize_text(w) in text:
            return (-999, False)

    # Medical extra blacklist
    if section_id == "medical":
        for w in medical_extra:
            if normalize_text(w) in text:
                return (-999, False)

    score = 0

    # Positive points
    for w in pos:
        ww = normalize_text(w)
        if ww and ww in text:
            score += 1

    # Negative points
    for w in neg:
        ww = normalize_text(w)
        if ww and ww in text:
            score -= 1

    # tiny bias: longer, more descriptive summaries tend to be better than empty
    if len(summary.strip()) >= 120:
        score += 1
    if len(summary.strip()) == 0:
        score -= 1

    # section bias
    if section_id in ("medical", "science", "environment"):
        score += 1

    # apply threshold later (caller)
    _ = thresholds.get(section_id, 0)
    return (score, True)


def fetch_rss(url: str) -> feedparser.FeedParserDict:
    # feedparser fetches itself; give it UA via requests? It can, but simplest:
    # feedparser supports request headers via 'agent'
    return feedparser.parse(url, agent=USER_AGENT)


def dedupe_key(link: str, title: str) -> str:
    base = (link or title or "").strip()
    if not base:
        base = hashlib.sha1(f"{link}|{title}".encode("utf-8", errors="ignore")).hexdigest()
    return hashlib.sha1(base.encode("utf-8", errors="ignore")).hexdigest()


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
            url = src.get("url", "").strip()
            if not url:
                continue

            feed = fetch_rss(url)
            for e in feed.entries[:50]:
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

                # threshold check
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

                # Translate non-RO sections (title+summary) if DeepL key exists
                if kind == "global":
                    tr_title = deepl_translate(title) or None
                    tr_sum = deepl_translate(summary) or None
                    if tr_title:
                        item["title_ro"] = tr_title
                    if tr_sum:
                        item["summary_ro"] = tr_sum

                items.append(item)

        # sort newest first
        items.sort(key=lambda x: x.get("published_utc", ""), reverse=True)

        # trim
        items = items[: max_items_map.get(section_id, 20)]
        out[section_id] = items

    return out


def build_joke() -> Dict[str, Any]:
    """
    Placeholder for pasul 3/4:
    we'll add data/jokes_ro.txt and deterministic daily pick.
    For now return empty.
    """
    return {}


def build_photos_placeholder() -> List[Dict[str, Any]]:
    """
    Placeholder for pasul 3:
    RSS parsing for images differs by feed. We'll implement next step.
    For now, return empty list.
    """
    return []


def build_satire_placeholder(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Placeholder for pasul 3/4:
    We'll either use a real RSS if provided, or do simple homepage scraping.
    For now empty list.
    """
    satire = cfg.get("satire") or {}
    if not satire.get("enabled", False):
        return []
    return []


def flatten_items(sections: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """
    Keep your current site working:
    - take romania + global sections and make a flat list.
    We'll keep order newest-first overall.
    """
    all_items: List[Dict[str, Any]] = []
    for sec_items in sections.values():
        all_items.extend(sec_items)

    all_items.sort(key=lambda x: x.get("published_utc", ""), reverse=True)

    # don't bloat the page
    return all_items[:60]


def main() -> None:
    if not os.path.exists(CONFIG_PATH):
        raise SystemExit(f"Missing config: {CONFIG_PATH}")

    cfg = load_yaml(CONFIG_PATH)

    safe_mkdir(os.path.dirname(OUT_PATH))

    sections = build_sections(cfg)

    # placeholders for now (pasul 3/4)
    sections["photos"] = build_photos_placeholder()
    sections["joke"] = [build_joke()] if build_joke() else []
    sections["satire"] = build_satire_placeholder(cfg)

    flat_items = flatten_items(sections)

    payload = {
        "generated_utc": utc_now_iso(),
        "count": len(flat_items),
        "items": flat_items,       # backwards compat (UI curent)
        "sections": sections,      # noul format (pasul 3)
    }

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"Wrote {OUT_PATH} with {len(flat_items)} items; sections: {', '.join(sections.keys())}")


if __name__ == "__main__":
    main()
