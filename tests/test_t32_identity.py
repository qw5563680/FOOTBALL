"""Synthetic identity/index checks; these fixtures do not prove historical coverage."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from zucai.db import get_engine, init_db, session_scope
from zucai.models import ApifFixtureIndex, Match, Team
from zucai.sources.apif_aliases import validate_approvals
from zucai.sources.apif_stats import instant
from zucai.sources.base import RawStore
from zucai.stats_link import link_fixtures
from zucai.stats_storage import link_archive, read_audit

ROOT = Path(__file__).parents[1]
FIX = ROOT / "data/fixtures/t32"
ALIASES_PATH = ROOT / "data/team_aliases/api_football.json"
APPROVAL = "https://github.com/qw5563680/FOOTBALL/pull/49#issuecomment-6627072639"


def read(path):
    return json.loads(path.read_text())


def metadata(raw):
    return dict(
        captured_utc="2026-11-04T12:00:00Z",
        raw_sha256=hashlib.sha256(json.dumps(raw).encode()).hexdigest(),
        raw_location="fixture://t32/synthetic",
    )


def link(session, raw):
    return link_fixtures(
        session,
        raw,
        league_id=int(raw["parameters"]["league"]),
        season=int(raw["parameters"]["season"]),
        aliases=read(ALIASES_PATH),
        metadata=metadata(raw),
    )


def single_page():
    page = read(FIX / "identity_fixtures_2024.json")["pages"][0]
    page["response"] = page["response"][:1]
    page["results"] = 1
    page["response"][0]["league"]["round"] = "Regular Season - 1"
    return page


def seed(session, pages):
    aliases = read(ALIASES_PATH)
    teams = {}
    for page in pages:
        for row in page["response"]:
            for side in ("home", "away"):
                pid = str(row["teams"][side]["id"])
                entry = next(records[pid] for records in aliases.values() if pid in records)
                if pid not in teams:
                    team = Team(name=entry["canonical"], aliases=[])
                    session.add(team)
                    session.flush()
                    teams[pid] = team.id
            session.add(
                Match(
                    match_id=f"fd:synthetic:{row['fixture']['id']}",
                    source="fd",
                    league=next(league for league, records in aliases.items() if pid in records),
                    home_team_id=teams[str(row["teams"]["home"]["id"])],
                    away_team_id=teams[str(row["teams"]["away"]["id"])],
                    kickoff_utc=row["fixture"]["date"],
                )
            )
    session.flush()


@pytest.fixture
def database(tmp_path):
    database = tmp_path / "feeds.db"
    engine = get_engine(database)
    init_db(engine)
    with session_scope(engine) as session:
        seed(session, [single_page()])
        match = session.scalar(select(Match))
        session.add(
            Match(
                match_id="fdorg:synthetic",
                source="fdorg",
                league=match.league,
                home_team_id=match.home_team_id,
                away_team_id=match.away_team_id,
                kickoff_utc=match.kickoff_utc,
            )
        )
    engine.dispose()
    return database


def test_exact_45_owner_approved_aliases_and_narrow_fd_alias():
    expected = read(FIX / "identity_approved.json")
    followup = read(FIX / "followup_approvals.json")
    aliases = read(ALIASES_PATH)
    assert sum(map(len, expected["approved"].values())) == 45
    for league, records in expected["approved"].items():
        for pid, entry in records.items():
            original = dict(
                entry,
                status="approved",
                approved_by="qw5563680",
                approved_utc="2026-11-04T09:10:00Z",
                approval_pr=APPROVAL,
            )
            correction = followup["changed_apif"].get(league, {}).get(pid)
            if correction:
                original.update(
                    canonical=correction, approval_pr=[APPROVAL, followup["approval_pr"]]
                )
            assert aliases[league][pid] == original
    for pid in expected["unapproved_missing_from_formal"]:
        if not any(pid in records for records in followup["new_apif"].values()):
            assert all(pid not in records for records in aliases.values())
    assert read(ROOT / "data/team_aliases/football_data_co_uk.json")["荷甲"]["Den Haag"] == dict(
        canonical="海牙",
        status="approved",
        approved_by="qw5563680",
        approved_utc="2026-11-04T09:10:00Z",
        approval_pr=APPROVAL,
    )
    validate_approvals(aliases, require_metadata=True)


def test_fourth_batch_formal_aliases_match_only_the_owner_approved_table():
    expected = read(FIX / "followup_approvals.json")
    fd = read(ROOT / "data/team_aliases/football_data_co_uk.json")
    apif = read(ALIASES_PATH)
    for league, records in expected["fd_merges"].items():
        for name, canonical in records.items():
            assert fd[league][name] == dict(canonical=canonical, **expected["metadata"])
    for league, records in expected["new_apif"].items():
        for pid, entry in records.items():
            assert apif[league][pid] == dict(entry, **expected["metadata"])
    for league, ids in expected["unapproved"].items():
        assert not set(ids) & apif[league].keys()
    validate_approvals(fd, require_metadata=True)
    validate_approvals(apif, require_metadata=True)


@pytest.mark.parametrize("season", [2022, 2024])
def test_synthetic_approved_id_pages_link_without_temporary_identity(tmp_path, season):
    fixture = read(FIX / f"identity_fixtures_{season}.json")
    assert fixture["synthetic"] is True
    engine = get_engine(tmp_path / "identities.db")
    init_db(engine)
    try:
        with session_scope(engine) as session:
            seed(session, fixture["pages"])
            before = (
                list(session.scalars(select(Team.id))),
                list(session.scalars(select(Match.match_id))),
            )
            for page in fixture["pages"]:
                result = link(session, page)
                assert result["linked"] == len(page["response"])
                assert result["rejected"] == 0 and result["audit"] == []
            assert len(list(session.scalars(select(ApifFixtureIndex)))) == 45
            assert before == (
                list(session.scalars(select(Team.id))),
                list(session.scalars(select(Match.match_id))),
            )
    finally:
        engine.dispose()


def test_fd_preference_retains_fixture_context_and_audit_is_idempotent(database, tmp_path):
    raw = single_page()
    path = RawStore(tmp_path / "raw").save(
        "api-football",
        "fixtures",
        json.dumps(raw).encode(),
        "json",
        captured_at=instant("2026-11-04T12:00:00Z"),
        url="https://v3.football.api-sports.io/fixtures?league=39&season=2024",
        status_code=200,
    )
    kwargs = dict(league_id=39, season=2024, aliases_path=ALIASES_PATH)
    assert link_archive(database, path, **kwargs) == dict(linked=1, unchanged=0, rejected=0)
    audit = database.parent / "state/audit/stats_link_audit.csv"
    before = audit.read_bytes()
    rows = read_audit(audit)
    assert len(rows) == 1
    assert rows[0]["reason"] == "preferred_fd" and rows[0]["outcome"] == "linked"
    assert json.loads(rows[0]["candidates"]) == [
        f"fd:synthetic:{raw['response'][0]['fixture']['id']}",
        "fdorg:synthetic",
    ]
    assert link_archive(database, path, **kwargs) == dict(linked=0, unchanged=1, rejected=0)
    assert audit.read_bytes() == before
    engine = get_engine(database)
    try:
        with session_scope(engine) as session:
            index = session.scalar(select(ApifFixtureIndex))
            assert index.match_id.startswith("fd:")
            assert index.evidence["fixture"] == raw["response"][0]
            assert len(list(session.scalars(select(Match)))) == 2
    finally:
        engine.dispose()


@pytest.mark.parametrize("case", ["unequal_kickoff", "third_candidate", "other_source"])
def test_other_ambiguities_still_rejected(database, case):
    engine = get_engine(database)
    try:
        with session_scope(engine) as session:
            fdorg = session.get(Match, "fdorg:synthetic")
            if case == "unequal_kickoff":
                fdorg.kickoff_utc = "2024-09-01T15:01:00Z"
            else:
                source = "fd" if case == "third_candidate" else "jc"
                if case == "other_source":
                    session.delete(fdorg)
                session.add(
                    Match(
                        match_id=source + ":other",
                        source=source,
                        league=fdorg.league,
                        home_team_id=fdorg.home_team_id,
                        away_team_id=fdorg.away_team_id,
                        kickoff_utc=fdorg.kickoff_utc,
                    )
                )
            session.flush()
            result = link(session, single_page())
            assert result["linked"] == 0 and result["rejected"] == 1
            assert result["audit"][0]["reason"] == "多对一"
            assert list(session.scalars(select(ApifFixtureIndex))) == []
    finally:
        engine.dispose()


def test_fd_preference_cannot_redirect_incumbent_fdorg_index(database):
    engine = get_engine(database)
    try:
        with session_scope(engine) as session:
            fd = session.scalar(select(Match).where(Match.source == "fd"))
            details = dict(
                match_id=fd.match_id,
                source=fd.source,
                league=fd.league,
                home_team_id=fd.home_team_id,
                away_team_id=fd.away_team_id,
                kickoff_utc=fd.kickoff_utc,
            )
            session.delete(fd)
            session.flush()
            raw = single_page()
            assert link(session, raw)["linked"] == 1
            index = session.scalar(select(ApifFixtureIndex))
            original = copy.deepcopy(index.evidence)
            session.add(Match(**details))
            session.flush()
            result = link(session, raw)
            assert result["linked"] == 0 and result["rejected"] == 1
            assert result["audit"][0]["detail"] == "既有索引禁止重指向"
            assert all(row["reason"] != "preferred_fd" for row in result["audit"])
            assert index.match_id == "fdorg:synthetic" and index.evidence == original
    finally:
        engine.dispose()


@pytest.mark.parametrize("malformed", [{}, {"fixture": {}}, {"fixture": None}])
def test_malformed_first_sibling_does_not_block_valid_fixture_context(database, malformed):
    raw = single_page()
    original = copy.deepcopy(raw["response"][0])
    raw["response"].insert(0, malformed)
    raw["results"] = 2
    engine = get_engine(database)
    try:
        with session_scope(engine) as session:
            result = link(session, raw)
            assert result["linked"] == 1 and result["rejected"] == 1
            rejected = [row for row in result["audit"] if row["outcome"] == "rejected"]
            assert len(rejected) == 1 and rejected[0]["reason"] == "候选不全"
            index = session.scalar(select(ApifFixtureIndex))
            assert index.evidence["fixture"] == original
            assert index.match_id.startswith("fd:")
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "url",
    [
        APPROVAL + "?bad=1",
        APPROVAL.replace("github.com", "github.com.evil"),
        APPROVAL.replace("#issuecomment-6627072639", "#issuecomment-0"),
        APPROVAL.replace("#issuecomment-6627072639", "#issuecomment-abc"),
        APPROVAL.replace("#issuecomment-6627072639", "#discussion_r123"),
        APPROVAL.replace("#issuecomment-6627072639", "/#issuecomment-123"),
    ],
)
def test_approval_comment_validation_rejects_malformed_urls(url):
    alias = copy.deepcopy(read(ALIASES_PATH)["英超"]["51"])
    alias["approval_pr"] = url
    with pytest.raises(ValueError, match="approval_pr"):
        validate_approvals({"英超": {"51": alias}}, require_metadata=True)
