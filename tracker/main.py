"""Ежедневна проверка на пътуванията.

    python -m tracker                 # нормално изпълнение
    python -m tracker --dry-run       # само показва, не пише в базата и не праща Slack
    python -m tracker --no-notify     # записва, но не праща Slack
    python -m tracker export          # само пресмята data/summary.json от базата
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import time
from pathlib import Path

from .changes import (COMBO_APPEARED, DEPARTURE_GONE, combo_matches, detect_changes, wants_alert, wants_digest)
from .config import ConfigError, load_config
from .db import Database, utcnow_iso
from .export import export_summary
from .fetch import FetchError, Fetcher
from .notify import (SlackNotifier, combo_alert_message, combo_phrase, digest_message, errors_message, fmt_price, fmt_range,
                     fmt_seats, reminder_message)
from .parser import ParseError, parse_travel

ROOT = Path(__file__).resolve().parent.parent
DEBUG_DIR = ROOT / "debug"
IN_ACTIONS = os.getenv("GITHUB_ACTIONS") == "true"


def log(level: str, msg: str) -> None:
    """В GitHub Actions ползва анотации (::notice / ::warning / ::error)."""
    icons = {"notice": "✅", "warning": "⚠️", "error": "❌", "info": "  "}
    if IN_ACTIONS and level in ("notice", "warning", "error"):
        print(f"::{level} ::{msg}")
    else:
        print(f"{icons.get(level, '')} {msg}")


def save_debug_html(slug: str, html: str) -> None:
    DEBUG_DIR.mkdir(exist_ok=True)
    (DEBUG_DIR / f"{slug}.html").write_text(html, encoding="utf-8")
    log("info", f"HTML-ът е записан в debug/{slug}.html")


def write_step_summary(rows: list[dict], errors: list[dict], alerts: int) -> None:
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if not path:
        return
    out = ["## Пътувания днес", ""]
    if alerts:
        out += [f"🧑‍🤝‍🧑 **{alerts} нови възможности за комбинация** (пратени в Slack)", ""]
    out += ["| Пътуване | Дати | Вариант | Цена | Места | Комбинация | Промени |", "|---|---|---|---:|---|---|---|"]
    for r in rows:
        out.append(f"| [{r['travel']}]({r['url']}) | {r['dates']} | {r['variant']} | {r['price']} | {r['seats']} "
                   f"| {r['combo'] or '—'} | {r['changes'] or '—'} |")
    if errors:
        out += ["", "### Грешки", ""]
        out += [f"- [{e['name'] or e['slug']}]({e['url']}): {e['error']}" for e in errors]
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


def run(config_path: Path, db_path: Path, summary_path: Path | None, *, dry_run: bool, notify: bool,
        fetcher: Fetcher | None = None) -> int:
    try:
        config = load_config(config_path)
    except ConfigError as e:
        log("error", str(e).replace("\n", " "))
        return 2

    travels = config.active_travels
    if not travels:
        log("warning", "Няма активни пътувания в travels.yaml.")
        return 0
    settings = config.settings

    db = Database(db_path if not dry_run else ":memory:")
    db.sync_travels(config.travels)
    run_id = db.start_run()

    slack = SlackNotifier()
    notify = notify and not dry_run
    if notify and not slack.mode:
        log("warning", "Slack не е настроен (липсват SLACK_BOT_TOKEN/SLACK_CHANNEL_ID "
                       "или SLACK_WEBHOOK_URL) – известията се пропускат.")
    dashboard_url = os.getenv("DASHBOARD_URL", "").strip() or None

    fetcher = fetcher or Fetcher(log=lambda m: log("info", m))
    checked_at = utcnow_iso()
    rows: list[dict] = []
    errors: list[dict] = []
    alerts_sent = 0
    digest: dict[str, list[dict]] = {}
    current_matches: list[dict] = []
    notified_event_ids: list[int] = []

    for i, t in enumerate(travels):
        if i:
            time.sleep(settings.request_delay + random.uniform(0, 1))
        html = None
        try:
            html = fetcher.get(t.url)
            page = parse_travel(html, t.url)
        except (FetchError, ParseError) as e:
            if html is not None:
                save_debug_html(t.slug, html)
            row = db.travel(t.slug)
            errors.append({"slug": t.slug, "url": t.url, "name": t.name or (row["title"] if row else None),
                           "error": str(e)})
            log("error", f"{t.slug} – {e}")
            continue
        except Exception as e:  # noqa: BLE001 – не спираме заради едно пътуване
            if html is not None:
                save_debug_html(t.slug, html)
            errors.append({"slug": t.slug, "url": t.url, "name": t.name, "error": f"{type(e).__name__}: {e}"})
            log("error", f"{t.slug} – неочаквана грешка: {e!r}")
            continue

        db.update_travel_details(t.slug, title=page.title, image=page.image, description=page.description)
        travel_name = t.name or page.title or t.slug
        # Първа проверка на това пътуване? Тогава всичко е „ново“ и не заливаме Slack с него.
        baseline = not db.departures(t.slug)

        seen_ids: set[str] = set()
        for dep in page.departures:
            dep_id, is_new = db.upsert_departure(t.slug, dep, checked_at)
            seen_ids.add(dep_id)
            prev = db.last_snapshot(dep_id)
            stats = db.price_stats(dep_id)
            changes = detect_changes(prev, dep, is_new=is_new)
            db.add_snapshot(dep_id, dep, checked_at)

            change_labels = []
            for ch in changes:
                event_id = db.add_event(dep_id, t.slug, ch.kind, checked_at, {**ch.detail, "baseline": baseline})
                change_labels.append(ch.kind)
                if wants_alert(ch, settings):
                    if notify:
                        text, blocks = combo_alert_message(
                            travel_name=travel_name, travel_url=t.url, dep=dep, change_detail=ch.detail,
                            dashboard_url=dashboard_url,
                            price_history={"min": stats["lo"], "max": stats["hi"]} if stats and stats["n"] else None)
                        if slack.send(text, blocks):
                            notified_event_ids.append(event_id)
                    alerts_sent += 1
                    log("notice", f"🧑‍🤝‍🧑 {travel_name}: {dep.name} {fmt_range(dep.start_date, dep.end_date)} – "
                                  f"{combo_phrase(ch.detail.get('combo'))}!")
                elif wants_digest(ch, settings, baseline=baseline):
                    digest.setdefault(travel_name, []).append(
                        {"kind": ch.kind, "detail": ch.detail, "dep": dep.to_dict(), "travel_url": t.url,
                         "event_id": event_id})

            if combo_matches(dep.combo, settings.combination) and dep.available is not False:
                current_matches.append({"travel_name": travel_name, "travel_url": t.url, "dep": dep.to_dict()})

            rows.append({"travel": travel_name, "url": t.url, "dates": fmt_range(dep.start_date, dep.end_date),
                         "variant": dep.name, "price": fmt_price(dep.price, dep.currency),
                         "seats": fmt_seats(dep.seats, dep.seats_text, dep.available),
                         "combo": dep.combo_text, "changes": ", ".join(change_labels)})
            combo = f" · комбинация с {dep.combo}" if dep.combo else ""
            log("info", f"{travel_name}: {dep.name} {fmt_range(dep.start_date, dep.end_date)} – "
                        f"{fmt_price(dep.price, dep.currency)}, {fmt_seats(dep.seats, dep.seats_text, dep.available)}{combo}")

        for gone in db.mark_gone(t.slug, seen_ids, checked_at):
            db.add_event(gone["id"], t.slug, DEPARTURE_GONE, checked_at,
                         {"start": gone["start_date"], "end": gone["end_date"], "name": gone["name"]})
            log("info", f"{travel_name}: датата {gone['name']} {fmt_range(gone['start_date'], gone['end_date'])} "
                        f"вече не е на страницата")

        if not page.departures:
            log("warning", f"{travel_name}: на страницата няма обявени дати.")
        else:
            log("notice", f"{travel_name}: {len(page.departures)} дати" + (" (първа проверка)" if baseline else ""))
        db.conn.commit()

    if digest and notify:
        text, blocks = digest_message(digest, dashboard_url)
        if slack.send(text, blocks):
            notified_event_ids += [i["event_id"] for items in digest.values() for i in items]
    elif digest:
        n = sum(len(v) for v in digest.values())
        log("info", f"📋 {n} промени (без известие)")

    if settings.repeat_alerts_daily and current_matches and notify and not alerts_sent:
        text, blocks = reminder_message(current_matches, dashboard_url)
        slack.send(text, blocks)

    if errors and notify and settings.notify_on_errors:
        text, blocks = errors_message(errors, len(travels))
        slack.send(text, blocks)

    db.mark_notified(notified_event_ids)
    db.finish_run(run_id, ok=len(travels) - len(errors), errors=errors)

    if not dry_run and summary_path is not None:
        export_summary(db, settings, summary_path, dashboard_url)
        log("info", f"Сайтът: {summary_path}")
    db.close()

    write_step_summary(rows, errors, alerts_sent)
    print(f"\nГотово: {len(travels) - len(errors)} пътувания прочетени, {len(rows)} дати, "
          f"{alerts_sent} известия за комбинация, {len(errors)} с грешка.")

    # Червено в Actions само ако нищо не е минало – единични счупени линкове
    # се виждат като предупреждения и в Slack.
    return 1 if errors and len(errors) == len(travels) else 0


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Следене на групови пътувания")
    ap.add_argument("command", nargs="?", choices=["check", "export"], default="check")
    ap.add_argument("--config", default=str(ROOT / "travels.yaml"))
    ap.add_argument("--db", default=str(ROOT / "data" / "travels.db"))
    ap.add_argument("--out", default=str(ROOT / "data" / "summary.json"), help="къде да запише summary.json")
    ap.add_argument("--dry-run", action="store_true", help="без запис в базата и без Slack")
    ap.add_argument("--no-notify", action="store_true", help="без Slack")
    args = ap.parse_args(argv)

    if args.command == "export":
        try:
            config = load_config(args.config)
        except ConfigError as e:
            log("error", str(e).replace("\n", " "))
            sys.exit(2)
        db = Database(args.db)
        export_summary(db, config.settings, Path(args.out), os.getenv("DASHBOARD_URL", "").strip() or None)
        db.close()
        print(f"Записано: {args.out}")
        return

    sys.exit(run(Path(args.config), Path(args.db), Path(args.out), dry_run=args.dry_run, notify=not args.no_notify))


if __name__ == "__main__":
    main()
