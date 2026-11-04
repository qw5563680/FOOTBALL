"""T35 synthetic mutations of the checked-in T26 issue; no live requests."""

import csv
import json
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from test_sfc_cloud_state import BRIDGE_RAW, RAW, database
from test_sfc_daily import files, freeze
from test_sfc_report import NOW
from test_sfc_report import complete_issue as complete_issue
from zucai.cloud_feeds import export_feeds
from zucai.db import session_scope
from zucai.models import Match, SfcIssue, SfcIssueMatch, Team
from zucai.sfc_reports import publish_reports, read_reports
from zucai.sources.c500 import C500FixtureBridge, C500Sfc
from zucai.sources.sfc import ingest_issue

CASES = json.loads((Path(__file__).parents[1] / "data/fixtures/t26/t35-cases.json").read_bytes())


def collect(session, issue, bridges=(), at=NOW):
    return ingest_issue(
        session,
        issue,
        bridges=bridges,
        mapped_only=True,
        observed=at,
        url=C500Sfc.url,
        raw_sha256="a" * 64,
    )


def add_target(session, bridge):
    home = session.scalar(select(Team).where(Team.name == bridge["home"])) or Team(
        name=bridge["home"]
    )
    away = session.scalar(select(Team).where(Team.name == bridge["away"])) or Team(
        name=bridge["away"]
    )
    session.add_all([home, away])
    session.flush()
    match = Match(
        match_id=bridge["jc_id"],
        source="jc",
        league=bridge["league"],
        home_team_id=home.id,
        away_team_id=away.id,
        kickoff_utc=bridge["kickoff_utc"],
        status="scheduled",
    )
    session.add(match)
    session.flush()
    return match


def test_mapped_row_uses_database_kickoff_and_retains_source_clock(tmp_path):
    _, engine = database(tmp_path)
    issue = C500Sfc().parse(RAW)[0]
    bridges = C500FixtureBridge().parse(BRIDGE_RAW)
    source_time = datetime.fromisoformat(CASES["placeholder_utc"])
    issue = replace(
        issue, fixtures=[replace(issue.fixtures[0], kickoff_utc=source_time), *issue.fixtures[1:]]
    )
    with session_scope(engine) as session:
        match = add_target(session, bridges[0])
        match.kickoff_utc = CASES["corrected_kickoff_utc"]
        row = collect(session, issue, bridges).snapshot[0]
        assert row["match_id"] == match.match_id
        assert row["kickoff_utc"] == row["matched_identity"]["kickoff_utc"] == match.kickoff_utc
        assert row["kickoff_basis"] == "database"
        assert row["source_kickoff_utc"] == source_time.isoformat()
    engine.dispose()


@pytest.mark.parametrize(
    "clock,basis", [("placeholder_utc", "placeholder"), ("unmapped_past_utc", "provider")]
)
def test_unmapped_past_clock_does_not_block_freeze(complete_issue, tmp_path, clock, basis):
    engine, _, batch = complete_issue
    with session_scope(engine) as session:
        issue = session.get(SfcIssue, "26156")
        rows = deepcopy(issue.snapshot)
        rows[0].update(
            match_id=None,
            matched_identity=None,
            mapping="unmatched",
            kickoff_utc=CASES[clock],
            source_kickoff_utc=CASES[clock],
            home=CASES["short_home"],
            away=CASES["short_away"],
            reason="unmatched",
        )
        issue.snapshot = rows
        session.execute(delete(SfcIssueMatch).where(SfcIssueMatch.seq == 1))
    archive = tmp_path / "archive"
    export_feeds(Path(engine.url.database), archive, date(2026, 11, 1), "offline")
    before = files(archive)
    batch["predictions"].pop(1)  # Mapped but no model: distinct from the first row's mapping gap.
    freeze(archive, tmp_path / "forecast", batch, "early", NOW)
    assert publish_reports(archive, now=NOW) == ["26156"]
    report = read_reports(archive)[0][1]
    row = report["rows"][0]
    assert row["kickoff_basis"] == basis
    assert row["reason"].startswith("未映射：")
    assert row["probabilities"] is None and row["selection"] == list("hda")
    from zucai.sfc_digest import section

    digest = "\n".join(
        section(
            archive, json.loads((archive / "feed_manifest.json").read_bytes()), {}, NOW, lambda s: s
        )
    )
    assert "未映射：" in digest and "全包＝无预测" in digest and "无模型：" in digest
    assert "无模型：未映射" not in digest
    assert ("占位开球时间" in digest) == (basis == "placeholder")
    assert ("占位开球时间" in (archive / "sfc/26156/report.md").read_text()) == (
        basis == "placeholder"
    )
    assert all((archive / path).read_bytes() == raw for path, raw in before.items())
    frozen = files(archive)
    assert publish_reports(archive, now=NOW + timedelta(minutes=1)) == []
    assert files(archive) == frozen


def test_legitimate_midnight_on_same_date_is_not_placeholder(tmp_path):
    _, engine = database(tmp_path)
    issue = C500Sfc().parse(RAW)[0]
    with session_scope(engine) as session:
        rows = collect(session, issue).snapshot
        assert rows[11]["kickoff_utc"].endswith("16:00:00+00:00")
        assert rows[11]["kickoff_basis"] == "provider"
    engine.dispose()


def test_short_names_remap_after_jc_arrives_and_retry_is_idempotent(tmp_path):
    from sqlalchemy import func, select

    _, engine = database(tmp_path)
    issue = C500Sfc().parse(RAW)[0]
    bridges = C500FixtureBridge().parse(BRIDGE_RAW)
    issue = replace(
        issue,
        fixtures=[
            replace(issue.fixtures[0], home=CASES["short_home"], away=CASES["short_away"]),
            *issue.fixtures[1:],
        ],
    )
    with session_scope(engine) as session:
        assert collect(session, issue).snapshot[0]["match_id"] is None
        target = add_target(session, bridges[0])
        first = deepcopy(collect(session, issue, bridges, NOW + timedelta(minutes=1)).snapshot)
        assert first[0]["match_id"] == target.match_id
        assert first[0]["home"] == CASES["short_home"]
        second = collect(session, issue, bridges, NOW + timedelta(minutes=2))
        assert second.snapshot == first
        assert session.scalar(select(func.count()).select_from(Match)) == 1
        assert session.scalar(select(func.count()).select_from(Team)) == 2
        assert session.scalar(select(func.count()).select_from(SfcIssueMatch)) == 1
    engine.dispose()


def test_conflicting_ids_audited_then_retried_without_name_guessing(tmp_path, monkeypatch):
    from zucai.sources.sfc import collect_current_sfc

    path, engine = database(tmp_path)
    bridges = C500FixtureBridge().parse(BRIDGE_RAW)
    with session_scope(engine) as session:
        target = add_target(session, bridges[0])
        target_id = target.match_id
    engine.dispose()
    # Both fetchers read checked-in pages; no HTTP even to a mocked endpoint.
    page = tmp_path / "page"
    page.write_bytes(RAW)
    bridge_page = tmp_path / "bridge"
    bridge_page.write_bytes(BRIDGE_RAW)
    monkeypatch.setattr(C500Sfc, "fetch_raw", lambda *a, **k: page)
    monkeypatch.setattr(C500FixtureBridge, "fetch_raw", lambda *a, **k: bridge_page)
    from zucai.sources.base import RawStore

    monkeypatch.setattr(RawStore, "load", lambda self, p: p.read_bytes())
    monkeypatch.setattr(
        RawStore,
        "load_metadata",
        lambda self, p: {
            "url": C500Sfc.url,
            "fetched_at_utc": NOW.isoformat(),
        },
    )
    bad = deepcopy(bridges)
    bad[0]["home_id"] = CASES["conflicting_home_id"]
    monkeypatch.setattr(C500FixtureBridge, "parse", lambda *a: bad)
    assert collect_current_sfc(path)["status"] == "complete"
    audit = path.parent / "state/audit/sfc_unmatched.csv"
    raw = audit.read_bytes()
    with audit.open() as stream:
        rows = list(csv.DictReader(stream))
    first = rows[0]
    assert first["reason"] == "conflicting_identity"
    assert first["provider_home_id"] == bridges[0]["home_id"]
    assert json.loads(first["candidates"]) == [target_id]
    evidence = json.loads(first["conflicting_identities"])
    assert evidence["bridge"]["home_id"] == CASES["conflicting_home_id"]
    assert evidence["source_identity"]["home_id"] == bridges[0]["home_id"]
    assert evidence["candidate_identities"][0]["home"] == bridges[0]["home"]
    assert collect_current_sfc(path)["mapped"] == 0
    assert audit.read_bytes() == raw
    monkeypatch.setattr(C500FixtureBridge, "parse", lambda *a: deepcopy(bridges))
    assert collect_current_sfc(path)["mapped"] == 1
    assert collect_current_sfc(path)["mapped"] == 1
    assert audit.read_bytes() == raw  # historical conflicts are retained, never erased


def test_same_forecast_can_freeze_new_mapping_without_rewriting_history(complete_issue, tmp_path):
    engine, _, batch = complete_issue
    bridge = C500FixtureBridge().parse(BRIDGE_RAW)[0]
    batch["models"][0]["source"] = "jc"
    batch["predictions"][0].update(
        match_id=bridge["jc_id"],
        source="jc",
        home=bridge["home"],
        away=bridge["away"],
        kickoff_utc=bridge["kickoff_utc"],
    )
    with session_scope(engine) as session:
        issue = session.get(SfcIssue, "26156")
        rows = deepcopy(issue.snapshot)
        rows[0].update(
            match_id=None,
            matched_identity=None,
            mapping="unmatched",
            mapping_evidence={"basis": "provider_fixture_and_team_ids", **bridge},
        )
        issue.snapshot = rows
        session.execute(delete(SfcIssueMatch).where(SfcIssueMatch.seq == 1))
    archive = tmp_path / "archive"
    export_feeds(Path(engine.url.database), archive, date(2026, 11, 1), "before")
    freeze(archive, tmp_path / "forecast", batch, "early", NOW)
    assert publish_reports(archive, now=NOW) == ["26156"]
    first_path, first = read_reports(archive)[0]
    assert first["rows"][0]["match_id"] is None
    history, forecasts = files(archive / first_path), files(archive / "forecasts")
    with session_scope(engine) as session:
        add_target(session, bridge)
    export_feeds(Path(engine.url.database), archive, date(2026, 11, 1), "after")
    assert publish_reports(archive, now=NOW + timedelta(minutes=1)) == ["26156"]
    assert len(read_reports(archive)) == 2
    current = json.loads((archive / "sfc/26156/sfc.json").read_bytes())
    assert current["rows"][0]["match_id"] == bridge["jc_id"]
    assert current["rows"][0]["probabilities"] == batch["predictions"][0]["model_probabilities"]
    assert files(archive / first_path) == history and files(archive / "forecasts") == forecasts
    before = files(archive)
    assert publish_reports(archive, now=NOW + timedelta(minutes=2)) == []
    assert files(archive) == before


def test_mapped_database_kickoff_blocks_publication_even_if_snapshot_is_future(
    complete_issue, tmp_path
):
    engine, _, batch = complete_issue
    with session_scope(engine) as session:
        match = session.get(Match, batch["predictions"][0]["match_id"])
        match.kickoff_utc = NOW.isoformat()
    archive = tmp_path / "archive"
    export_feeds(Path(engine.url.database), archive, date(2026, 11, 1), "started")
    freeze(archive, tmp_path / "forecast", batch, "early", NOW)
    before = files(archive)
    assert publish_reports(archive, now=NOW) == []
    assert files(archive) == before


def test_rechecks_mapped_kickoff_immediately_before_writing(complete_issue, tmp_path, monkeypatch):
    import zucai.sfc_reports as reports

    engine, _, batch = complete_issue
    kickoff = NOW + timedelta(minutes=5)
    with session_scope(engine) as session:
        session.get(Match, batch["predictions"][0]["match_id"]).kickoff_utc = kickoff.isoformat()
    batch["predictions"][0]["kickoff_utc"] = kickoff.isoformat()
    archive = tmp_path / "archive"
    export_feeds(Path(engine.url.database), archive, date(2026, 11, 1), "before-kickoff")
    freeze(archive, tmp_path / "forecast", batch, "early", NOW)
    before = files(archive)

    class Clock(datetime):
        instants = iter([NOW, kickoff])

        @classmethod
        def now(cls, tz):
            return cls.fromtimestamp(next(cls.instants).timestamp(), tz)

    monkeypatch.setattr(reports, "datetime", Clock)
    assert reports.publish_reports(archive) == []
    assert files(archive) == before
