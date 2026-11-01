"""Review regressions: bounded windows, individual replay, defaults, and first-seen audit."""

import csv
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import responses

from zucai.sources.base import RateLimiter, RawStore
from zucai.sources.fdorg_command import _requests, fetch_fdorg
from zucai.sources.football_data import read_json
from zucai.sources.football_data_org import COMPETITIONS_PATH, FdorgHttpError, FootballDataOrg

FIXTURES = Path(__file__).resolve().parents[1] / "data/fixtures/t09"
API = "https://api.football-data.org/v4"
FIRST_URL = API + "/matches?dateFrom=2026-10-26&dateTo=2026-11-05&competitions=PL"
SECOND_URL = API + "/matches?dateFrom=2026-11-05&dateTo=2026-11-10&competitions=PL"
SEEN = datetime(2026, 11, 3, 0, 0, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "days,expected",
    [
        (0, [("2026-11-02", "2026-11-03")]),
        (4, [("2026-10-29", "2026-11-07")]),
        (5, [("2026-10-28", "2026-11-07"), ("2026-11-07", "2026-11-08")]),
        (7, [("2026-10-26", "2026-11-05"), ("2026-11-05", "2026-11-10")]),
        (
            10,
            [
                ("2026-10-23", "2026-11-02"),
                ("2026-11-02", "2026-11-12"),
                ("2026-11-12", "2026-11-13"),
            ],
        ),
    ],
)
def test_days_windows_cover_each_requested_utc_day_exactly_once(days, expected):
    scopes = _requests("PL", None, days, None, False, date(2026, 11, 2), {"PL": {}})
    assert [(s["date_from"], s["date_to"]) for s in scopes] == expected


def window_payload(index):
    doc = json.loads((FIXTURES / "synthetic_command.json").read_bytes())
    doc["matches"] = [doc["matches"][index]]
    doc["resultSet"]["count"] = 1
    return json.dumps(doc).encode()


def freeze_window_job(monkeypatch):
    import zucai.sources.fdorg_command as command
    import zucai.sources.football_data_org as source

    class CommandClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 2, 23, 59, 59, tzinfo=UTC)

    class SourceClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return SEEN

    monkeypatch.setattr(command, "datetime", CommandClock)
    monkeypatch.setattr(source, "datetime", SourceClock)
    monkeypatch.setattr(FootballDataOrg, "limiter", RateLimiter(0))
    monkeypatch.setenv("FOOTBALL_DATA_ORG_TOKEN", "offline-" + uuid4().hex)


def test_split_windows_archive_and_replay_individually_across_utc_midnight(tmp_path, monkeypatch):
    freeze_window_job(monkeypatch)
    raw_root = tmp_path / "raw"
    with responses.RequestsMock() as http:
        http.get(FIRST_URL, body=window_payload(0))
        http.get(SECOND_URL, body=window_payload(1))
        result = fetch_fdorg(
            competition="PL",
            days=7,
            db=tmp_path / "live.db",
            raw_root=raw_root,
            audit_dir=tmp_path / "audit",
        )
    assert result["requests"] == 2 and result["inserted"] == 2
    sidecars = sorted(raw_root.rglob("*.meta.json"))
    assert len(sidecars) == 2
    assert {read_json(p)["url"] for p in sidecars} == {FIRST_URL, SECOND_URL}
    assert {read_json(p)["window_anchor_date"] for p in sidecars} == {"2026-11-02"}
    assert all(read_json(p)["fetched_at_utc"].startswith("2026-11-03") for p in sidecars)
    monkeypatch.delenv("FOOTBALL_DATA_ORG_TOKEN")
    for sidecar in sidecars:
        replay = fetch_fdorg(
            competition="PL",
            days=7,
            db=tmp_path / "replay.db",
            raw_file=Path(str(sidecar).removesuffix(".meta.json")),
            audit_dir=tmp_path / "replay-audit",
        )
        assert replay["requests"] == 0 and len(replay["files"]) == 1
    with sqlite3.connect(tmp_path / "replay.db") as db:
        assert db.execute("select match_id,status from matches order by match_id").fetchall() == [
            ("fdorg:990001", "finished"),
            ("fdorg:990002", "scheduled"),
        ]
        assert db.execute("select available_source from results").fetchall() == [("captured",)]


def test_later_window_http_failure_keeps_prior_committed_rows_and_both_archives(
    tmp_path, monkeypatch
):
    freeze_window_job(monkeypatch)
    with responses.RequestsMock() as http:
        http.get(FIRST_URL, body=window_payload(0))
        http.get(SECOND_URL, body=(FIXTURES / "synthetic_http_error.json").read_bytes(), status=400)
        with pytest.raises(FdorgHttpError):
            fetch_fdorg(
                competition="PL",
                days=7,
                db=tmp_path / "live.db",
                raw_root=tmp_path / "raw",
                audit_dir=tmp_path / "audit",
            )
    with sqlite3.connect(tmp_path / "live.db") as db:
        assert db.execute("select match_id from matches").fetchall() == [("fdorg:990001",)]
    assert sorted(read_json(p)["status_code"] for p in (tmp_path / "raw").rglob("*.meta.json")) == [
        200,
        400,
    ]


def archived(tmp_path, raw, seen):
    return RawStore(tmp_path / "raw").save(
        "football-data-org",
        "matches",
        raw,
        "json",
        captured_at=seen,
        url=API + "/competitions/PL/matches?season=2026",
        status_code=200,
    )


def test_unknown_team_audit_keeps_earliest_observation_without_duplicate_ids(tmp_path):
    raw = (FIXTURES / "synthetic_command.json").read_bytes()
    first = archived(tmp_path, raw, SEEN)
    kwargs = dict(
        competition="PL",
        seasons=[2026],
        assume=timedelta(hours=3),
        db=tmp_path / "db.sqlite",
        audit_dir=tmp_path / "audit",
    )
    fetch_fdorg(**kwargs, raw_file=first)
    fetch_fdorg(**kwargs, raw_file=first)
    earlier = archived(tmp_path, raw, datetime(2026, 11, 2, 1, tzinfo=UTC))
    fetch_fdorg(**kwargs, raw_file=earlier)
    with (tmp_path / "audit/fdorg_unmatched_teams.csv").open(newline="", encoding="utf8") as stream:
        rows = list(csv.DictReader(stream))
    assert rows == [
        {
            "league": "英超",
            "team_id": "90001",
            "english_name": "Test North FC",
            "first_seen_utc": "2026-11-02T01:00:00.000000Z",
        },
        {
            "league": "英超",
            "team_id": "90002",
            "english_name": "Test South FC",
            "first_seen_utc": "2026-11-02T01:00:00.000000Z",
        },
    ]


def test_live_crop_import_uses_committed_id_aliases_by_default(tmp_path):
    path = archived(tmp_path, (FIXTURES / "live_pl_season.json").read_bytes(), SEEN)
    fetch_fdorg(
        competition="PL",
        seasons=[2026],
        assume=timedelta(hours=3),
        db=tmp_path / "db.sqlite",
        raw_file=path,
        audit_dir=tmp_path / "audit",
    )
    with sqlite3.connect(tmp_path / "db.sqlite") as db:
        assert db.execute(
            "select h.name,a.name from matches m join teams h on h.id=m.home_team_id "
            "join teams a on a.id=m.away_team_id where m.match_id='fdorg:560542'"
        ).fetchone() == ("阿森纳", "考文垂")
    assert {entry["earliest_season"] for entry in read_json(COMPETITIONS_PATH).values()} == {2023}


@pytest.mark.parametrize("anchor", [None, 123, "not-a-date", "2026-11-04"])
def test_replay_rejects_invalid_or_future_window_anchor_before_import(tmp_path, anchor):
    raw = window_payload(0)
    path = RawStore(tmp_path / "raw").save(
        "football-data-org",
        "matches",
        raw,
        "json",
        captured_at=SEEN,
        url=FIRST_URL,
        status_code=200,
    )
    sidecar = path.with_suffix(".json.meta.json")
    meta = read_json(sidecar)
    meta["window_anchor_date"] = anchor
    sidecar.write_text(json.dumps(meta), encoding="utf8")
    with pytest.raises(ValueError):
        fetch_fdorg(
            competition="PL",
            days=7,
            raw_file=path,
            db=tmp_path / "bad.db",
            audit_dir=tmp_path,
        )
    assert not (tmp_path / "bad.db").exists()


@pytest.mark.parametrize("url", [123, {}, []])
def test_replay_rejects_non_string_url_before_import(tmp_path, url):
    path = archived(tmp_path, (FIXTURES / "synthetic_command.json").read_bytes(), SEEN)
    sidecar = path.with_suffix(".json.meta.json")
    meta = read_json(sidecar)
    meta["url"] = url
    sidecar.write_text(json.dumps(meta), encoding="utf8")
    with pytest.raises(ValueError):
        fetch_fdorg(
            competition="PL",
            seasons=[2026],
            assume=timedelta(hours=3),
            raw_file=path,
            db=tmp_path / "bad.db",
            audit_dir=tmp_path,
        )
    assert not (tmp_path / "bad.db").exists()


def test_multi_season_replay_does_not_silently_import_only_one_requested_season(tmp_path):
    path = archived(tmp_path, (FIXTURES / "synthetic_command.json").read_bytes(), SEEN)
    with pytest.raises(ValueError):
        fetch_fdorg(
            competition="PL",
            seasons=[2025, 2026],
            assume=timedelta(hours=3),
            raw_file=path,
            db=tmp_path / "bad.db",
            audit_dir=tmp_path,
        )
    assert not (tmp_path / "bad.db").exists()


def test_capture_example_windows_keep_replay_anchor_across_midnight(tmp_path, monkeypatch):
    import runpy
    from urllib.parse import parse_qs, urlsplit

    freeze_window_job(monkeypatch)
    helpers = runpy.run_path(str(FIXTURES.parents[2] / "examples/t09_capture.py"))
    capture = helpers["capture"]
    instants = iter(
        [
            datetime(2026, 11, 2, 23, 59, 58, tzinfo=UTC),
            datetime(2026, 11, 2, 23, 59, 59, tzinfo=UTC),
            datetime(2026, 11, 3, 0, 0, 2, tzinfo=UTC),
        ]
    )

    class CaptureClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(instants)

    monkeypatch.setitem(capture.__globals__, "datetime", CaptureClock)

    def reply(request):
        parts = urlsplit(request.url)
        if "/competitions/" in parts.path:
            code = parts.path.split("/")[3]
            doc = json.loads((FIXTURES / "synthetic_command.json").read_bytes())
            for row in doc["matches"]:
                row["competition"]["code"] = code
            body = json.dumps(doc).encode()
        else:
            start = parse_qs(parts.query)["dateFrom"][0]
            body = window_payload(0 if start == "2026-10-26" else 1)
        return 200, {"Content-Type": "application/json"}, body

    with responses.RequestsMock() as http:
        import re

        http.add_callback(responses.GET, re.compile(API + "/.*"), callback=reply)
        capture(tmp_path / "capture", 2026, 2026)
    meta = [read_json(p) for p in (tmp_path / "capture/raw").rglob("*.meta.json")]
    windows = [m for m in meta if "/v4/matches?" in m["url"]]
    assert len(windows) == 2
    assert {m["window_anchor_date"] for m in windows} == {"2026-11-02"}
    assert all(m["fetched_at_utc"].startswith("2026-11-03") for m in windows)
