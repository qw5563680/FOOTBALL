import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from zucai.db import get_engine, init_db, session_scope, upsert_match
from zucai.models import Match, Odds, Result, Team

WHEN = datetime(2026, 9, 1, 19, tzinfo=UTC)
SEEN = datetime(2026, 11, 2, tzinfo=UTC)
DELAY = timedelta(hours=3)


@pytest.fixture
def fd_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    engine = get_engine(tmp_path / "input.db")
    init_db(engine)
    with session_scope(engine) as s:
        s.add_all([Team(name=n) for n in ("主方", "客方", "另一方")])
    yield engine
    engine.dispose()


def sample(**kwargs):
    from zucai.sources.football_data import FdGame

    values = dict(
        div_code="E0",
        season="2627",
        kickoff_utc=WHEN,
        home_raw="Home FC",
        away_raw="Away",
        full=(2, 1),
        half=(1, 0),
        closing={"B365": (2.1, 3.2, 4.0)},
        row_number=2,
        raw_sha256="a" * 64,
    )
    return FdGame(**(values | kwargs))


def aliases(home="approved", away="approved"):
    return {
        "英超": {
            "Home FC": {"canonical": "主方", "status": home},
            "Away": {"canonical": "客方", "status": away},
        }
    }


def add_official(s, *, counterpart=None):
    teams = {t.name: t.id for t in s.scalars(select(Team))}
    upsert_match(
        s,
        match_id="jc:1",
        source="jc",
        league="英超",
        kickoff_utc=WHEN,
        home_team_id=teams["主方"],
        away_team_id=teams["客方"],
        jc_num="周二001",
        status="finished",
        counterpart_id=counterpart,
    )
    s.add(
        Result(
            match_id="jc:1",
            home_goals=9,
            away_goals=0,
            available_utc=WHEN + timedelta(hours=4),
            available_source="captured",
        )
    )
    s.flush()


def ingest(s, games=None, mapping=None, **kwargs):
    from zucai.sources.fd_ingest import import_fd_games

    return import_fd_games(
        s,
        [sample()] if games is None else games,
        seen=kwargs.pop("seen", SEEN),
        assume=kwargs.pop("assume", DELAY),
        aliases=aliases() if mapping is None else mapping,
        leagues={"E0": {"league": "英超"}},
        **kwargs,
    )


def test_import_requires_explicit_assumption_before_writes(fd_db):
    with session_scope(fd_db) as s:
        with pytest.raises(ValueError, match="assume-known"):
            ingest(s, assume=None)
        assert s.scalar(select(func.count()).select_from(Match)) == 0


@pytest.mark.parametrize("status", ["approved", "auto_verified", "proposed"])
def test_alias_states_and_untimed_closing_archive(fd_db, status):
    from zucai.models import ClosingOdds

    with session_scope(fd_db) as s:
        result = ingest(s, mapping=aliases(home=status, away=status))
        assert result["inserted"] == 1 and result["assumed"] == 1
        match = s.scalar(select(Match))
        home = s.get(Team, match.home_team_id)
        assert home.name == ("Home" if status == "proposed" else "主方")
        if status != "proposed":
            assert "Home FC" in home.aliases
        score = s.get(Result, match.match_id)
        assert score.available_source == "assumed"
        assert datetime.fromisoformat(score.available_utc) == WHEN + DELAY
        price = s.scalar(select(ClosingOdds))
        assert (price.bookmaker, price.h, price.d, price.a) == ("B365", 2.1, 3.2, 4)
        assert price.basis == "publisher_closing"
        assert "snapshot_utc" not in ClosingOdds.__table__.columns
        assert s.scalar(select(func.count()).select_from(Odds)) == 0


def test_proposed_does_not_activate_an_existing_global_alias(fd_db):
    with session_scope(fd_db) as s:
        canonical = s.scalar(select(Team).where(Team.name == "主方"))
        canonical.aliases.append("Home FC")
        result = ingest(s, mapping=aliases(home="proposed"))
        assert result["inserted"] == 1
        assert s.scalar(select(Match)).home_team_id != canonical.id


def test_active_alias_in_another_league_does_not_merge_unknown_history(fd_db):
    from dataclasses import replace

    from zucai.sources.fd_ingest import import_fd_games

    with session_scope(fd_db) as s:
        first = ingest(s, mapping={})
        assert first["inserted"] == 1
        unknown_home = s.scalar(select(Team).where(Team.name == "Home"))
        mapped = import_fd_games(
            s,
            [replace(sample(), div_code="E1")],
            seen=SEEN,
            assume=DELAY,
            aliases={"英冠": aliases()["英超"]},
            leagues={"E1": {"league": "英冠"}},
        )
        assert mapped["inserted"] == 1 and mapped["rejected_rows"] == 0
        assert s.get(Match, "fd:E0:20260901:home-fc:away").home_team_id == unknown_home.id
        assert s.get(Match, "fd:E1:20260901:home-fc:away").home_team_id != unknown_home.id
        assert ingest(s, mapping={})["unchanged"] == 1
        assert s.get(Match, "fd:E0:20260901:home-fc:away").home_team_id == unknown_home.id


def test_new_alias_cannot_silently_reassign_existing_same_league_identity(fd_db):
    with session_scope(fd_db) as s:
        assert ingest(s, mapping={})["inserted"] == 1
        result = ingest(s, games=[sample(kickoff_utc=WHEN + timedelta(days=7))])
        assert result["rejected_rows"] == 1
        assert "独立球队身份" in result["rejected"][0]["reason"]


@pytest.mark.parametrize("counterpart", [None, "c500:old"])
def test_official_overlap_keeps_score_and_existing_counterpart(fd_db, counterpart):
    from zucai.models import ClosingOdds

    with session_scope(fd_db) as s:
        add_official(s, counterpart=counterpart)
        result = ingest(s)
        assert result["linked_existing"] == 1 and result["inserted"] == 0
        assert s.scalar(select(func.count()).select_from(Match)) == 1
        assert s.get(Result, "jc:1").home_goals == 9
        assert s.get(Match, "jc:1").counterpart_id == (counterpart or "fd:E0:20260901:home-fc:away")
        assert s.scalar(select(ClosingOdds)).match_id == "jc:1"


@pytest.mark.parametrize("mapping", [aliases(away="proposed"), {}])
def test_partial_or_unknown_identity_overlap_is_quarantined(fd_db, mapping):
    with session_scope(fd_db) as s:
        add_official(s)
        result = ingest(s, mapping=mapping)
        assert result["suspected_duplicates"] == 1
        assert s.scalar(select(func.count()).select_from(Match)) == 1


def test_rerun_preserves_assumption_correction_uses_capture_and_old_snapshot_cannot_revert(fd_db):
    from zucai.sources.fd_ingest import fd_match_id

    key = fd_match_id(sample())
    with session_scope(fd_db) as s:
        ingest(s)
    with session_scope(fd_db) as s:
        result = ingest(s, seen=SEEN + timedelta(days=1), assume=timedelta(hours=6))
        assert result["unchanged"] == 1
        old = s.get(Result, key)
        assert old.available_source == "assumed"
        assert datetime.fromisoformat(old.available_utc) == WHEN + DELAY
    later = SEEN + timedelta(days=2)
    with session_scope(fd_db) as s:
        result = ingest(s, games=[sample(full=(3, 1))], seen=later)
        assert result["corrected"] == 1
        current = s.get(Result, key)
        assert current.available_source == "captured"
        assert datetime.fromisoformat(current.available_utc) == later
    with session_scope(fd_db) as s:
        result = ingest(s)
        assert result["unchanged"] == 1
        assert s.get(Result, key).home_goals == 3


def test_one_rejected_match_does_not_rollback_valid_matches(fd_db):
    with session_scope(fd_db) as s:
        result = ingest(s, games=[sample(), sample(home_raw="Bad", full=(-1, 0))])
        assert result["inserted"] == 1 and result["rejected_rows"] == 1
        assert s.scalar(select(func.count()).select_from(Match)) == 1


def test_cli_requires_assumption_without_creating_database(tmp_path, capsys):
    from zucai.cli import main

    db = tmp_path / "missing.db"
    with pytest.raises(SystemExit) as error:
        main(["fetch", "fd", "--div", "E0", "--season", "2627", "--db", str(db)])
    assert error.value.code == 2 and not db.exists()
    assert "required: --assume-known-after" in capsys.readouterr().err


def test_savepoint_flush_failure_does_not_count_rolled_back_game(fd_db):
    with session_scope(fd_db) as s:
        s.connection().exec_driver_sql(
            "CREATE TRIGGER reject_one BEFORE INSERT ON results "
            "WHEN NEW.match_id LIKE '%:rejected:%' "
            "BEGIN SELECT RAISE(ABORT, 'fixture rejection'); END"
        )
        result = ingest(s, games=[sample(home_raw="Rejected", closing={}), sample()], mapping={})
        assert result["inserted"] == 1 and result["rejected_rows"] == 1
        assert len(result["outcomes"]) == 1
        assert s.scalar(select(func.count()).select_from(Match)) == 1
        assert "Rejected" not in {n["raw"] for n in result["unmatched_names"]}


@pytest.fixture
def replay(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fixture = Path(__file__).resolve().parents[1] / "data/fixtures/t08/main.csv"
    raw = tmp_path / "sample.csv"
    raw.write_bytes(fixture.read_bytes())
    metadata = dict(
        url="https://www.football-data.co.uk/mmz4281/2627/E0.csv",
        status_code=200,
        sha256=hashlib.sha256(raw.read_bytes()).hexdigest(),
        fetched_at_utc="2026-11-01T12:00:00Z",
    )
    leagues = tmp_path / "leagues.json"
    leagues.write_text(
        json.dumps(
            {
                "E0": dict(
                    div_code="E0",
                    league="英超",
                    status="verified",
                    time_basis="Europe/London",
                    verified_by=2,
                    verified_utc="2026-11-01T00:00:00Z",
                )
            }
        ),
        encoding="utf-8",
    )
    mapping = tmp_path / "aliases.json"
    mapping.write_text("{}", encoding="utf-8")
    return (
        raw,
        metadata,
        dict(
            div="E0",
            seasons=["2627"],
            assume=DELAY,
            db=tmp_path / "replay.db",
            raw_file=raw,
            leagues_path=leagues,
            aliases_path=mapping,
            audit_dir=tmp_path,
        ),
    )


@pytest.mark.parametrize(
    "change",
    [
        {"url": "https://www.football-data.co.uk/mmz4281/2526/E0.csv"},
        {"fetched_at_utc": "2026-11-01T12:00:00"},
        {"sha256": "0" * 64},
    ],
)
def test_replay_rejects_wrong_archive_provenance(replay, change):
    from zucai.sources.fd_ingest import fetch_fd

    raw, metadata, kwargs = replay
    raw.with_suffix(".csv.meta.json").write_text(json.dumps(metadata | change), encoding="utf-8")
    with pytest.raises(ValueError):
        fetch_fd(**kwargs)


def test_cli_offline_replay_is_idempotent_and_audits_bad_row(replay):
    from zucai.cli import main

    raw, metadata, kwargs = replay
    raw.with_suffix(".csv.meta.json").write_text(json.dumps(metadata), encoding="utf-8")
    command = ["fetch", "fd", "--div", "E0", "--season", "2627", "--assume-known-after", "3h"]
    for option, key in [
        ("--db", "db"),
        ("--raw-file", "raw_file"),
        ("--leagues-file", "leagues_path"),
        ("--aliases-file", "aliases_path"),
        ("--audit-dir", "audit_dir"),
    ]:
        command += [option, str(kwargs[key])]
    assert main(command) == 0
    assert main(command) == 0
    engine = get_engine(kwargs["db"])
    with session_scope(engine) as s:
        assert s.scalar(select(func.count()).select_from(Match)) == 29
        assert s.scalar(select(func.count()).select_from(Result)) == 29
        assert s.scalar(select(func.count()).select_from(Odds)) == 0
    engine.dispose()
    assert "Time" in (kwargs["audit_dir"] / "fd_rejected_rows.csv").read_text(encoding="utf-8")


def test_combined_rejection_counts_follow_requested_seasons(replay):
    import csv
    import io

    from zucai.sources.fd_ingest import fetch_fd

    raw, metadata, kwargs = replay
    row = dict(
        Country="Japan",
        League="J1 League",
        Season="2025",
        Date="01/09/2025",
        Time="",
        Home="Home",
        Away="Away",
        HG="1",
        AG="0",
        Res="H",
    )
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=row)
    writer.writeheader()
    writer.writerow(row)
    writer.writerow(row | {"Season": "2026", "Date": "01/09/2026"})
    raw.write_bytes(stream.getvalue().encode())
    metadata.update(
        url="https://football-data.co.uk/new/JPN.csv",
        sha256=hashlib.sha256(raw.read_bytes()).hexdigest(),
    )
    raw.with_suffix(".csv.meta.json").write_text(json.dumps(metadata), encoding="utf-8")
    config = json.loads(kwargs["leagues_path"].read_text(encoding="utf-8"))["E0"]
    config.update(
        div_code="JPN", league="日职", publisher_country="Japan", publisher_league="J1 League"
    )
    kwargs["leagues_path"].write_text(json.dumps({"JPN": config}), encoding="utf-8")
    kwargs.pop("div")
    kwargs.pop("seasons")
    result = fetch_fd(**kwargs, country="JPN", from_season=2026, to_season=2026)
    assert result["rejected_rows"] == 1
    assert result["files"][0]["rejected"][0]["season"] == "2026"
