"""Събира всичко, което сайтът показва, в един JSON (data/summary.json).

Сайтът е статичен (GitHub Pages) и чете само този файл. Пълната история
остава в SQLite; тук снимките на всяка дата се сгъват до отсечки
„от – до – състояние“, за да е файлът малък и графиките бързи.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from .changes import KIND_LABELS, combo_matches
from .db import Database


def _segments(snapshots) -> list[dict]:
    """Последователни еднакви състояния -> една отсечка."""
    segs: list[dict] = []
    for s in snapshots:
        state = (s["price"], s["seats"], s["seats_text"], s["available"], s["combo"], s["action"], s["badges"])
        if segs and segs[-1]["_state"] == state:
            segs[-1]["to"] = s["checked_at"]
            segs[-1]["n"] += 1
            continue
        segs.append({"_state": state, "from": s["checked_at"], "to": s["checked_at"], "n": 1,
                     "price": s["price"], "seats": s["seats"], "seats_text": s["seats_text"],
                     "available": None if s["available"] is None else bool(s["available"]),
                     "combo": s["combo"], "action": s["action"],
                     "badges": json.loads(s["badges"]) if s["badges"] else []})
    for seg in segs:
        seg.pop("_state")
    return segs


def _days_until(iso: str, today: date) -> int:
    try:
        return (date.fromisoformat(iso) - today).days
    except ValueError:
        return 0


def departure_payload(db: Database, dep_row, wanted: str, today: date) -> dict:
    snaps = db.snapshots(dep_row["id"])
    last = snaps[-1] if snaps else None
    prices = [s["price"] for s in snaps if s["price"] is not None]
    first_price = prices[0] if prices else None
    cur_price = last["price"] if last else None
    combo = last["combo"] if last else None
    combo_since = None
    if combo:
        # откога стои сегашната бележка за комбинация без прекъсване
        for s in reversed(snaps):
            if s["combo"] != combo:
                break
            combo_since = s["checked_at"]
    return {
        "id": dep_row["id"],
        "travel": dep_row["travel_slug"],
        "start": dep_row["start_date"],
        "end": dep_row["end_date"],
        "days": _days_until(dep_row["end_date"], date.fromisoformat(dep_row["start_date"])) + 1,
        "days_until": _days_until(dep_row["start_date"], today),
        "name": dep_row["name"],
        "first_seen": dep_row["first_seen"],
        "last_seen": dep_row["last_seen"],
        "gone_since": dep_row["gone_since"],
        "price": cur_price,
        "currency": last["currency"] if last else None,
        "price_first": first_price,
        "price_min": min(prices) if prices else None,
        "price_max": max(prices) if prices else None,
        "price_changes": sum(1 for a, b in zip(prices, prices[1:]) if a != b),
        "action": last["action"] if last else None,
        "seats": last["seats"] if last else None,
        "seats_text": last["seats_text"] if last else None,
        "available": None if not last or last["available"] is None else bool(last["available"]),
        "combo": combo,
        "combo_text": last["combo_text"] if last else None,
        "combo_match": combo_matches(combo, wanted),
        "combo_since": combo_since,
        "badges": json.loads(last["badges"]) if last and last["badges"] else [],
        "heading": last["heading"] if last else None,
        "checks": len(snaps),
        "history": _segments(snaps),
    }


def export_summary(db: Database, settings, out_path: Path | None, dashboard_url: str | None = None) -> dict:
    today = datetime.now(timezone.utc).date()
    wanted = settings.combination
    travels = []
    for t in db.travels():
        deps = [departure_payload(db, d, wanted, today) for d in db.departures(t["slug"])]
        travels.append({
            "slug": t["slug"], "url": t["url"], "name": t["name"],   # твоето име от YAML (може да липсва)
            "title": t["title"], "image": t["image"], "description": t["description"],
            "active": bool(t["active"]), "departures": deps,
        })

    dep_index = {d["id"]: d for t in travels for d in t["departures"]}
    travel_index = {t["slug"]: t for t in travels}
    events = []
    for e in db.events(limit=400):
        d = dep_index.get(e["departure_id"])
        t = travel_index.get(e["travel_slug"])
        events.append({
            "id": e["id"], "kind": e["kind"], "label": KIND_LABELS.get(e["kind"], e["kind"]),
            "at": e["checked_at"], "notified": bool(e["notified"]),
            "detail": json.loads(e["detail"]) if e["detail"] else {},
            "departure": e["departure_id"], "travel": e["travel_slug"],
            "travel_name": (t["name"] or t["title"] or t["slug"]) if t else e["travel_slug"],
            "travel_url": t["url"] if t else None,
            "dep_name": d["name"] if d else None, "start": d["start"] if d else None, "end": d["end"] if d else None,
        })

    live = [d for d in dep_index.values() if not d["gone_since"] and d["days_until"] >= 0]
    highlights = {
        "combo_matches": [d["id"] for d in live if d["combo_match"] and d["available"] is not False],
        "other_combos": [d["id"] for d in live if d["combo"] and not d["combo_match"] and d["available"] is not False],
        "last_seats": [d["id"] for d in live
                       if settings.notify_last_seats and d["seats"] is not None
                       and 0 < d["seats"] <= settings.notify_last_seats],
    }

    last_run = db.last_run()
    summary = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "settings": {"combination": wanted, "notify_last_seats": settings.notify_last_seats,
                     "notify_any_combination": settings.notify_any_combination},
        "dashboard_url": dashboard_url,
        "last_run": dict(last_run) if last_run else None,
        "runs": db.run_count(),
        "travels": travels,
        "events": events,
        "highlights": highlights,
    }
    if last_run and last_run["errors"]:
        summary["last_run"]["errors"] = json.loads(last_run["errors"])

    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return summary
