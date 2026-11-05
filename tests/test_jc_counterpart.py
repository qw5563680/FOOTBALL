"""T40: synthetic stored IDs only, with no sports API calls."""

import csv
import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import select

from zucai.db import get_engine, init_db, session_scope, upsert_match
from zucai.models import Match, Result, Team
from zucai.sources.api_common import ApiGame
from zucai.sources.apif_ingest import import_apif_games
from zucai.sources.fdorg_ingest import import_fdorg_games

ROOT = Path(__file__).parents[1]
SAMPLE = json.loads((ROOT / "data/fixtures/t40/late_jc.json").read_bytes())
WHEN = datetime.fromisoformat(SAMPLE["kickoff_utc"])
SEEN = datetime.fromisoformat(SAMPLE["seen_utc"])
JC = SAMPLE["jc_id"]


@pytest.fixture(params=["apif", "fdorg"])
def provider(request):
    return request.param


@pytest.fixture
def db(tmp_path, provider):
    engine = get_engine(tmp_path / "data/zucai.db")
    init_db(engine)
    with session_scope(engine) as s:
        s.add_all(Team(name=name) for name in SAMPLE["teams"])
        s.flush()
        seed(s, f"{provider}:{SAMPLE['provider_match_id']}")
        seed(s, JC)
    yield engine
    engine.dispose()


def seed(s, key, *, reverse=False, hours=0, counterpart=None, league=None):
    teams = list(s.scalars(select(Team).order_by(Team.id)))
    upsert_match(
        s,
        match_id=key,
        source=key.split(":")[0],
        league=league or SAMPLE["league"],
        home_team_id=teams[int(reverse)].id,
        away_team_id=teams[int(not reverse)].id,
        kickoff_utc=WHEN + timedelta(hours=hours),
        counterpart_id=counterpart,
        status="finished",
        result_checked_utc=SEEN,
        result_provider=key.split(":")[0],
    )
    score = SAMPLE["jc_score" if key.startswith("jc:") else "api_score"]
    s.add(
        Result(
            match_id=key,
            home_goals=score[0],
            away_goals=score[1],
            available_utc=SEEN,
            available_source="captured",
        )
    )
    s.flush()


def unchanged_columns(s):
    return {
        table.name: list(
            s.execute(select(*[c for c in table.columns if c.name != "counterpart_id"]))
        )
        for table in Match.metadata.sorted_tables
    }


def ingest(s, provider, **changes):
    game = ApiGame(
        **{
            "provider": provider,
            "provider_match_id": SAMPLE["provider_match_id"],
            "competition_code": "3" if provider == "apif" else "EL",
            "league": SAMPLE["league"],
            "kickoff_utc": WHEN,
            "status": "FT" if provider == "apif" else "FINISHED",
            "home_raw": SAMPLE["aliases"]["551"]["name"],
            "away_raw": SAMPLE["aliases"]["552"]["name"],
            "home_provider_id": 551,
            "away_provider_id": 552,
            "full": (6, 1),  # A changed API result must never overwrite either stored score.
            "half": None,
            "regular_time_basis": "regular",
            **changes,
        }
    )
    importer = import_apif_games if provider == "apif" else import_fdorg_games
    return importer(
        s, [game], seen=SEEN, assume=None, aliases={SAMPLE["league"]: SAMPLE["aliases"]}
    )


@pytest.mark.parametrize("hours", [-3, 0, 3])
def test_relink_only_counterpart_and_repeat_is_database_byte_identical(db, provider, hours):
    from zucai.sources.jc_counterpart import relink_jc

    path = Path(db.url.database)
    with session_scope(db) as s:
        s.get(Match, JC).kickoff_utc = WHEN + timedelta(hours=hours)
        s.flush()
        before = unchanged_columns(s)
    report = relink_jc(path)
    assert (report["linked"], report["skipped"], report["requests"]) == (1, 0, 0)
    key = f"{provider}:{SAMPLE['provider_match_id']}"
    assert report["pairs"][0]["api_id"] == key
    assert report["pairs"][0]["jc_id"] == JC
    assert report["pairs"][0]["outcome"] == "linked"
    with session_scope(db) as s:
        assert unchanged_columns(s) == before
        assert s.get(Match, JC).counterpart_id == key
        assert s.get(Match, key).counterpart_id is None
    database_bytes = path.read_bytes()
    repeated = relink_jc(path)
    assert repeated["linked"] == repeated["skipped"] == 0
    assert repeated["already_linked"] == 1
    assert repeated["pairs"][0]["outcome"] == "already_linked"
    assert path.read_bytes() == database_bytes
    audit = path.parent / "state/audit/jc_relink.csv"
    rows = list(csv.DictReader(audit.open(encoding="utf-8")))
    assert [row["outcome"] for row in rows] == ["linked", "already_linked"]
    audit_bytes = audit.read_bytes()
    relink_jc(path)
    assert path.read_bytes() == database_bytes
    assert audit.read_bytes() == audit_bytes


CONFLICTS = [
    ("reverse", "主客"),
    ("multiple_jc", "多个 jc"),
    ("jc_linked", "jc 行已有"),
    ("api_linked", "API 行已有"),
    ("api_claimed", "API 行已被"),
    ("jc_claimed", "jc 行已被"),
    ("temporary_apif", "临时身份"),
    ("temporary_fdorg", "临时身份"),
    ("multiple_api", "多个 API"),
    ("overlapping_team", "主客"),
]


def conflict(s, provider, kind):
    key = f"{provider}:{SAMPLE['provider_match_id']}"
    jc = s.get(Match, JC)
    api = s.get(Match, key)
    if kind == "reverse":
        jc.home_team_id, jc.away_team_id = jc.away_team_id, jc.home_team_id
    elif kind == "multiple_jc":
        seed(s, "jc:second")
    elif kind == "jc_linked":
        jc.counterpart_id = f"{provider}:elsewhere"
    elif kind == "api_linked":
        api.counterpart_id = "fd:elsewhere"
    elif kind in {"api_claimed", "jc_claimed"}:
        seed(s, "fd:claim", hours=-24, counterpart=key if kind == "api_claimed" else JC)
    elif kind.startswith("temporary_"):
        s.get(Team, api.home_team_id).name = f"{kind.removeprefix('temporary_')}:team:551"
    elif kind == "multiple_api":
        seed(s, f"{provider}:second")
    elif kind == "overlapping_team":
        seed(s, "jc:shared-team")
        s.get(Match, "jc:shared-team").away_team_id = list(
            s.scalars(select(Team.id).order_by(Team.id))
        )[2]
    s.flush()


@pytest.mark.parametrize("kind,reason", CONFLICTS)
def test_cleanup_rejects_conflict_without_changing_database(db, provider, kind, reason):
    from zucai.sources.jc_counterpart import relink_jc

    with session_scope(db) as s:
        conflict(s, provider, kind)
    path = Path(db.url.database)
    before = path.read_bytes()
    report = relink_jc(path)
    assert report["linked"] == 0
    assert report["skipped"] >= 1
    assert reason in report["pairs"][0]["reason"]
    assert report["pairs"][0]["candidates"]
    assert path.read_bytes() == before


@pytest.mark.parametrize("variation", ["other_league", "earlier", "later", "different_ids"])
def test_no_pair_in_exact_id_window_is_not_linked(db, provider, variation):
    from zucai.sources.jc_counterpart import relink_jc

    with session_scope(db) as s:
        jc = s.get(Match, JC)
        if variation == "other_league":
            jc.league = "欧冠"
        elif variation in {"earlier", "later"}:
            delta = timedelta(hours=3, seconds=1)
            jc.kickoff_utc = WHEN + (delta if variation == "later" else -delta)
        else:
            ids = list(s.scalars(select(Team.id).order_by(Team.id)))
            jc.home_team_id, jc.away_team_id = ids[2:]
    path = Path(db.url.database)
    before = path.read_bytes()
    report = relink_jc(path)
    assert report["linked"] == report["already_linked"] == 0
    assert report["pairs"] == []
    assert path.read_bytes() == before


@pytest.mark.parametrize("existing_audit", [False, True])
def test_audit_failure_rolls_back_counterpart_and_receipt(db, monkeypatch, existing_audit):
    from zucai.sources import jc_counterpart

    path = Path(db.url.database)
    audit = path.parent / "state/audit/jc_relink.csv"
    if existing_audit:
        with session_scope(db) as s:
            s.get(Match, JC).home_team_id, s.get(Match, JC).away_team_id = (
                s.get(Match, JC).away_team_id,
                s.get(Match, JC).home_team_id,
            )
        assert jc_counterpart.relink_jc(path)["skipped"] == 1
        with session_scope(db) as s:
            s.get(Match, JC).home_team_id, s.get(Match, JC).away_team_id = (
                s.get(Match, JC).away_team_id,
                s.get(Match, JC).home_team_id,
            )
    before = path.read_bytes()
    previous_audit = audit.read_bytes() if existing_audit else None
    writer = jc_counterpart._write_audit

    def fail_after_write(path, rows):
        writer(path, rows)
        raise OSError("synthetic failure after audit write")

    monkeypatch.setattr(jc_counterpart, "_write_audit", fail_after_write)
    with pytest.raises(OSError, match="synthetic failure"):
        jc_counterpart.relink_jc(path)
    assert path.read_bytes() == before
    assert (audit.read_bytes() if audit.exists() else None) == previous_audit


def test_ingestion_late_jc_preserves_all_evidence_and_repeat_links(db, provider):
    with session_scope(db) as s:
        before = unchanged_columns(s)
        first = ingest(s, provider)
        assert first["linked_late_jc"] == 1
        assert first["suspected_duplicates"] == 0
        assert first["links"][0]["jc_id"] == JC
        assert s.get(Match, JC).counterpart_id == f"{provider}:{SAMPLE['provider_match_id']}"
        # Alias resolution already knows these canonical identities; no model/results mutation.
        assert unchanged_columns(s) == before
        second = ingest(s, provider)
        assert second["linked_existing"] == 1
        assert second["linked_late_jc"] == 0
        assert second["suspected_duplicates"] == 0
        assert unchanged_columns(s) == before


@pytest.mark.parametrize("hours", [-3, 3])
def test_ingestion_includes_both_window_endpoints(db, provider, hours):
    with session_scope(db) as s:
        s.get(Match, JC).kickoff_utc = WHEN + timedelta(hours=hours)
        assert ingest(s, provider)["linked_late_jc"] == 1


@pytest.mark.parametrize("kind,reason", CONFLICTS)
def test_ingestion_does_not_guess_late_jc(db, provider, kind, reason):
    with session_scope(db) as s:
        conflict(s, provider, kind)
        before = unchanged_columns(s)
        report = ingest(s, provider)
        assert report["suspected_duplicates"] + report["rejected_rows"] == 1
        assert report.get("linked_late_jc", 0) == 0
        assert unchanged_columns(s) == before


@pytest.mark.parametrize("changed", ["home", "league", "kickoff"])
def test_current_api_observation_must_also_match_stored_row(db, provider, changed):
    with session_scope(db) as s:
        existing = s.get(Match, f"{provider}:{SAMPLE['provider_match_id']}")
        if changed == "home":
            existing.home_team_id = list(s.scalars(select(Team.id).order_by(Team.id)))[2]
        elif changed == "league":
            existing.league = "欧冠"
        else:
            existing.kickoff_utc = WHEN - timedelta(days=1)
        s.flush()
        before = unchanged_columns(s)
        report = ingest(s, provider)
        assert report["suspected_duplicates"] == 1
        assert s.get(Match, JC).counterpart_id is None
        assert unchanged_columns(s) == before


def test_pooled_training_refuses_unlinked_duplicates_then_excludes_api(db, provider):
    from zucai.predict.backtest import read_history
    from zucai.predict.dixon_coles import DixonColes
    from zucai.sources.jc_counterpart import relink_jc

    history = pd.read_csv(ROOT / "data/fixtures/t21/model-history.csv", comment="#")
    with session_scope(db) as s:
        for match in s.scalars(select(Match)):
            match.kickoff_utc = SAMPLE["pooled_kickoff_utc"]
            s.get(Result, match.match_id).available_utc = datetime.fromisoformat(
                SAMPLE["pooled_kickoff_utc"]
            ) + timedelta(hours=3)
        ids = {}
        for name in sorted(set(history.home) | set(history.away)):
            team = Team(name=name)
            s.add(team)
            s.flush()
            ids[name] = team.id
        for row in history.itertuples():
            upsert_match(
                s,
                match_id=row.match_id,
                source=row.source,
                league=row.league,
                home_team_id=ids[row.home],
                away_team_id=ids[row.away],
                kickoff_utc=row.kickoff_utc,
                status="finished",
            )
            s.add(
                Result(
                    match_id=row.match_id,
                    home_goals=row.home_goals,
                    away_goals=row.away_goals,
                    available_utc=row.available_utc,
                    available_source=row.available_source,
                )
            )
    scope = sorted(set(history.league) | {SAMPLE["league"]})
    with session_scope(db) as s, pytest.raises(ValueError, match="重复|重叠"):
        read_history(s, source="jc,fd,fdorg,apif", training_leagues=scope)
    assert relink_jc(Path(db.url.database))["linked"] == 1
    with session_scope(db) as s:
        clean = read_history(s, source="jc,fd,fdorg,apif", training_leagues=scope)
    assert clean.attrs["linked_rows_excluded"] == [f"{provider}:{SAMPLE['provider_match_id']}"]
    assert len(clean) == len(history) + 1
    model = DixonColes(pooled=True).fit(clean, as_of=pd.Timestamp(SAMPLE["pooled_cutoff_utc"]))
    assert model.league == "欧战"
