"""Synthetic T14-derived fixtures prove T65 history routes stay out of daily work."""

import copy
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest

from zucai.cloud_aliases import write_candidates
from zucai.db import get_engine, init_db
from zucai.league_registry import daily_codes, provider_entries
from zucai.sources.apif_command import fetch_apif
from zucai.sources.base import RawStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data/fixtures/t14/apif.json"
DAILY_CODES = "98 292 188 113 103 244 119 253 169 3 848".split()
# The review listed 62 unique IDs despite its original heading saying 60.
NEW_CODES = (
    "79 89 99 307 41 235 80 95 114 104 293 45 81 48 101 137 66 143 102 90 46 "
    "73 294 96 547 556 529 528 105 526 115 17 13 15 18 531 1168 16 32 30 34 "
    "37 10 1 6 4 7 9 22 960 1222 23 480 532 524 490 38 897 803 965 1245 1188"
).split()
PENDING_CODES = {"40", "62", "71", "850", "916"}
CAPTURED = datetime(2026, 11, 3, tzinfo=UTC)


def _document(*, extra=False, rejected=False):
    doc = json.loads(FIXTURE.read_bytes())
    if extra:
        for index, code in enumerate(NEW_CODES):
            row = copy.deepcopy(doc["response"][0])
            row["fixture"]["id"] = 20000 + index
            row["fixture"]["status"]["short"] = "FT"
            row["league"]["id"] = int(code)
            row["teams"]["home"]["id"] = 30000 + index * 2
            row["teams"]["away"]["id"] = 30001 + index * 2
            doc["response"].append(row)
        if rejected:
            row = copy.deepcopy(doc["response"][1])
            row["fixture"]["id"] = 29999
            row["fixture"]["status"]["short"] = "AWD"
            doc["response"].append(row)
    doc["results"] = len(doc["response"])
    return doc


def _archive(root, doc):
    scope = doc["parameters"]
    return RawStore(root).save(
        "api-football",
        "synthetic-t65-daily",
        json.dumps(doc).encode(),
        "json",
        captured_at=CAPTURED,
        url="https://v3.football.api-sports.io/fixtures?"
        + "&".join(f"{key}={value}" for key, value in scope.items()),
        status_code=200,
    )


def _snapshot(database):
    with sqlite3.connect(database) as db:
        tables = db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        return {
            name: db.execute(f'SELECT * FROM "{name}" ORDER BY 1').fetchall() for (name,) in tables
        }


def _official(database):
    engine = get_engine(database)
    init_db(engine)
    engine.dispose()
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO teams VALUES (1,'Home','[]'),(2,'Away','[]')")
        for index, league in enumerate(("日职", "德乙"), 1):
            db.execute(
                "INSERT INTO matches (match_id,source,league,home_team_id,away_team_id,"
                "kickoff_utc,status) VALUES (?,'jc',?,1,2,?,'scheduled')",
                (f"jc:{index}", league, "2026-11-01T11:00:00+00:00"),
            )


def test_reviewed_history_routes_do_not_enter_the_daily_request_plan():
    assert daily_codes("apif") == DAILY_CODES
    assert set(provider_entries("apif", purpose="backfill")) == set(DAILY_CODES + NEW_CODES)
    for purpose in ("daily", "backfill"):
        assert not PENDING_CODES & provider_entries("apif", purpose=purpose).keys()
        assert "5" not in provider_entries("apif", purpose=purpose)


def test_new_routes_do_not_change_daily_database_or_audits(tmp_path, monkeypatch):
    # Compare the same actual-import clock in both runs; T63 never backdates it.
    from zucai.sources import apif_command

    original = apif_command.import_apif_games

    def at_same_clock(*args, **kwargs):
        return original(*args, **kwargs, imported_utc=CAPTURED + timedelta(hours=1))

    monkeypatch.setattr(apif_command, "import_apif_games", at_same_clock)
    observed = []
    for name, extra in (("clean", False), ("mixed", True)):
        root = tmp_path / name
        path = _archive(root / "raw", _document(extra=extra, rejected=extra))
        database = root / "data/zucai.db"
        result = fetch_apif(
            date="2026-11-01",
            raw_file=path,
            db=database,
            audit_dir=root / "audit",
            quota_path=root / "quota.json",
        )
        assert result["inserted"] == 1
        assert result["rejected_rows"] == result["suspected_duplicates"] == 0
        assert result["requests"] == 0
        observed.append(
            (
                _snapshot(database),
                {p.name: p.read_bytes() for p in (root / "audit").glob("*.csv")},
                {key: value for key, value in result.items() if key != "files"},
            )
        )
    assert observed[0] == observed[1]


@pytest.mark.parametrize("rejected", [False, True], ids=["valid-new-rows", "new-awd-row"])
def test_new_routes_cannot_add_candidates_or_hide_old_daily_candidates(tmp_path, rejected):
    observed = []
    for name, extra in (("clean", False), ("mixed", True)):
        root = tmp_path / name
        database, raw, out = root / "data/zucai.db", root / "raw", root / "candidates"
        _official(database)
        path = _archive(raw, _document(extra=extra, rejected=rejected))
        digest = sha256(path.read_bytes()).hexdigest()
        captures = [{"scope": {"date": "2026-11-01"}, "raw_sha256": digest}]
        assert write_candidates("apif", database, raw, captures, out) == 2
        candidates = json.loads((out / "apif.json").read_bytes())
        assert set(candidates) == {"日职"}
        assert set(candidates["日职"]) == {"551", "552"}
        assert [candidates["日职"][code]["canonical"] for code in ("551", "552")] == [
            "Home",
            "Away",
        ]
        for entry in candidates["日职"].values():
            evidence = entry["evidence"][0]
            assert evidence["raw_sha256"] == digest
            assert raw / evidence["sidecar"] == path.with_suffix(".json.meta.json")
            # Different raw bytes have different truthful provenance. Compare the
            # business decision only after checking each witness's actual archive.
            evidence.pop("raw_sha256")
            evidence.pop("sidecar")
        observed.append(candidates)
    assert observed[0] == observed[1]


def test_new_approved_route_still_imports_an_explicit_historical_season(tmp_path):
    doc = _document(extra=True)
    doc["response"] = [doc["response"][1]]
    doc.update(parameters={"league": "79", "season": "2026"}, results=1)
    path = _archive(tmp_path / "raw", doc)
    database = tmp_path / "data/zucai.db"
    result = fetch_apif(
        league_id=79,
        seasons=[2026],
        assume=timedelta(hours=3),
        raw_file=path,
        db=database,
        audit_dir=tmp_path / "audit",
        quota_path=tmp_path / "quota.json",
    )
    assert result["inserted"] == result["assumed"] == 1
    assert result["rejected_rows"] == result["requests"] == 0
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT match_id,league FROM matches").fetchall() == [
            ("apif:20000", "德乙")
        ]


def test_special_nations_date_route_remains_available(tmp_path):
    doc = _document()
    doc["response"][0]["league"]["id"] = 5
    path = _archive(tmp_path / "raw", doc)
    database = tmp_path / "data/zucai.db"
    result = fetch_apif(
        date="2026-11-01",
        raw_file=path,
        db=database,
        audit_dir=tmp_path / "audit",
        quota_path=tmp_path / "quota.json",
    )
    assert result["inserted"] == 1
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT league FROM matches").fetchall() == [("欧国联",)]
