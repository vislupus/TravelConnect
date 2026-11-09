"""Цялостен цикъл: две проверки върху фикстурите с промени между тях."""
import json
import sqlite3

import pytest

from tracker import main as tracker_main
from tracker.db import Database

URL_JP = "https://cedadventures.com/travels/yaponia/"
URL_NA = "https://cedadventures.com/travels/yuar-namibiya/"
URL_NZ = "https://cedadventures.com/travels/nova-zelandiya-samoa/"

CONFIG = f"""
settings:
  combination: мъж
  request_delay: 0
travels:
  - {URL_JP}
  - url: {URL_NA}
    name: Намибия
  - {URL_NZ}
"""


class SlackSpy:
    mode = "bot"

    def __init__(self):
        self.messages = []

    def send(self, text, blocks=None):
        self.messages.append((text, blocks))
        return True


@pytest.fixture
def env(tmp_path, monkeypatch, fixture_html, fake_fetcher):
    cfg = tmp_path / "travels.yaml"
    cfg.write_text(CONFIG, encoding="utf-8")
    db_path = tmp_path / "travels.db"
    out = tmp_path / "summary.json"
    spy = SlackSpy()
    monkeypatch.setattr(tracker_main, "SlackNotifier", lambda: spy)
    monkeypatch.setenv("DASHBOARD_URL", "https://example.github.io/tc/")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    pages = {URL_JP: fixture_html("yaponia"), URL_NA: fixture_html("yuar-namibiya"),
             URL_NZ: fixture_html("nova-zelandiya-samoa")}

    def run(pages_now):
        return tracker_main.run(cfg, db_path, out, dry_run=False, notify=True, fetcher=fake_fetcher(pages_now))
    return {"run": run, "pages": pages, "db": db_path, "out": out, "spy": spy}


def test_first_run_is_baseline_but_alerts_on_matching_combo(env):
    code = env["run"](env["pages"])
    assert code == 0
    # Само комбинациите, които пасват (Нова Зеландия – без посочен пол), са пратени;
    # „нови дати“ от първата проверка не заливат Slack.
    texts = [t for t, _ in env["spy"].messages]
    assert len(texts) == 2 and all("комбиниране" in t for t in texts)
    assert all("Нова Зеландия" in t for t in texts)
    assert all("#reservation-form" in t for t in texts)

    conn = sqlite3.connect(env["db"])
    assert conn.execute("SELECT COUNT(*) FROM departures").fetchone()[0] == 16
    assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 16
    kinds = dict(conn.execute("SELECT kind, COUNT(*) FROM events GROUP BY kind").fetchall())
    assert kinds == {"new_departure": 16, "combo_appeared": 4}
    assert conn.execute("SELECT COUNT(*) FROM events WHERE notified = 1").fetchone()[0] == 2

    summary = json.loads(env["out"].read_text(encoding="utf-8"))
    assert summary["settings"]["combination"] == "мъж"
    assert len(summary["travels"]) == 3
    assert len(summary["highlights"]["combo_matches"]) == 2
    assert len(summary["highlights"]["other_combos"]) == 2    # жена (Япония, Намибия)
    assert summary["highlights"]["last_seats"]                 # Япония Сакура, 1 място
    nam = next(t for t in summary["travels"] if t["slug"] == "yuar-namibiya")
    assert nam["name"] == "Намибия" and nam["title"].startswith("Намибия и ЮАР")


def test_second_run_without_changes_is_silent(env):
    env["run"](env["pages"])
    env["spy"].messages.clear()
    env["run"](env["pages"])
    assert env["spy"].messages == []
    conn = sqlite3.connect(env["db"])
    assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 32
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 20  # нищо ново


def test_changes_between_runs(env):
    env["run"](env["pages"])
    env["spy"].messages.clear()

    jp = env["pages"][URL_JP]
    # 1) комбинация с жена -> с мъж (Япония Сакура 08.04)
    jp = jp.replace("комбинация с жена", "комбинация с мъж")
    # 2) цена 3930 -> 3850 за датата 21.03 (в мрежата и във формата)
    jp = jp.replace("21.03.2027 - 04.04.2027 &gt;&gt; ЯПОНИЯ &gt;&gt; 3930 EUR", "21.03.2027 - 04.04.2027 &gt;&gt; ЯПОНИЯ &gt;&gt; 3850 EUR")
    # 3) 4 свободни места -> 2
    jp = jp.replace("4 СВОБОДНИ МЕСТА", "2 СВОБОДНИ МЕСТА")
    na = env["pages"][URL_NA]
    # 4) в Намибия една дата изчезва
    na = na.replace("14.09.2027 - 25.09.2027", "14.09.2027 - 26.09.2027")
    pages = {**env["pages"], URL_JP: jp, URL_NA: na}

    code = env["run"](pages)
    assert code == 0
    msgs = env["spy"].messages
    # едно силно известие (мъж) + едно общо съобщение с останалите промени
    alerts = [t for t, _ in msgs if "мъж за комбиниране" in t]
    digests = [t for t, _ in msgs if t.startswith("📋")]
    assert len(alerts) == 1 and "ЯПОНИЯ САКУРА" in alerts[0] and "3 930 €" in alerts[0]
    assert len(digests) == 1
    digest_blocks = json.dumps([b for _, b in msgs if b and any("📋" in json.dumps(x, ensure_ascii=False) for x in b)], ensure_ascii=False)
    assert "3 850 €" in digest_blocks            # цената
    assert "2 свободни места" in digest_blocks   # последни места
    assert "Нова дата" in digest_blocks          # 14.09 – 26.09 в Намибия
    assert len(msgs) == 2

    conn = sqlite3.connect(env["db"])
    kinds = dict(conn.execute(
        "SELECT kind, COUNT(*) FROM events WHERE checked_at = (SELECT MAX(checked_at) FROM events) GROUP BY kind"
    ).fetchall())
    assert kinds == {"combo_appeared": 1, "price_change": 1, "seats_change": 1,
                     "new_departure": 1, "departure_gone": 1}
    gone = conn.execute("SELECT name, start_date FROM departures WHERE gone_since IS NOT NULL").fetchall()
    assert gone == [("ЮАР И НАМИБИЯ", "2027-09-14")]

    summary = json.loads(env["out"].read_text(encoding="utf-8"))
    sakura = next(d for t in summary["travels"] for d in t["departures"] if d["start"] == "2027-04-08")
    assert sakura["combo"] == "мъж" and sakura["combo_match"] is True
    assert len(sakura["history"]) == 2
    cheaper = next(d for t in summary["travels"] for d in t["departures"] if d["start"] == "2027-03-21" and d["travel"] == "yaponia")
    assert cheaper["price"] == 3850 and cheaper["price_first"] == 3930 and cheaper["price_changes"] == 1
    assert cheaper["price_min"] == 3850 and cheaper["price_max"] == 3930

    # трета проверка със същите страници – тишина
    env["spy"].messages.clear()
    env["run"](pages)
    assert env["spy"].messages == []


def test_fetch_error_is_reported(env):
    pages = dict(env["pages"])
    del pages[URL_NA]
    code = env["run"](pages)
    assert code == 0  # останалите минаха
    errs = [t for t, _ in env["spy"].messages if t.startswith("⚠️")]
    assert len(errs) == 1 and "1 от 3" in errs[0]
    db = Database(env["db"])
    last = db.last_run()
    assert last["fail_count"] == 1 and "404" in last["errors"]
    db.close()


def test_all_fail_returns_1(env):
    assert env["run"]({}) == 1


def test_dry_run_writes_nothing(env):
    code = tracker_main.run(env["db"].parent / "travels.yaml", env["db"], env["out"], dry_run=True, notify=True,
                            fetcher=__import__("tests.conftest", fromlist=["FakeFetcher"]).FakeFetcher(env["pages"]))
    assert code == 0
    assert not env["db"].exists() and not env["out"].exists()
    assert env["spy"].messages == []
