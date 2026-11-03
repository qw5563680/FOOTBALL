"""T24 uses only the synthetic T09 fixture; no collection requests."""

import csv
import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from zucai.db import get_engine, init_db, session_scope, upsert_match
from zucai.models import Match, Result
from zucai.sources.api_common import ApiGame
from zucai.sources.fd_ingest import _append_audit, fd_match_id, import_fd_games
from zucai.sources.fdorg_command import _audit
from zucai.sources.fdorg_ingest import import_fdorg_games
from zucai.sources.football_data import FdGame

ROOT = Path(__file__).parents[1]
SAMPLE = json.loads((ROOT / "data/fixtures/t09/synthetic_ingest.json").read_bytes())
VALUES = SAMPLE["fixture"]
WHEN = datetime.fromisoformat(VALUES["kickoff_utc"])
SEEN = datetime.fromisoformat(SAMPLE["delayed_finish"]["finish_seen"])
KEY = f"fdorg:{VALUES['provider_match_id']}"
FD = FdGame(
    "E0",
    "2627",
    WHEN,
    VALUES["home_raw"],
    VALUES["away_raw"],
    tuple(VALUES["full"]),
    tuple(VALUES["half"]),
    {},
    2,
    "a" * 64,
)
FD_KEY = fd_match_id(FD)
GAME = ApiGame(
    **(VALUES | {"kickoff_utc": WHEN, "full": tuple(VALUES["full"]), "half": tuple(VALUES["half"])})
)
FD_ALIASES = {"英超": {entry["name"]: entry for entry in SAMPLE["aliases"]["英超"].values()}}


def fd(session):
    return import_fd_games(
        session,
        [FD],
        seen=SEEN,
        assume=timedelta(hours=3),
        aliases=FD_ALIASES,
        leagues={"E0": {"league": "英超"}},
    )


def fdorg(session):
    return import_fdorg_games(session, [GAME], seen=SEEN, assume=None, aliases=SAMPLE["aliases"])


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ZUCAI_DB", str(tmp_path / "data/zucai.db"))
    engine = get_engine(tmp_path / "data/zucai.db")
    init_db(engine)
    with session_scope(engine) as session:
        result = import_fdorg_games(
            session,
            [replace(GAME, status="SCHEDULED", full=None, half=None)],
            seen=WHEN - timedelta(days=1),
            assume=None,
            aliases=SAMPLE["aliases"],
        )
        assert result["inserted"] == 1
    yield engine
    engine.dispose()


def clone(session, match, key, *, reverse=False, hours=0, counterpart=None):
    upsert_match(
        session,
        match_id=key,
        source=key.split(":")[0],
        league=match.league,
        home_team_id=match.away_team_id if reverse else match.home_team_id,
        away_team_id=match.home_team_id if reverse else match.away_team_id,
        kickoff_utc=datetime.fromisoformat(match.kickoff_utc) + timedelta(hours=hours),
        status="scheduled",
        counterpart_id=counterpart,
    )
    session.flush()


def evidence(session):
    return {
        table.name: [tuple(row) for row in session.execute(select(table))]
        for table in Match.metadata.sorted_tables
        if table.name != "matches"
    }


def test_fd_late_links_fdorg_and_recapture_preserves_scores_and_availability(db):
    with session_scope(db) as s:
        assert fd(s)["inserted"] == 1
        assert s.get(Match, KEY).counterpart_id == FD_KEY
        before = evidence(s)
        for _ in range(2):
            result = fdorg(s)
            assert result["linked_fd"] == 1 and result["suspected_duplicates"] == 0
            assert fd(s)["unchanged"] == 1
        assert evidence(s) == before
        assert len(list(s.scalars(select(Match)))) == 2
        assert s.get(Result, FD_KEY).home_goals == FD.full[0]
        assert s.get(Result, KEY) is None


def test_fdorg_repairs_unique_existing_pair(db):
    with session_scope(db) as s:
        fd(s)
        s.get(Match, KEY).counterpart_id = None
        before = evidence(s)
        assert fdorg(s)["linked_fd"] == 1
        assert s.get(Match, KEY).counterpart_id == FD_KEY
        assert evidence(s) == before


@pytest.mark.parametrize(
    "conflict", ["multiple", "reverse", "linked", "fd_claimed", "fd_multiple", "fd_reverse"]
)
def test_conflicts_remain_suspected(db, conflict):
    with session_scope(db) as s:
        match = s.get(Match, KEY)
        if conflict in {"multiple", "reverse"}:
            clone(s, match, "fdorg:91002", reverse=conflict == "reverse")
        elif conflict == "linked":
            match.counterpart_id = "fd:elsewhere"
        else:
            clone(
                s,
                match,
                "fd:existing",
                reverse=conflict == "fd_reverse",
                counterpart="jc:other" if conflict == "fd_claimed" else None,
            )
        if conflict in {"multiple", "reverse", "linked", "fd_claimed", "fd_multiple"}:
            result = fd(s)
            assert result["suspected_duplicates"] == 1
            assert s.get(Match, FD_KEY) is None
        if conflict == "fd_multiple":
            clone(s, match, "fd:second")
        result = fdorg(s)
        if conflict != "linked":
            assert result["suspected_duplicates"] == 1
            assert result["linked_fd"] == 0


@pytest.mark.parametrize("provider", ["fd", "fdorg"])
@pytest.mark.parametrize("kind", ["rejected_rows", "suspected_duplicates"])
def test_audit_append_deduplicates_but_preserves_old_bytes(tmp_path, provider, kind):
    path = tmp_path / f"{provider}_{kind}.csv"
    row = dict(
        div_code=FD.div_code,
        row_number=FD.row_number,
        provider_match_id=GAME.provider_match_id,
        raw_sha256=FD.raw_sha256,
        reason="fixture",
        candidates=[KEY],
    )

    def append(rows, sha=FD.raw_sha256):
        if provider == "fd":
            _append_audit(path, [r | {"raw_sha256": sha} for r in rows])
        else:
            _audit(path, rows, sha)

    append([row])
    # Old duplicate history is deliberately retained, not rewritten.
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(b"".join([*lines, lines[-1]]))
    previous = path.read_bytes()
    append([row, row])
    assert path.read_bytes() == previous
    append([row | {"reason": "new reason"}, row | {"reason": "new reason"}])
    append([row], "b" * 64)
    append([row | {"row_number": 3, "provider_match_id": GAME.provider_match_id + 1}])
    assert path.read_bytes().startswith(previous)
    with path.open(newline="") as stream:
        assert len(list(csv.DictReader(stream))) == 5


def test_relink_is_idempotent_and_changes_only_counterparts(db):
    from zucai.sources.fd_counterpart import relink_fdorg

    with session_scope(db) as s:
        fd(s)
        s.get(Match, KEY).counterpart_id = None
        before = evidence(s)
        matches = [
            tuple(row)
            for row in s.execute(
                select(*[c for c in Match.__table__.columns if c.name != "counterpart_id"])
            )
        ]
    report = relink_fdorg(Path(db.url.database))
    assert report["linked"] == 1 and report["skipped"] == 0
    assert report["pairs"][0]["fd_id"] == FD_KEY
    again = relink_fdorg(Path(db.url.database))
    assert again["linked"] == 0 and again["reasons"] == {"already_linked": 1}
    with session_scope(db) as s:
        assert evidence(s) == before
        assert [
            tuple(row)
            for row in s.execute(
                select(*[c for c in Match.__table__.columns if c.name != "counterpart_id"])
            )
        ] == matches


@pytest.mark.parametrize(
    "actions,event", [("", ""), ("true", "schedule"), ("false", "workflow_dispatch")]
)
def test_cleanup_cli_rejects_non_manual_actions_before_restore(
    monkeypatch, tmp_path, actions, event
):
    from zucai.cloud_feeds import main

    monkeypatch.setenv("GITHUB_ACTIONS", actions)
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setattr(
        "sys.argv",
        [
            "cloud_feeds",
            "backfill",
            "--relink-fdorg",
            "--archive",
            str(tmp_path / "absent"),
            "--run-id",
            "offline",
        ],
    )
    with pytest.raises(ValueError, match="workflow_dispatch"):
        main()


def test_cleanup_cli_roundtrip_and_summary(db, tmp_path, monkeypatch):
    from zucai.cloud_feeds import export_feeds, main, restore_feeds

    with session_scope(db) as s:
        fd(s)
        s.get(Match, KEY).counterpart_id = None
    archive = tmp_path / "archive"
    prior = export_feeds(Path(db.url.database), archive, WHEN.date(), "before")
    work = tmp_path / "isolated"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(
        "sys.argv",
        [
            "cloud_feeds",
            "backfill",
            "--relink-fdorg",
            "--archive",
            str(archive),
            "--run-id",
            "offline",
        ],
    )
    main()
    after = restore_feeds(archive, tmp_path / "restored/data/zucai.db")
    assert after["counts"] == prior["counts"]
    assert after["through_beijing"] == prior["through_beijing"]
    assert after["collection"]["fd"]["linked"] == 1
    assert "补链 1；跳过 0" in summary.read_text()
    assert KEY in summary.read_text() and FD_KEY in summary.read_text()


def test_cleanup_workflow_is_manual_without_secrets():
    text = (ROOT / ".github/workflows/football-feeds.yml").read_text()
    steps = text.split("      - name:")
    step = next(s for s in steps if "Relink fdorg" in s)
    assert "env.FEED_MODE == 'relink-fdorg'" in step
    assert "backfill --relink-fdorg" in step
    assert "secrets." not in step
    assert "env.FEED_MODE != 'relink-fdorg'" in next(
        s for s in steps if "Restore and collect APIs" in s
    )
    for title in ("Publish verified data", "Keep collection originals"):
        assert "env.FEED_MODE == 'relink-fdorg'" in next(s for s in steps if title in s)


def test_fdorg_audit_uses_provider_id_with_row_number_only_as_fallback(tmp_path):
    path = tmp_path / "fdorg_rejected_rows.csv"
    _audit(
        path,
        [{"provider_match_id": GAME.provider_match_id, "row_number": 2, "reason": "fixture"}],
        FD.raw_sha256,
    )
    old = path.read_bytes()
    _audit(
        path,
        [{"provider_match_id": GAME.provider_match_id, "row_number": 3, "reason": "fixture"}],
        FD.raw_sha256,
    )
    assert path.read_bytes() == old
    _audit(
        path,
        [{"row_number": 2, "reason": "fixture"}, {"row_number": 3, "reason": "fixture"}],
        FD.raw_sha256,
    )
    with path.open(newline="") as stream:
        assert len(list(csv.DictReader(stream))) == 3


@pytest.mark.parametrize(
    "conflict", ["multiple_fd", "multiple_fdorg", "reverse", "existing_link", "incoming_link", "jc"]
)
def test_cleanup_skips_conflicts_without_mutating_rows(db, conflict):
    from zucai.sources.fd_counterpart import relink_fdorg

    with session_scope(db) as s:
        match = s.get(Match, KEY)
        clone(s, match, FD_KEY, reverse=conflict == "reverse")
        if conflict == "multiple_fd":
            clone(s, match, "fd:other")
        elif conflict == "multiple_fdorg":
            clone(s, match, "fdorg:other")
        elif conflict == "existing_link":
            match.counterpart_id = "fd:other"
        elif conflict == "incoming_link":
            clone(s, match, "apif:other", counterpart=FD_KEY)
        elif conflict == "jc":
            clone(s, match, "jc:other")
        before = [tuple(row) for row in s.execute(select(Match.__table__))]
    result = relink_fdorg(Path(db.url.database))
    assert result["linked"] == 0 and result["skipped"] > 0 and result["reasons"]
    with session_scope(db) as s:
        assert [tuple(row) for row in s.execute(select(Match.__table__))] == before


def test_fdorg_recapture_cannot_move_link_outside_time_window(db):
    with session_scope(db) as s:
        fd(s)
        before = [tuple(row) for row in s.execute(select(Match.__table__))]
        scores = evidence(s)
        result = import_fdorg_games(
            s,
            [
                replace(
                    GAME,
                    kickoff_utc=WHEN + timedelta(days=7),
                    status="SCHEDULED",
                    full=None,
                    half=None,
                )
            ],
            seen=SEEN,
            assume=None,
            aliases=SAMPLE["aliases"],
        )
        assert result["suspected_duplicates"] == 1
        assert [tuple(row) for row in s.execute(select(Match.__table__))] == before
        assert evidence(s) == scores


@pytest.mark.parametrize("days,expected", [(1, 1), (-1, 0)])
def test_fd_reimport_checks_effective_time_without_changing_stale_observation_rules(
    db, days, expected
):
    with session_scope(db) as s:
        fd(s)
        before = [tuple(row) for row in s.execute(select(Match.__table__))]
        scores = evidence(s)
        result = import_fd_games(
            s,
            [replace(FD, kickoff_utc=WHEN - timedelta(hours=4))],
            seen=SEEN + timedelta(days=days),
            assume=timedelta(hours=3),
            aliases=FD_ALIASES,
            leagues={"E0": {"league": "英超"}},
        )
        assert result["suspected_duplicates"] == expected
        assert result["unchanged"] == 1 - expected
        assert [tuple(row) for row in s.execute(select(Match.__table__))] == before
        assert evidence(s) == scores
