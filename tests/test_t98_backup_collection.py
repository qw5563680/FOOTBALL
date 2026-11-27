import json
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import func, select

from test_cloud_feeds import feed_json, run_cloud_sync
from zucai import cloud_feeds
from zucai.db import get_engine, init_db, session_scope
from zucai.jc_fixture_backup import STATE_FILE, collect_backup, read_state
from zucai.models import Match, Odds, Team
from zucai.sources import sporttery
from zucai.sources.c500 import URL, C500JcRoot, parse_jc_fixtures

FIXTURES = Path(__file__).resolve().parents[1] / "data/fixtures"
ROOT = (FIXTURES / "c500_root_20261102.html").read_bytes()
MID = "jc:2042439"
DAY = date(2026, 11, 2)
AT = datetime(2026, 11, 2, 9, tzinfo=UTC)
RETURNED = AT.replace(hour=10)
ORIGINAL_CALCULATOR = sporttery.SportteryCalculator


@pytest.fixture
def seed(tmp_path):
    db = tmp_path / "seed.db"
    engine = get_engine(db)
    init_db(engine)
    with session_scope(engine) as session:
        session.add_all(
            [
                Team(id=1, name="萨索洛", aliases=[]),
                Team(id=2, name="佛罗伦萨", aliases=[]),
                Team(id=3, name="博洛尼亚", aliases=[]),
                Team(id=4, name="圣旺红星", aliases=[]),
            ]
        )
        session.flush()
        session.add_all(
            [
                Match(
                    match_id=key,
                    source="fd",
                    home_team_id=h,
                    away_team_id=a,
                    kickoff_utc=AT,
                    status="scheduled",
                    league=league,
                )
                for key, league, h, a in (("fd:1", "意甲", 1, 2), ("fd:2", "法乙", 4, 3))
            ]
        )
    engine.dispose()
    return db


def offline_root(monkeypatch, *, raw=ROOT, captured=AT):
    calls = []

    def read(bridge):
        calls.append(bridge)
        return raw, dict(
            url=URL, fetched_at_utc=captured.isoformat(), raw_sha256=sha256(raw).hexdigest()
        )

    monkeypatch.setattr(C500JcRoot, "_read", staticmethod(read))
    return calls


def test_complete_without_existing_root_causes_no_request_or_state(seed, monkeypatch):
    calls = offline_root(monkeypatch)
    before = seed.read_bytes()
    assert collect_backup(seed, {"schedule_status": "complete"}, C500JcRoot(), at=AT) is None
    assert not calls and not (seed.parent / STATE_FILE).exists()
    assert seed.read_bytes() == before


def test_complete_with_full_official_catalog_has_no_backup_output(seed, monkeypatch):
    calls = offline_root(monkeypatch)
    catalogue = seed.parent / "state/audit/jc_catalog/2026-11.csv"
    catalogue.parent.mkdir(parents=True)
    catalogue.write_text(
        "match_id,captured_utc\n"
        + "".join(f"{row.match_id},{AT.isoformat()}\n" for row in parse_jc_fixtures(ROOT))
    )
    root = C500JcRoot()
    root.snapshot()
    before = seed.read_bytes()
    assert (
        collect_backup(
            seed,
            {"schedule_status": "complete", "schedule_updated_utc": AT.isoformat()},
            root,
            at=AT,
        )
        is None
    )
    assert len(calls) == 1 and not (seed.parent / STATE_FILE).exists()
    assert seed.read_bytes() == before


@pytest.mark.parametrize("occupied_slot", [False, True])
def test_successful_official_schedule_respects_ids_and_sale_slots(seed, monkeypatch, occupied_slot):
    calls = offline_root(monkeypatch)
    catalogue = seed.parent / "state/audit/jc_catalog/2026-11.csv"
    catalogue.parent.mkdir(parents=True)
    catalogue.write_text(
        "match_id,captured_utc,business_date,jc_num\n"
        + "".join(
            f"{row.match_id},{AT.isoformat()},{row.sales_date},{row.jc_num}\n"
            for row in parse_jc_fixtures(ROOT)
            if row.match_id != MID
        )
        + (f"jc:9999999,{AT.isoformat()},{DAY.isoformat()},周一002\n" if occupied_slot else ""),
        encoding="utf-8",
    )
    root = C500JcRoot()
    root.snapshot()
    summary = collect_backup(
        seed, {"schedule_status": "complete", "schedule_updated_utc": AT.isoformat()}, root, at=AT
    )
    assert len(calls) == 1
    if occupied_slot:
        assert summary is None and not read_state(seed.parent)["active"]
    else:
        assert summary["inserted"] == 1 and summary["gaps"] == []
        assert summary["official_schedule_status"] == "complete"
        assert set(read_state(seed.parent)["active"]) == {MID}


def mock_sync_sources(monkeypatch, now):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now[0].astimezone(tz)

    def collect_sfc(_, *, jc_root, **kw):
        jc_root.snapshot()
        return dict(status="complete", issue_no=None, requests=2, errors=[])

    monkeypatch.setattr(cloud_feeds, "datetime", Clock)
    monkeypatch.setattr(cloud_feeds, "fetch_results", lambda *args: None)
    monkeypatch.setattr("zucai.sources.sfc.collect_current_sfc", collect_sfc)


def mock_official(monkeypatch, *, selling, captured=RETURNED, complete_identity=True):
    group = feed_json("sporttery_calculator_20261101.json")["value"]["matchInfoList"][0]
    row = next(game for game in group["subMatchList"] if game["matchStatus"] == "Selling")
    row.update(
        matchId=MID.split(":")[1],
        matchNumStr="周一012",
        matchDate="2026-11-03",
        matchTime="01:45:00",
        homeTeamAbbName="博洛尼亚",
        awayTeamAbbName="佛罗伦萨",
        leagueAbbName="意甲",
    )
    if not selling:
        row.update(matchStatus="Stopped", sellStatus="0")
    if not complete_identity:
        row.pop("homeTeamAbbName")
    raw = json.dumps(
        {
            "success": True,
            "errorCode": 0,
            "value": {"matchInfoList": [{"businessDate": DAY.isoformat(), "subMatchList": [row]}]},
        }
    ).encode()

    class Store:
        @staticmethod
        def load(_):
            return raw

        @staticmethod
        def load_metadata(_):
            return {"fetched_at_utc": captured.isoformat()}

    class Sample(ORIGINAL_CALCULATOR):
        def __init__(self):
            super().__init__(store=Store())

        def fetch_raw(self):
            return "official-fixture"

    monkeypatch.setattr(sporttery, "SportteryCalculator", Sample)
    monkeypatch.setattr(cloud_feeds, "fetch_schedule", sporttery.fetch_schedule)


@pytest.mark.parametrize("selling, withheld", [(True, False), (False, False), (True, True)])
def test_archived_root_sync_and_authoritative_official_recovery(
    seed, tmp_path, monkeypatch, selling, withheld
):
    archive = tmp_path / "archive"
    cloud_feeds.export_feeds(seed, archive, date(2026, 11, 1), "seed")
    now = [AT]
    mock_sync_sources(monkeypatch, now)

    def unavailable(**kw):
        raise ValueError("fixture offline control")

    monkeypatch.setattr(cloud_feeds, "fetch_schedule", unavailable)
    calls = offline_root(monkeypatch)
    run_cloud_sync(monkeypatch, tmp_path / "first", archive, DAY.isoformat())
    manifest = cloud_feeds.archive_info(archive)
    collection = manifest["collection"]["jc_backup"]
    assert collection["official_schedule_status"] == "failed"
    assert collection["inserted"] == collection["active"] == 1
    assert manifest["counts"]["odds"] == 0 and manifest["counts"]["teams"] == 4
    assert len(calls) == 1
    state = read_state(archive)
    proof = state["active"][MID]
    assert proof["url"] == URL and proof["raw_sha256"] == sha256(ROOT).hexdigest()
    assert proof["sale_status"] == "未核实"
    assert proof["observed_utc"] == sporttery.utc_stamp(AT)
    gaps = {g["match_id"]: g for g in collection["gaps"]}
    assert "南锡" in gaps["jc:2042442"]["reason"]
    assert "1130" in gaps["jc:2042442"]["reason"]
    assert gaps["jc:2042438"]["code"] in {"league_unverified", "kickoff_placeholder"}

    # Identical pinned data can be restored repeatedly, including source proof.
    restored_db = tmp_path / "restored" / "work.db"
    cloud_feeds.restore_feeds(archive, restored_db)
    assert read_state(restored_db.parent) == state
    engine = get_engine(restored_db)
    with session_scope(engine) as session:
        stored = session.get(Match, MID)
        assert (stored.home_team_id, stored.away_team_id, stored.source) == (1, 2, "jc")
        assert session.scalar(select(func.count()).select_from(Odds)) == 0
    engine.dispose()

    # Feed sync owns no frozen forecast path.
    frozen = archive / "forecasts/2026-11-03/backup/forecast.json"
    frozen.parent.mkdir(parents=True)
    frozen.write_bytes(b'{"frozen_batch":"backup"}\n')
    before = frozen.read_bytes()

    now[0] = RETURNED
    if withheld:
        mock_official(monkeypatch, selling=False, complete_identity=False)
        run_cloud_sync(monkeypatch, tmp_path / "only-status", archive, DAY.isoformat())
        stopped = cloud_feeds.archive_info(archive)
        state = read_state(archive)
        assert state["active"][MID]["official_excluded_utc"] == sporttery.utc_stamp(RETURNED)
        assert stopped["collection"]["jc_backup"]["active"] == 0
        assert stopped["collection"]["jc"]["schedule_status"] == "complete"
        assert state["reconciled"][0]["identity_pending"]
        now[0] = RETURNED.replace(hour=11)
    mock_official(monkeypatch, selling=selling, captured=now[0])
    run_cloud_sync(monkeypatch, tmp_path / "returned", archive, DAY.isoformat())
    after = cloud_feeds.archive_info(archive)
    assert after["counts"]["matches"] == manifest["counts"]["matches"]
    assert after["counts"]["teams"] == manifest["counts"]["teams"]
    assert (after["counts"]["odds"] > 0) is selling
    state = read_state(archive)
    assert MID not in state["active"]
    correction = next(
        row
        for row in state["reconciled"]
        if row["match_id"] == MID and not row.get("identity_pending")
    )
    assert correction["changes"]["home"] == {"backup": "萨索洛", "official": "博洛尼亚"}
    assert correction["changes"]["kickoff_utc"]["official"] == "2026-11-02T17:45:00.000000Z"
    assert correction["changes"]["sale_status"]["official"] == (
        "Selling/1" if selling else "Stopped/0"
    )
    assert correction["identity_changed"] and correction["retained"] is selling
    assert frozen.read_bytes() == before
    cloud_feeds.restore_feeds(archive, restored_db)
    engine = get_engine(restored_db)
    with session_scope(engine) as session:
        assert session.get(Match, MID).home_team_id == 3
        assert session.get(Match, MID).jc_num == "周一012"
    engine.dispose()
