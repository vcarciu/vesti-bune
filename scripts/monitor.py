import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


ROOT = Path(__file__).resolve().parents[1]
NEWS_PATH = ROOT / "data" / "news.json"
REPORT_PATH = ROOT / "data" / "monitor_report.json"
HISTORY_PATH = ROOT / "data" / "monitor_history.json"


def read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    raw = s.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def is_fresh_24h(iso: str, now: datetime) -> bool:
    dt = parse_iso(iso or "")
    if not dt:
        return False
    return (now - dt).total_seconds() <= 24 * 3600


def analyze(news: Dict[str, Any]) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    generated = parse_iso(news.get("generated_utc", ""))
    gen_age_min = int((now - generated).total_seconds() // 60) if generated else None

    sections = news.get("sections") or {}
    mix_items: List[Dict[str, Any]] = list(news.get("mix_items") or [])
    all_items: List[Dict[str, Any]] = []
    for sec in ("romania", "medical", "science", "environment"):
        all_items.extend(list(sections.get(sec) or []))

    section_counts = {k: len(list(v or [])) for k, v in sections.items()}
    fresh_24h_mix = sum(1 for it in mix_items if is_fresh_24h(it.get("published_utc", ""), now))
    fresh_24h_all = sum(1 for it in all_items if is_fresh_24h(it.get("published_utc", ""), now))

    source_counts = Counter((it.get("source") or "Unknown").strip() for it in mix_items)
    top_sources = [{"source": s, "count": c} for s, c in source_counts.most_common(10)]

    title_counts = Counter((it.get("title") or "").strip().lower() for it in mix_items if (it.get("title") or "").strip())
    duplicate_titles = sum(1 for _, c in title_counts.items() if c > 1)

    issues: List[str] = []
    if not mix_items:
        issues.append("mix_items empty")
    if fresh_24h_mix == 0:
        issues.append("no fresh items in last 24h (mix)")
    if gen_age_min is not None and gen_age_min > 180:
        issues.append("news.json older than 180 minutes")
    if section_counts.get("romania", 0) < 8:
        issues.append("low Romania count")

    status = "ok" if not issues else "warn"
    return {
        "status": status,
        "checked_utc": now.replace(microsecond=0).isoformat(),
        "generated_utc": news.get("generated_utc"),
        "generated_age_minutes": gen_age_min,
        "section_counts": section_counts,
        "mix_count": len(mix_items),
        "fresh_24h_mix": fresh_24h_mix,
        "fresh_24h_all_sections": fresh_24h_all,
        "duplicate_titles_in_mix": duplicate_titles,
        "top_sources_mix": top_sources,
        "issues": issues,
    }


def update_history(report: Dict[str, Any]) -> Dict[str, Any]:
    hist = read_json(HISTORY_PATH)
    entries = list(hist.get("entries") or [])
    entries.append({
        "checked_utc": report.get("checked_utc"),
        "status": report.get("status"),
        "mix_count": report.get("mix_count"),
        "fresh_24h_mix": report.get("fresh_24h_mix"),
        "generated_age_minutes": report.get("generated_age_minutes"),
        "issues": report.get("issues") or [],
    })
    entries = entries[-240:]
    return {"entries": entries}


def main() -> None:
    news = read_json(NEWS_PATH)
    report = analyze(news)
    history = update_history(report)
    write_json(REPORT_PATH, report)
    write_json(HISTORY_PATH, history)
    print(f"[monitor] status={report['status']} mix={report['mix_count']} fresh24h={report['fresh_24h_mix']}")


if __name__ == "__main__":
    main()

