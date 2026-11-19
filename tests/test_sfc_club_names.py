"""Lottery fixtures JC does not sell: an owner-approved club short name maps the row to its
stored fixture, and that fixture becomes a forecast target for the period whose turn it is."""

import json
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm import Session

from zucai import sfc_club_names
from zucai.db import get_engine, init_db
from zucai.models import Match, SfcIssue, Team
from zucai.sources.c500 import SfcFixture
from zucai.sources.sfc import _map_fixture
from zucai.sources.teams import normalize_team

KICKOFF = datetime(2026, 11, 20, 17, 30, tzinfo=UTC)
NOW = datetime(2026, 11, 19, 10, tzinfo=UTC)
APPROVAL = dict(
    status="approved",
    approved_by="qw5563680",
    approved_utc="2026-11-19T09:30:00Z",
    approval_pr="https://github.com/qw5563680/FOOTBALL/pull/1",
)
TABLE = {
    ("德乙", "1926"): dict(label="达姆施", canonical="达姆施塔", **APPROVAL),
    ("德乙", "5315"): dict(label="海登", canonical="海登海姆", **APPROVAL),
}


def test_repository_table_is_valid():
    table = sfc_club_names.load()
    assert table, "the checked-in table must hold at least one approved name"
    for (league, provider_id), entry in table.items():
        assert league and provider_id.isdecimal()
        assert normalize_team(entry["canonical"]) == entry["canonical"]
        assert entry["label"] != entry["canonical"] and entry["evidence"]


@pytest.mark.parametrize(
    "entry",
    [
        dict(label="达姆施", canonical="达姆施塔", **{**APPROVAL, "approved_by": "someone"}),
        dict(label="达姆施", canonical="达姆施塔", **{**APPROVAL, "approval_pr": "https://x/1"}),
        dict(label="达姆施", canonical="达姆施塔队", **APPROVAL),
        dict(label="达姆施塔", canonical="达姆施塔", **APPROVAL),
        dict(label="达姆施", **APPROVAL),
    ],
)
def test_malformed_or_unauthorised_entries_are_refused(tmp_path, entry):
    path = tmp_path / "clubs.json"
    path.write_text(
        json.dumps(dict(version=1, leagues={"德乙": {"1926": entry}})), encoding="utf-8"
    )
    with pytest.raises(ValueError):
        sfc_club_names.load(path)


def test_unapproved_entry_is_ignored(tmp_path):
    path = tmp_path / "clubs.json"
    entry = dict(TABLE["德乙", "1926"], status="proposed")
    path.write_text(
        json.dumps(dict(version=1, leagues={"德乙": {"1926": entry}})), encoding="utf-8"
    )
    assert sfc_club_names.load(path) == {}


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(sfc_club_names, "_table", lambda: TABLE)
    engine = get_engine(tmp_path / "games.db")
    init_db(engine)
    with Session(engine) as session:
        teams = {
            name: Team(name=name, aliases=[])
            for name in ("达姆施塔", "海登海姆", "比勒费", "马格德堡")
        }
        session.add_all(teams.values())
        session.flush()
        for match_id, source, home, away in (
            ("apif:1576235", "apif", "达姆施塔", "海登海姆"),
            ("jc:2042882", "jc", "比勒费", "马格德堡"),
        ):
            session.add(
                Match(
                    match_id=match_id,
                    source=source,
                    league="德乙",
                    home_team_id=teams[home].id,
                    away_team_id=teams[away].id,
                    status="scheduled",
                    kickoff_utc=KICKOFF,
                )
            )
        session.flush()
        yield session
    engine.dispose()


def _fixture(**changes):
    values = dict(
        seq=3,
        fixture_id="1429930",
        league="德乙",
        home="达姆施",
        away="海登",
        kickoff_utc=KICKOFF,
        ended=False,
        home_id="1926",
        away_id="5315",
    )
    values.update(changes)
    return SfcFixture(**values)


def _state(session):
    teams = [(t.id, t.name, list(t.aliases)) for t in session.query(Team).order_by(Team.id)]
    return teams, [m.match_id for m in session.query(Match).order_by(Match.match_id)]


def test_approved_short_names_reach_the_stored_fixture_without_becoming_aliases(session):
    before = _state(session)
    match, mapping, candidates, evidence = _map_fixture(session, _fixture(), {}, mapped_only=True)
    assert (match.match_id, mapping, candidates) == ("apif:1576235", "matched", ["apif:1576235"])
    assert evidence["basis"] == "approved_club_short_names"
    assert evidence["names"]["home"] == dict(
        provider_id="1926", label="达姆施", canonical="达姆施塔"
    )
    assert evidence["names"]["away"]["canonical"] == "海登海姆"
    # Nothing is written: no alias, no team, no match.
    assert _state(session) == before


@pytest.mark.parametrize(
    "changes",
    [
        dict(home_id="9999"),  # another source team ID with the same printed label
        dict(home="达姆"),  # the right ID with a different printed label
        dict(league="德甲"),  # the entry belongs to another competition
        dict(away="不认识", away_id="1"),  # one side resolved, the other unknown
        dict(home_id=None),
    ],
)
def test_id_label_and_league_must_all_agree(session, changes):
    before = _state(session)
    match, mapping, candidates, _ = _map_fixture(session, _fixture(**changes), {}, mapped_only=True)
    assert (match, mapping, candidates) == (None, "unmatched", [])
    assert _state(session) == before


def _row(seq, fixture_id, home, away, match_id=None, source=None, league="德乙", ids=(None, None)):
    return dict(
        seq=seq,
        fixture_id=fixture_id,
        home=home,
        away=away,
        league=league,
        kickoff_utc=KICKOFF.isoformat(),
        source_kickoff_utc=KICKOFF.isoformat(),
        kickoff_basis="provider" if match_id is None else "database",
        source_ended=False,
        match_id=match_id,
        mapping="matched" if match_id else "unmatched",
        reason=None if match_id else "unmatched",
        candidate_ids=[match_id] if match_id else [],
        mapping_evidence={"basis": "exact_team_names"},
        provider_home_id=ids[0],
        provider_away_id=ids[1],
        matched_identity=dict(
            home=home, away=away, league=league, source=source, kickoff_utc=KICKOFF.isoformat()
        )
        if match_id
        else None,
    )


def _issue(number, end, rows):
    return SfcIssue(
        issue_no=number,
        sale_end_utc=end,
        observed_utc="2026-11-19T08:00:00Z",
        source_url="https://trade.500.com/sfc/",
        raw_sha256="a" * 64,
        date_basis="deadline_anchored",
        snapshot=rows,
    )


def test_period_in_turn_adds_every_mapped_fixture_whether_jc_sells_it_or_not(session):
    from zucai.cloud_predict import open_national_fixtures

    rows = [
        _row(2, "1429804", "比勒费", "马格德堡", "jc:2042882", "jc"),
        _row(3, "1429930", "达姆施", "海登", ids=("1926", "5315")),
    ]
    session.add_all(
        [
            _issue("26165", "2026-11-15T14:00:00Z", []),
            _issue("26166", "2026-11-20T14:00:00Z", rows),
        ]
    )
    session.flush()
    day = date(2026, 11, 21)
    # Both the fixture JC sells and the one it does not are forecast targets.
    assert open_national_fixtures(session, NOW) == {day: {"apif:1576235", "jc:2042882"}}
    # After the deadline nothing is added any more.
    assert open_national_fixtures(session, datetime(2026, 11, 20, 14, tzinfo=UTC)) == {}


def test_period_listed_ahead_of_its_turn_adds_nothing(session):
    from zucai.cloud_predict import open_national_fixtures

    session.add_all(
        [
            _issue("26165", "2026-11-15T14:00:00Z", []),
            _issue(
                "26171",
                "2026-11-26T14:00:00Z",
                [_row(3, "1429930", "达姆施", "海登", ids=("1926", "5315"))],
            ),
        ]
    )
    session.flush()
    assert open_national_fixtures(session, NOW) == {}
