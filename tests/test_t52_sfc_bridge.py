"""A missing JC target cannot suppress verified national identities or hide conflicts."""

import copy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from zucai.db import get_engine, init_db
from zucai.models import Match, Team
from zucai.sources.c500 import C500Sfc, SfcFixture, SfcSnapshot
from zucai.sources.sfc import _snapshot_rows, ingest_issue, refresh_snapshot


@pytest.fixture
def national_bridge(tmp_path):
    engine = get_engine(tmp_path / "national.db")
    init_db(engine)
    with Session(engine) as session:
        home, away = (Team(name=n, aliases=[]) for n in ("亚美尼亚", "塞浦路斯"))
        session.add_all([home, away])
        session.flush()
        when = datetime(2026, 11, 12, 17, tzinfo=UTC)
        candidate = Match(
            match_id="apif:1528959",
            source="apif",
            league="欧国联",
            home_team_id=home.id,
            away_team_id=away.id,
            kickoff_utc=when,
            status="scheduled",
        )
        session.add(candidate)
        session.flush()
        session.expire_all()
        fixture = SfcFixture(
            seq=1,
            fixture_id="1398678",
            home="亚美尼",
            away="塞浦路",
            league="欧国联",
            kickoff_utc=when,
            ended=False,
            home_id="101",
            away_id="84",
        )
        bridge = dict(
            fixture_id=fixture.fixture_id,
            jc_id="jc:2042693",
            home_id="101",
            away_id="84",
            home="亚美尼亚",
            away="塞浦路斯",
            league="欧国联",
            kickoff_utc=when.isoformat(),
            provenance=dict(
                url="https://trade.500.com/jczq/",
                observed_utc="2026-11-11T15:00:00Z",
                raw_sha256="b" * 64,
            ),
        )
        yield session, candidate, fixture, bridge
    engine.dispose()


def snapshot_row(session, fixture, bridge):
    issue = SfcSnapshot("26163", None, [fixture], "sale_end_year")
    return _snapshot_rows(session, issue, {fixture.fixture_id: bridge}, mapped_only=True)[0]


def test_dead_bridge_uses_strict_national_mapping_and_keeps_original(national_bridge):
    session, candidate, fixture, bridge = national_bridge
    original = copy.deepcopy(bridge)
    row = snapshot_row(session, fixture, bridge)
    assert row["match_id"] == candidate.match_id
    assert row["mapping"] == "matched"
    assert row["mapping_evidence"]["basis"] == "national_team_ids_and_exact_kickoff"
    assert row["mapping_evidence"]["bridge_fallback_reason"] == "target_match_missing"
    assert row["mapping_evidence"]["bridge"] == original
    assert bridge == original
    assert session.get(Match, bridge["jc_id"]) is None
    assert session.scalar(select(func.count()).select_from(Match)) == 1


def test_refresh_dead_bridge_is_repeatable_and_does_not_write_old_snapshot(national_bridge):
    session, candidate, fixture, bridge = national_bridge
    old = dict(
        seq=fixture.seq,
        fixture_id=fixture.fixture_id,
        home=fixture.home,
        away=fixture.away,
        league=fixture.league,
        kickoff_utc=fixture.kickoff_utc.isoformat(),
        provider_home_id=fixture.home_id,
        provider_away_id=fixture.away_id,
        match_id=None,
        mapping="unmatched",
        kickoff_basis="provider",
        mapping_evidence={"basis": "provider_fixture_and_team_ids", **bridge},
    )
    issue = SimpleNamespace(issue_no="26163", date_basis="sale_end_year", snapshot=[old])
    original = copy.deepcopy(issue.snapshot)
    first = refresh_snapshot(session, issue)
    assert first[0]["match_id"] == candidate.match_id
    assert issue.snapshot == original
    assert refresh_snapshot(session, issue) == first
    refreshed_issue = SimpleNamespace(issue_no="26163", date_basis=issue.date_basis, snapshot=first)
    assert refresh_snapshot(session, refreshed_issue) == first


@pytest.mark.parametrize("wrong", ["sides", "kickoff", "league"])
def test_existing_wrong_bridge_target_remains_conflict(national_bridge, wrong):
    session, candidate, fixture, bridge = national_bridge
    target = Match(
        match_id=bridge["jc_id"],
        source="jc",
        league=candidate.league,
        home_team_id=candidate.home_team_id,
        away_team_id=candidate.away_team_id,
        kickoff_utc=fixture.kickoff_utc,
        status="scheduled",
    )
    if wrong == "sides":
        target.home_team_id, target.away_team_id = target.away_team_id, target.home_team_id
    elif wrong == "kickoff":
        target.kickoff_utc = fixture.kickoff_utc + timedelta(minutes=1)
    else:
        target.league = "欧冠"
    session.add(target)
    session.flush()
    session.expire_all()
    row = snapshot_row(session, fixture, bridge)
    assert row["match_id"] is None
    assert row["mapping"] == "ambiguous"
    assert row["reason"] == "conflicting_identity"
    assert row["mapping_evidence"]["bridge"] == bridge
    assert target.match_id in row["candidate_ids"]


@pytest.mark.parametrize("wrong", ["provider_id", "label", "exact_kickoff"])
def test_dead_bridge_never_relaxes_national_identity_or_clock(national_bridge, wrong):
    session, candidate, fixture, bridge = national_bridge
    if wrong == "provider_id":
        bridge["home_id"] = "84"
    elif wrong == "label":
        fixture = replace(fixture, home="塞浦路")
    else:
        fixture = replace(fixture, kickoff_utc=fixture.kickoff_utc + timedelta(minutes=1))
    row = snapshot_row(session, fixture, bridge)
    assert row["match_id"] is None
    assert row["mapping"] != "matched"
    if wrong != "exact_kickoff":
        assert row["reason"] == "conflicting_identity"


def test_missing_non_national_bridge_keeps_previous_policy(national_bridge):
    session, candidate, fixture, bridge = national_bridge
    candidate.league = "欧冠"
    fixture = replace(fixture, league="欧冠")
    bridge["league"] = "欧冠"
    session.flush()
    row = snapshot_row(session, fixture, bridge)
    assert row["match_id"] is None
    assert row["mapping_evidence"]["basis"] == "provider_fixture_and_team_ids"


@pytest.mark.parametrize("wrong", ["league", "kickoff"])
def test_dead_bridge_conflicting_source_identity_is_not_discarded(national_bridge, wrong):
    session, candidate, fixture, bridge = national_bridge
    if wrong == "league":
        bridge["league"] = "欧冠"
    else:
        bridge["kickoff_utc"] = (fixture.kickoff_utc + timedelta(minutes=1)).isoformat()
    row = snapshot_row(session, fixture, bridge)
    assert row["match_id"] is None
    assert row["mapping"] == "ambiguous"
    assert row["reason"] == "conflicting_identity"
    assert row["mapping_evidence"]["bridge"] == bridge


@pytest.mark.parametrize("linked", [False, True])
def test_dead_bridge_is_checked_again_if_target_later_appears(national_bridge, linked):
    session, candidate, fixture, bridge = national_bridge
    row = snapshot_row(session, fixture, bridge)
    assert row["match_id"] == candidate.match_id
    issue = SimpleNamespace(issue_no="26163", date_basis="sale_end_year", snapshot=[row])
    session.add(
        Match(
            match_id=bridge["jc_id"],
            source="jc",
            league="欧国联",
            home_team_id=candidate.away_team_id,
            away_team_id=candidate.home_team_id,
            kickoff_utc=fixture.kickoff_utc,
            status="scheduled",
            counterpart_id=candidate.match_id if linked else None,
        )
    )
    session.flush()
    session.expire_all()
    result = refresh_snapshot(session, issue)[0]
    assert result["match_id"] is None
    assert result["reason"] == "conflicting_identity"


@pytest.mark.parametrize("pointer", ["candidate", "archived"])
def test_dead_bridge_accepts_later_correct_counterpart_without_reingesting_issue(
    national_bridge, pointer
):
    session, archived, fixture, bridge = national_bridge
    row = snapshot_row(session, fixture, bridge)
    issue = SimpleNamespace(issue_no="26163", date_basis="sale_end_year", snapshot=[row])
    frozen = copy.deepcopy(issue.snapshot)
    candidate = Match(
        match_id=bridge["jc_id"],
        source="jc",
        league=archived.league,
        home_team_id=archived.home_team_id,
        away_team_id=archived.away_team_id,
        kickoff_utc=fixture.kickoff_utc,
        status="scheduled",
        counterpart_id=archived.match_id if pointer == "candidate" else None,
    )
    if pointer == "archived":
        archived.counterpart_id = candidate.match_id
    session.add(candidate)
    session.flush()
    session.expire_all()

    refreshed = refresh_snapshot(session, issue)[0]
    assert refreshed["mapping"] == "matched"
    assert refreshed["match_id"] == candidate.match_id
    assert refreshed["matched_identity"]["source"] == "jc"
    assert refreshed["mapping_evidence"]["basis"] == "provider_fixture_and_team_ids"
    assert issue.snapshot == frozen


@pytest.mark.parametrize("pointer", [None, "apif:another"])
def test_dead_bridge_does_not_replace_frozen_identity_without_exact_counterpart(
    national_bridge, pointer
):
    session, archived, fixture, bridge = national_bridge
    row = snapshot_row(session, fixture, bridge)
    issue = SimpleNamespace(issue_no="26163", date_basis="sale_end_year", snapshot=[row])
    session.add(
        Match(
            match_id=bridge["jc_id"],
            source="jc",
            league=archived.league,
            home_team_id=archived.home_team_id,
            away_team_id=archived.away_team_id,
            kickoff_utc=fixture.kickoff_utc,
            status="scheduled",
            counterpart_id=pointer,
        )
    )
    session.flush()
    session.expire_all()
    refreshed = refresh_snapshot(session, issue)[0]
    assert refreshed["mapping"] == "ambiguous"
    assert refreshed["match_id"] is None
    assert refreshed["reason"] == "期次原场次待复核"


@pytest.mark.parametrize("mapping", ["matched", "unmatched", "ambiguous"])
def test_next_ingest_preserves_dead_bridge_authority(national_bridge, mapping):
    session, candidate, fixture, bridge = national_bridge
    if mapping == "unmatched":
        session.delete(candidate)
        session.flush()
    elif mapping == "ambiguous":
        session.add(
            Match(
                match_id="apif:duplicate",
                source=candidate.source,
                league=candidate.league,
                home_team_id=candidate.home_team_id,
                away_team_id=candidate.away_team_id,
                kickoff_utc=candidate.kickoff_utc,
                status=candidate.status,
            )
        )
        session.flush()
        session.expire_all()
    raw = (Path(__file__).parents[1] / "data/fixtures/t07/26156.html").read_bytes()
    base = C500Sfc().parse(raw)[0]
    issue = replace(base, issue_no="26163", fixtures=[fixture, *base.fixtures[1:]])
    observed = fixture.kickoff_utc - timedelta(days=1)
    record = ingest_issue(
        session,
        issue,
        observed=observed,
        url=C500Sfc.url,
        raw_sha256="a" * 64,
        bridges=[bridge],
        mapped_only=True,
    )
    assert record.snapshot[0]["mapping"] == mapping
    assert record.snapshot[0]["mapping_evidence"]["bridge"] == bridge
    changed_bridge = dict(bridge, jc_id="jc:9999999")
    record = ingest_issue(
        session,
        issue,
        observed=observed + timedelta(minutes=1),
        url=C500Sfc.url,
        raw_sha256="b" * 64,
        bridges=[changed_bridge],
        mapped_only=True,
    )
    row = record.snapshot[0]
    assert row["match_id"] is None
    assert row["reason"] == "conflicting_identity"
    assert row["mapping_evidence"]["previous_bridge"] == bridge
    assert row["mapping_evidence"]["bridge"] == changed_bridge
