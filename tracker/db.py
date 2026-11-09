"""SQLite хранилище – всичко, което проверката вижда, се пази тук."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Пътуванията от travels.yaml
CREATE TABLE IF NOT EXISTS travels (
    slug        TEXT PRIMARY KEY,          -- от линка: /travels/<slug>/
    url         TEXT NOT NULL,
    name        TEXT,                      -- твое име от YAML (по избор)
    title       TEXT,                      -- заглавие от сайта
    image       TEXT,
    description TEXT,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Всяка обявена дата (група) на пътуване
CREATE TABLE IF NOT EXISTS departures (
    id          TEXT PRIMARY KEY,          -- <slug>/<start>_<end>_<вариант>
    travel_slug TEXT NOT NULL REFERENCES travels(slug),
    start_date  TEXT NOT NULL,             -- ISO
    end_date    TEXT NOT NULL,
    name        TEXT NOT NULL,             -- вариант на програмата
    post_id     TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    gone_since  TEXT                       -- кога е изчезнала от страницата (NULL = още е там)
);
CREATE INDEX IF NOT EXISTS idx_departures_travel ON departures(travel_slug, start_date);

-- Какво е видяла всяка проверка за всяка дата
CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    departure_id  TEXT NOT NULL REFERENCES departures(id),
    checked_at    TEXT NOT NULL,           -- ISO 8601, UTC
    price         REAL,
    currency      TEXT,
    action        TEXT,                    -- ЗАПИШИ СЕ | РЕЗЕРВИ
    seats         INTEGER,                 -- брой свободни места (NULL = не е посочен)
    seats_text    TEXT,
    available     INTEGER,                 -- 1 има места, 0 няма, NULL неизвестно
    combo         TEXT,                    -- NULL | мъж | жена | any
    combo_text    TEXT,
    badges        TEXT,                    -- JSON списък
    heading       TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshots_dep_time ON snapshots(departure_id, checked_at);

-- Промени, забелязани между две проверки (това е и дневникът на сайта)
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    departure_id  TEXT NOT NULL REFERENCES departures(id),
    travel_slug   TEXT NOT NULL,
    kind          TEXT NOT NULL,           -- new_departure | price_change | combo_appeared | combo_gone |
                                           -- seats_change | sold_out | available_again | departure_gone
    checked_at    TEXT NOT NULL,
    detail        TEXT,                    -- JSON
    notified      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_time ON events(checked_at);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    ok_count     INTEGER NOT NULL DEFAULT 0,
    fail_count   INTEGER NOT NULL DEFAULT 0,
    errors       TEXT                      -- JSON: [{slug, url, error}]
);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path) if str(path) != ":memory:" else path
        if isinstance(self.path, Path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        # Класически журнал – без -wal файлове, за да е един файл в git
        self.conn.execute("PRAGMA journal_mode=DELETE")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    # ---- пътувания ------------------------------------------------------

    def sync_travels(self, travels) -> None:
        now = utcnow_iso()
        slugs = []
        for t in travels:
            slugs.append(t.slug)
            self.conn.execute(
                """
                INSERT INTO travels(slug, url, name, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(slug) DO UPDATE SET
                    url = excluded.url,
                    name = excluded.name,
                    active = excluded.active,
                    updated_at = excluded.updated_at
                """,
                (t.slug, t.url, t.name, int(t.active), now, now),
            )
        if slugs:
            marks = ",".join("?" * len(slugs))
            self.conn.execute(
                f"UPDATE travels SET active = 0, updated_at = ? WHERE active = 1 AND slug NOT IN ({marks})",
                (now, *slugs),
            )
        self.conn.commit()

    def update_travel_details(self, slug: str, *, title=None, image=None, description=None) -> None:
        self.conn.execute(
            """
            UPDATE travels SET title = COALESCE(?, title), image = COALESCE(?, image),
                               description = COALESCE(?, description), updated_at = ?
            WHERE slug = ?
            """,
            (title, image, description, utcnow_iso(), slug),
        )

    def travels(self, active_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM travels"
        if active_only:
            sql += " WHERE active = 1"
        return self.conn.execute(sql + " ORDER BY COALESCE(name, title, slug)").fetchall()

    def travel(self, slug: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM travels WHERE slug = ?", (slug,)).fetchone()

    # ---- дати -----------------------------------------------------------

    @staticmethod
    def departure_id(slug: str, key: str) -> str:
        return f"{slug}/{key}"

    def upsert_departure(self, slug: str, dep, checked_at: str) -> tuple[str, bool]:
        """Връща (id, is_new)."""
        dep_id = self.departure_id(slug, dep.key)
        row = self.conn.execute("SELECT id, gone_since FROM departures WHERE id = ?", (dep_id,)).fetchone()
        if row is None:
            self.conn.execute(
                """
                INSERT INTO departures(id, travel_slug, start_date, end_date, name, post_id, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (dep_id, slug, dep.start_date, dep.end_date, dep.name, dep.post_id, checked_at, checked_at),
            )
            return dep_id, True
        self.conn.execute(
            "UPDATE departures SET last_seen = ?, post_id = COALESCE(?, post_id), gone_since = NULL WHERE id = ?",
            (checked_at, dep.post_id, dep_id),
        )
        return dep_id, False

    def mark_gone(self, slug: str, seen_ids: set[str], checked_at: str) -> list[sqlite3.Row]:
        """Дати на пътуването, които вече ги няма на страницата. Връща новоизчезналите."""
        rows = self.conn.execute(
            "SELECT * FROM departures WHERE travel_slug = ? AND gone_since IS NULL", (slug,)
        ).fetchall()
        gone = [r for r in rows if r["id"] not in seen_ids]
        for r in gone:
            self.conn.execute("UPDATE departures SET gone_since = ? WHERE id = ?", (checked_at, r["id"]))
        return gone

    def departures(self, slug: str | None = None, include_gone: bool = True) -> list[sqlite3.Row]:
        sql = "SELECT * FROM departures"
        conds, params = [], []
        if slug:
            conds.append("travel_slug = ?")
            params.append(slug)
        if not include_gone:
            conds.append("gone_since IS NULL")
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        return self.conn.execute(sql + " ORDER BY start_date, end_date, name", params).fetchall()

    # ---- снимки ---------------------------------------------------------

    def last_snapshot(self, dep_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM snapshots WHERE departure_id = ? ORDER BY checked_at DESC, id DESC LIMIT 1",
            (dep_id,),
        ).fetchone()

    def add_snapshot(self, dep_id: str, dep, checked_at: str) -> None:
        self.conn.execute(
            """
            INSERT INTO snapshots(departure_id, checked_at, price, currency, action, seats, seats_text,
                                  available, combo, combo_text, badges, heading)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dep_id, checked_at, dep.price, dep.currency, dep.action, dep.seats, dep.seats_text,
                None if dep.available is None else int(dep.available),
                dep.combo, dep.combo_text, json.dumps(dep.badges, ensure_ascii=False), dep.heading,
            ),
        )

    def snapshots(self, dep_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM snapshots WHERE departure_id = ? ORDER BY checked_at, id", (dep_id,)
        ).fetchall()

    def price_stats(self, dep_id: str) -> sqlite3.Row:
        return self.conn.execute(
            "SELECT MIN(price) AS lo, MAX(price) AS hi, COUNT(*) AS n FROM snapshots "
            "WHERE departure_id = ? AND price IS NOT NULL", (dep_id,)
        ).fetchone()

    # ---- събития --------------------------------------------------------

    def add_event(self, dep_id: str, slug: str, kind: str, checked_at: str, detail: dict | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO events(departure_id, travel_slug, kind, checked_at, detail) VALUES (?, ?, ?, ?, ?)",
            (dep_id, slug, kind, checked_at, json.dumps(detail or {}, ensure_ascii=False)),
        )
        return cur.lastrowid

    def mark_notified(self, event_ids: list[int]) -> None:
        if event_ids:
            marks = ",".join("?" * len(event_ids))
            self.conn.execute(f"UPDATE events SET notified = 1 WHERE id IN ({marks})", event_ids)

    def events(self, limit: int = 300) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM events ORDER BY checked_at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()

    def last_event(self, dep_id: str, kind: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM events WHERE departure_id = ? AND kind = ? ORDER BY checked_at DESC, id DESC LIMIT 1",
            (dep_id, kind),
        ).fetchone()

    # ---- изпълнения -----------------------------------------------------

    def start_run(self) -> int:
        cur = self.conn.execute("INSERT INTO runs(started_at) VALUES (?)", (utcnow_iso(),))
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, ok: int, errors: list[dict]) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, ok_count = ?, fail_count = ?, errors = ? WHERE id = ?",
            (utcnow_iso(), ok, len(errors), json.dumps(errors, ensure_ascii=False), run_id),
        )
        self.conn.commit()

    def last_run(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def run_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM runs WHERE finished_at IS NOT NULL").fetchone()[0]
