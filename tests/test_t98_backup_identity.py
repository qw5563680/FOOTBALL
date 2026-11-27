from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select

from zucai.db import get_engine, init_db, session_scope
from zucai.jc_backup_identity import FixtureRejection, resolve_fixture
from zucai.models import Match, Team
from zucai.sources.c500 import C500JcFixture

WHEN = datetime(2026, 11, 20, 17, 30, tzinfo=UTC)
HISTORY = datetime(2026, 11, 1, 14, tzinfo=UTC)


FIXTURE = C500JcFixture(
    match_id="jc:2043001",
    provider_fixture_id="1429930",
    jc_num="周五001",
    sales_date=date(2026, 11, 20),
    league="德乙",
    home="达姆施",
    away="海登",
    home_id="1926",
    away_id="5315",
    kickoff_utc=WHEN,
)


def _match(key, home, away, *, league="德乙", at=HISTORY, status="finished", counterpart=None):
    return Match(
        match_id=key,
        source=key.split(":", 1)[0],
        league=league,
        home_team_id=home,
        away_team_id=away,
        kickoff_utc=at,
        status=status,
        counterpart_id=counterpart,
    )


@pytest.fixture
def session():
    engine = get_engine(":memory:")
    init_db(engine)
    try:
        with session_scope(engine) as db:
            db.add_all(
                [
                    Team(id=10, name="达姆施塔", aliases=["旧简称"]),
                    Team(id=20, name="海登海姆", aliases=[]),
                    Team(id=30, name="柏林赫塔", aliases=[]),
                    Team(id=40, name="apif:team:99", aliases=["未经核验"]),
                ]
            )
            db.flush()
            db.add_all(
                [
                    _match("apif:1", 10, 30),
                    _match("apif:2", 30, 20),
                    _match("apif:3", 40, 30),
                    _match("apif:4", 10, 20, league="瑞超"),
                ]
            )
            db.flush()
            yield db
    finally:
        engine.dispose()


def test_approved_provider_ids_and_exact_names_reuse_existing_teams(session):
    row = FIXTURE
    identity = resolve_fixture(session, row, [row])
    assert (identity.league, identity.home_team_id, identity.away_team_id) == ("德乙", 10, 20)
    assert identity.evidence["teams"]["home"]["basis"] == "approved_club_short_name"
    assert identity.evidence["teams"]["home"]["provider_team_id"] == "1926"

    exact = replace(row, home="达姆施塔", away="海登海姆")
    assert resolve_fixture(session, exact, [exact]).evidence["teams"]["home"]["basis"] == (
        "exact_team_name"
    )
    assert session.scalar(select(func.count()).select_from(Team)) == 4
    assert session.scalar(select(func.count()).select_from(Match)) == 4


@pytest.mark.parametrize(
    ("change", "side", "printed", "provider_id"),
    [
        ({"away_id": "999"}, "away", "海登", "999"),
        ({"home": "达姆"}, "home", "达姆", "1926"),
        ({"home": "柏林赫塔"}, "home", "柏林赫塔", "1926"),
        ({"home_id": ""}, "home", "达姆施", ""),
        ({"home": "未经核验", "home_id": "732"}, "home", "未经核验", "732"),
    ],
)
def test_one_unverified_side_reports_500_id_and_label(session, change, side, printed, provider_id):
    row = replace(FIXTURE, **change)
    with pytest.raises(FixtureRejection, match=printed) as failed:
        resolve_fixture(session, row, [row])
    assert failed.value.code == "team_unverified"
    assert any(
        gap["side"] == side and gap["label"] == printed and gap["provider_team_id"] == provider_id
        for gap in failed.value.details["teams"]
    )


def test_existing_alias_needs_the_same_ordered_fixture_not_just_history(session):
    row = replace(FIXTURE, home="旧简称", home_id="900")
    with pytest.raises(FixtureRejection, match="旧简称"):
        resolve_fixture(session, row, [row])
    session.add(_match("apif:8", 10, 20, at=WHEN, status="scheduled"))
    session.flush()
    identity = resolve_fixture(session, row, [row])
    assert identity.evidence["teams"]["home"]["basis"] == "verified_same_fixture_alias"
    assert identity.evidence["fixture_witnesses"] == ["apif:8"]


def test_league_is_exact_or_uses_the_existing_sfc_mapping(session):
    row = replace(FIXTURE, league="瑞典超", home="达姆施塔", away="海登海姆")
    assert resolve_fixture(session, row, [row]).league == "瑞超"
    unknown = replace(row, league="瑞典超级联赛")
    with pytest.raises(FixtureRejection, match="瑞典超级联赛") as failed:
        resolve_fixture(session, unknown, [unknown])
    assert failed.value.code == "league_unverified"


def test_midnight_needs_corroboration_and_batch_clashes_block_it(session):
    midnight = datetime(2026, 11, 20, 16, 0, tzinfo=UTC)
    row = replace(FIXTURE, kickoff_utc=midnight)
    with pytest.raises(FixtureRejection, match="00:00") as failed:
        resolve_fixture(session, row, [row])
    assert failed.value.code == "kickoff_placeholder"

    session.add(_match("apif:8", 10, 20, at=midnight, status="scheduled"))
    session.flush()
    assert resolve_fixture(session, row, [row]).evidence["fixture_witnesses"] == ["apif:8"]
    clash = replace(row, match_id="jc:2043002", provider_fixture_id="248", away_id="3")
    with pytest.raises(FixtureRejection, match="同批"):
        resolve_fixture(session, row, [row, clash])


def test_same_fixture_cannot_gain_second_official_id_and_side_reversal_is_refused(session):
    row = FIXTURE
    session.add(_match("jc:2042999", 10, 20, at=WHEN, status="scheduled"))
    session.flush()
    with pytest.raises(FixtureRejection, match="另一场体彩比赛"):
        resolve_fixture(session, row, [row])
    session.delete(session.get(Match, "jc:2042999"))
    session.add(_match("fdorg:8", 20, 10, at=WHEN, status="scheduled"))
    session.flush()
    with pytest.raises(FixtureRejection, match="主客或联赛冲突"):
        resolve_fixture(session, row, [row])


def test_batch_duplicates_conflicting_provider_link_and_backup_as_evidence_are_refused(session):
    row = FIXTURE
    other = replace(row, match_id="jc:2043002")
    with pytest.raises(FixtureRejection, match="比赛编号重复"):
        resolve_fixture(session, row, [row, other])

    session.add(_match("c500:1429930", 10, 20, at=WHEN, counterpart="jc:2042999"))
    session.flush()
    with pytest.raises(FixtureRejection, match="另一场体彩比赛"):
        resolve_fixture(session, row, [row])
    session.delete(session.get(Match, "c500:1429930"))

    session.add(_match("jc:2043010", 10, 20, league="临时联赛"))
    session.flush()
    new_league = replace(row, league="临时联赛", home="达姆施塔", away="海登海姆")
    with pytest.raises(FixtureRejection, match="临时联赛") as unverified:
        resolve_fixture(session, new_league, [new_league], backup_ids={"jc:2043010"})
    assert unverified.value.code == "league_unverified"
