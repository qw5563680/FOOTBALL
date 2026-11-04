import csv
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
import responses
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from zucai.cli import main

SAMPLE = Path(__file__).parents[1] / "data" / "fixtures" / "t01" / "sample.json"


def test_cli_initializes_all_tables_without_losing_existing_rows(tmp_path, monkeypatch):
    path = tmp_path / "nested" / "football.db"
    monkeypatch.setenv("ZUCAI_DB", str(path))
    assert main(["db", "init"]) == 0
    with sqlite3.connect(path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == {
            "match_stats",
            "match_events",
            "match_lineups",
            "apif_fixture_index",
            "closing_odds",
            "teams",
            "matches",
            "odds",
            "results",
            "sfc_issues",
            "sfc_issue_matches",
            "predictions",
        }
        conn.execute("INSERT INTO teams (name, aliases) VALUES (?, ?)", ("测试队", "[]"))
    assert main(["db", "init"]) == 0
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT name FROM teams").fetchall() == [("测试队",)]


def test_db_path_priority_rollback_and_foreign_keys(tmp_path, monkeypatch):
    from zucai.db import get_engine, init_db, session_scope
    from zucai.models import Team

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ZUCAI_DB", raising=False)
    default = get_engine()
    init_db(default)
    assert (tmp_path / "data" / "zucai.db").is_file()
    default.dispose()
    monkeypatch.setenv("ZUCAI_DB", str(tmp_path / "env.db"))
    explicit = get_engine(tmp_path / "explicit.db")
    init_db(explicit)
    assert not (tmp_path / "env.db").exists()
    with pytest.raises(RuntimeError), session_scope(explicit) as session:
        session.add(Team(name="rollback"))
        session.flush()
        raise RuntimeError("simulate aborted import")
    with session_scope(explicit) as session:
        assert session.scalar(select(func.count()).select_from(Team)) == 0
        assert session.scalar(text("PRAGMA foreign_keys")) == 1
    with pytest.raises(IntegrityError), session_scope(explicit) as session:
        session.execute(
            text(
                "INSERT INTO results(match_id, home_goals, away_goals) VALUES ('jc:missing', 0, 0)"
            )
        )
    explicit.dispose()


def test_team_identity_and_unknown_name_audit(tmp_path, monkeypatch):
    from zucai.db import get_engine, init_db, session_scope
    from zucai.models import Team
    from zucai.sources.teams import normalize_team, upsert_team

    monkeypatch.chdir(tmp_path)
    engine = get_engine(":memory:")
    init_db(engine)
    with session_scope(engine) as session:
        first = upsert_team(session, "　曼联ＦＣ　", source="jc")
        assert first.name == "曼联"
        assert upsert_team(session, "曼联队").id == first.id
        first.aliases = [*first.aliases, "Manchester United"]
        assert upsert_team(session, "Manchester   United FC").id == first.id
        assert upsert_team(session, "曼联女足").id != first.id
        assert upsert_team(session, "曼联 U21").id != first.id
        assert upsert_team(session, "曼联").id == first.id
        assert upsert_team(session, "曼彻斯特联", source="c500").id != first.id
        assert session.scalar(select(func.count()).select_from(Team)) == 4
    assert normalize_team("ＡＦＣ Bournemouth") == "AFC Bournemouth"
    assert normalize_team("曼联队 FC") == "曼联"
    with session_scope(engine) as session:
        lyon = upsert_team(session, "Olympique Alès")
        assert upsert_team(session, "OLYMPIQUE ALÈS FC").id == lyon.id
    with pytest.raises(ValueError):
        normalize_team("　 ")
    with (tmp_path / "data" / "unmatched_teams.csv").open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert {r["normalized"] for r in rows} == {
        "曼联",
        "曼联女足",
        "曼联 U21",
        "Olympique Alès",
        "曼彻斯特联",
    }
    assert len(rows) == 5
    assert rows[0]["source"] == "jc" and rows[0]["raw_name"] == "　曼联ＦＣ　"
    assert datetime.fromisoformat(rows[0]["created_utc"]).utcoffset() == timedelta(0)
    engine.dispose()


def test_raw_store_replays_two_snapshots_at_a_utc_day_boundary(tmp_path):
    from zucai.sources.base import RawStore

    store = RawStore(tmp_path / "raw")
    ts = datetime(2026, 11, 1, 0, 15, tzinfo=timezone(timedelta(hours=8)))
    body = SAMPLE.read_bytes()
    first = store.save("jc", "fixtures", body, ".json", captured_at=ts)
    second = store.save("jc", "fixtures", b'{"version":2}', "json", captured_at=ts)
    assert first != second
    assert first.parent == tmp_path / "raw" / "jc" / "2026-10-31"
    assert store.load(first) == body
    assert store.load(second) == b'{"version":2}'
    with pytest.raises(ValueError):
        store.save("../escape", "payload", b"x", "bin")
    with pytest.raises(ValueError):
        store.save("jc", "payload", b"x", "bin", captured_at=ts.replace(tzinfo=None))


def test_http_retries_only_transient_get_and_has_default_timeout():
    from zucai.sources.base import make_session

    url = "https://fixtures.invalid/football"
    with responses.RequestsMock() as stub, make_session() as http:
        stub.add(responses.GET, url, status=503)
        stub.add(responses.GET, url, body=SAMPLE.read_bytes(), status=200)
        assert http.get(url).content == SAMPLE.read_bytes()
        assert [c.response.status_code for c in stub.calls] == [503, 200]
        assert stub.calls[-1].request.req_kwargs["timeout"] == 15
        assert http.headers["User-Agent"]
    with responses.RequestsMock() as stub, make_session() as http:
        stub.add(responses.GET, url, status=404)
        assert http.get(url, timeout=2).status_code == 404
        assert len(stub.calls) == 1
        assert stub.calls[-1].request.req_kwargs["timeout"] == 2
    with responses.RequestsMock() as stub, make_session() as http:
        stub.add(responses.GET, url, status=503)
        assert http.get(url).status_code == 503
        assert len(stub.calls) == 4
    with responses.RequestsMock() as stub, make_session() as http:
        stub.add(responses.GET, url, status=429, headers={"Retry-After": "1"})
        assert http.get(url).status_code == 429
        assert len(stub.calls) == 1


def test_archived_response_parses_twice_offline_with_original_metadata(tmp_path):
    from zucai.sources.base import RawStore, Source, make_session

    @dataclass(frozen=True)
    class Fixture:
        home: str
        away: str

    url = "https://fixtures.invalid/schedule"
    fixed = datetime(2026, 11, 1, 0, 15, tzinfo=timezone(timedelta(hours=8)))
    store = RawStore(tmp_path / "raw")

    class SampleSource(Source[Fixture]):
        name = "demo"

        def fetch_raw(self) -> Path:
            with make_session() as http:
                return store.save_response(
                    self.name, "schedule", http.get(url), "json", captured_at=fixed
                )

        def parse(self, raw: bytes) -> list[Fixture]:
            return [Fixture(**item) for item in json.loads(raw)["matches"]]

    source = SampleSource()
    with responses.RequestsMock() as stub:
        stub.add(responses.GET, url, body=SAMPLE.read_bytes(), status=200)
        path = source.fetch_raw()
    metadata = store.load_metadata(path)
    assert metadata == {
        "url": url,
        "fetched_at_utc": "2026-10-31T16:15:00.000000Z",
        "status_code": 200,
        "sha256": hashlib.sha256(SAMPLE.read_bytes()).hexdigest(),
    }
    assert path.with_suffix(path.suffix + ".meta.json").exists()
    assert (
        source.parse(store.load(path))
        == source.parse(store.load(path))
        == [Fixture("示例甲", "示例乙")]
    )


def test_rate_limiter_sleeps_only_for_remaining_interval(monkeypatch):
    from zucai.sources import base

    clock = [100.0]
    delays = []

    def sleep(seconds):
        delays.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(base.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(base.time, "sleep", sleep)
    limiter = base.RateLimiter(5)
    limiter.wait()
    clock[0] += 2
    limiter.wait()
    clock[0] += 10
    limiter.wait()
    assert delays == [3]


def test_odds_replay_key_keeps_separate_capture_times():
    from zucai.db import get_engine, init_db, session_scope, upsert_match
    from zucai.models import Match, Odds, Team

    engine = get_engine(":memory:")
    init_db(engine)
    first = "2026-11-01T16:10:00+08:00"
    second = "2026-11-01T08:15:00Z"
    with session_scope(engine) as session:
        home, away = Team(name="主队"), Team(name="客队")
        session.add_all([home, away])
        session.flush()
        fixture = dict(
            match_id="jc:1",
            source="jc",
            league="示例",
            home_team_id=home.id,
            away_team_id=away.id,
            kickoff_utc="2026-11-01T09:00:00Z",
        )
        upsert_match(session, **fixture)
        upsert_match(session, **fixture, jc_num="周日001")
        assert session.scalar(select(func.count()).select_from(Match)) == 1
        assert session.get(Match, "jc:1").jc_num == "周日001"
        market = dict(match_id="jc:1", pool="had", selection="h")
        session.add_all(
            [
                Odds(**market, snapshot_utc=first, price=2.0),
                Odds(**market, snapshot_utc=second, price=1.9),
            ]
        )
    with pytest.raises(IntegrityError), session_scope(engine) as session:
        session.add(Odds(**market, snapshot_utc="2026-11-01T08:10:00Z", price=2.0))
    with session_scope(engine) as session:
        assert session.scalars(select(Odds.price).order_by(Odds.snapshot_utc)).all() == [2.0, 1.9]
    engine.dispose()


def test_utc_storage_normalizes_offsets(tmp_path):
    from zucai.db import get_engine, init_db, session_scope
    from zucai.models import SfcIssue

    engine = get_engine(tmp_path / "utc.db")
    init_db(engine)
    with session_scope(engine) as s:
        s.add(SfcIssue(issue_no="001", sale_end_utc="2026-11-01T00:15:00+08:00"))
    with session_scope(engine) as s:
        stored = s.get(SfcIssue, "001").sale_end_utc
        assert datetime.fromisoformat(stored).utcoffset() == timedelta(0)
        assert datetime.fromisoformat(stored) == datetime(2026, 10, 31, 16, 15, tzinfo=UTC)
    engine.dispose()
