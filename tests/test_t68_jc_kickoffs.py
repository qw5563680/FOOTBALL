"""Offline source identity, probe safety, and durable kickoff evidence tests."""

import hashlib
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import responses

from test_cloud_feeds import seed_cloud_archive
from test_t61_workflow import enabled_jobs
from zucai.cloud_feeds import archive_info, export_feeds, restore_feeds
from zucai.sources.base import RawStore
from zucai.sources.sporttery import API

FIXTURES = Path(__file__).parents[1] / "data/fixtures"
HEADS = FIXTURES / "t68/heads"


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False))


def catalogue(root, ids=(2042404,), leagues=None):
    """Synthetic page, preserving the real official dictionary, with exact IDs."""
    page = json.loads((FIXTURES / "t65/jc-one-result-real.json").read_bytes())
    row = page["value"]["matchResult"][0]
    dictionary = {r["leagueId"]: r for r in page["value"]["leagueList"]}
    rows = []
    for i, sid in enumerate(ids):
        lid = (leagues or [24] * len(ids))[i]
        league = dictionary[lid]
        rows.append(
            dict(
                row,
                matchId=sid,
                matchDate="2026-10-31",
                leagueId=lid,
                leagueName=league["leagueAllName"],
                leagueNameAbbr=league["leagueAbbName"],
                homeTeamId=849,
                awayTeamId=508,
            )
        )
    page["success"] = True
    page["value"].update(matchResult=rows, total=len(rows), pages=1, pageNo=1, pageSize=30)
    raw = json.dumps(page).encode()
    folder = root / "state/catalogue/catalogue"
    path = folder / "jc/2026-10/2026-10-31-p1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    query = (
        "matchBeginDate=2026-10-31&matchEndDate=2026-10-31&leagueId="
        "&pageSize=30&pageNo=1&isFix=0&matchPage=1&pcOrWap=1"
    )
    save_json(
        path.with_suffix(".json.meta.json"),
        dict(
            url=API + "getUniformMatchResultV1.qry?" + query,
            fetched_at_utc="2026-11-14T01:00:00Z",
            status_code=200,
            sha256=hashlib.sha256(raw).hexdigest(),
        ),
    )
    report = dict(
        status="complete", window=dict(start_beijing="2026-10-31", end_beijing="2026-10-31")
    )
    save_json(folder / "jc.json", report)
    save_json(
        root / "state/catalogue/latest.json",
        dict(
            version=1,
            providers=dict(
                jc=dict(
                    run_id="catalogue",
                    path="state/catalogue/catalogue/jc.json",
                    sha256=hashlib.sha256((folder / "jc.json").read_bytes()).hexdigest(),
                )
            ),
        ),
    )


@pytest.fixture
def module(monkeypatch):
    import zucai.jc_catalogue_kickoffs as mod

    monkeypatch.setattr(mod, "CATALOGUE_SOURCE_RUN", "catalogue")
    monkeypatch.setattr(mod, "CATALOGUE_WINDOW", (date(2026, 10, 31), date(2026, 10, 31)))

    for key, val in [
        ("GITHUB_ACTIONS", "true"),
        ("GITHUB_EVENT_NAME", "workflow_dispatch"),
        ("RUNNER_ENVIRONMENT", "self-hosted"),
    ]:
        monkeypatch.setenv(key, val)
    monkeypatch.setattr("zucai.sources.base.time.sleep", lambda _: None)
    return mod


def test_plan_exact_official_ids_and_no_b_generic_association(tmp_path, module):
    catalogue(tmp_path, (2042404, 2042405, 2042406, 2042407, 2042408), [24, 39, 73, 76, 25])
    plan = module.plan_kickoffs(tmp_path)
    assert plan["new_route_count"] == 62
    assert set(plan["official_league_ids"]) == {
        1,
        3,
        4,
        7,
        11,
        13,
        14,
        15,
        19,
        21,
        23,
        24,
        26,
        27,
        28,
        29,
        30,
        34,
        36,
        38,
        39,
        41,
        43,
        45,
        46,
        49,
        52,
        54,
        61,
        66,
        67,
        71,
        72,
        73,
        75,
        76,
        78,
        80,
        82,
        83,
        86,
        90,
        93,
        94,
        95,
        100,
        106,
        118,
        120,
        2068445,
        2068446,
        2070739,
    }
    assert [r["match_id"] for r in plan["pending"]] == [2042404, 2042405, 2042406, 2042407]
    assert plan["selected_count"] == 4


@responses.activate
def test_collect_roundtrip_resume_and_cli(tmp_path, module, monkeypatch):
    archive = seed_cloud_archive(tmp_path)
    root = tmp_path / "work/data"
    root.mkdir(parents=True)
    database = root / "zucai.db"
    restore_feeds(archive, database)
    catalogue(root)
    responses.add(
        responses.GET, API + "getMatchHeadV1.qry", body=(HEADS / "success.json").read_bytes()
    )
    report = module.collect_kickoffs(database, "pilot")
    assert report["requests"] == 1 and report["success_count"] == 1
    assert report["rows"][0]["kickoff_utc"] == "2026-10-31T15:15:00+00:00"
    assert report["pilot_verified"] is True
    export_feeds(database, archive, date(2026, 11, 1), "export")
    files = archive_info(archive)["files"]
    assert "state/catalogue/pilot/jc-heads/2042404.json.meta.json" in files
    assert "state/catalogue/pilot/jc-kickoff-plan.json" in files
    restored = tmp_path / "restored/data/zucai.db"
    restore_feeds(archive, restored)
    again = module.collect_kickoffs(restored, "resume", max_requests=1200)
    assert again["requests"] == 0 and again["success_count"] == 1
    monkeypatch.setattr(
        sys,
        "argv",
        ["kickoffs", "plan", "--archive", str(archive), "--out", str(tmp_path / "plan.json")],
    )
    module.main()
    assert json.loads((tmp_path / "plan.json").read_bytes())["pending"] == []
    assert len(responses.calls) == 1


@pytest.mark.parametrize(
    "fixture,status",
    [
        ("wrong-id", 200),
        ("reversed", 200),
        ("wrong-day", 200),
        ("bad-time", 200),
        ("midnight", 200),
        ("empty", 200),
        ("success", 503),
    ],
)
@responses.activate
def test_failed_heads_never_resume_as_success(tmp_path, module, fixture, status):
    catalogue(tmp_path)
    responses.add(
        responses.GET,
        API + "getMatchHeadV1.qry",
        body=(HEADS / f"{fixture}.json").read_bytes(),
        status=status,
    )
    report = module.collect_kickoffs(tmp_path / "zucai.db", "bad")
    assert report["requests"] == 1 and report["success_count"] == 0
    assert report["pilot_verified"] is False
    assert report["rows"][0]["status"] == "failed"
    with pytest.raises(ValueError, match="pilot"):
        module.collect_kickoffs(tmp_path / "zucai.db", "full", max_requests=1200)
    assert len(module.plan_kickoffs(tmp_path)["pending"]) == 1


@responses.activate
def test_sha_damage_invalidates_success_and_pilot(tmp_path, module):
    catalogue(tmp_path)
    responses.add(
        responses.GET, API + "getMatchHeadV1.qry", body=(HEADS / "success.json").read_bytes()
    )
    module.collect_kickoffs(tmp_path / "zucai.db", "pilot")
    (tmp_path / "state/catalogue/pilot/jc-heads/2042404.json").write_bytes(b"{}")
    assert len(module.plan_kickoffs(tmp_path)["pending"]) == 1
    with pytest.raises(ValueError, match="pilot"):
        module.collect_kickoffs(tmp_path / "zucai.db", "full", max_requests=1200)


def test_verified_existing_raw_head_skips_and_unproven_db_does_not(tmp_path, module):
    catalogue(tmp_path)
    RawStore(tmp_path / "raw").save(
        "sporttery-head",
        "match-2042404",
        (HEADS / "success.json").read_bytes(),
        "json",
        url=API + "getMatchHeadV1.qry?source=web&sportteryMatchId=2042404",
        status_code=200,
        captured_at=datetime(2026, 11, 1, tzinfo=UTC),
    )
    plan = module.plan_kickoffs(tmp_path)
    assert plan["pending"] == [] and len(plan["verified"]) == 1


def test_no_full_batch_without_pilot_and_earliest_five_only(tmp_path, module):
    catalogue(tmp_path, tuple(range(2042404, 2042410)))
    with pytest.raises(ValueError, match="pilot"):
        module.collect_kickoffs(tmp_path / "zucai.db", "full", max_requests=6)
    assert [r["match_id"] for r in module.plan_kickoffs(tmp_path)["pilot"]] == list(
        range(2042404, 2042409)
    )


def test_workflow_routes_jc_kickoffs_only_selfhosted_without_keys():
    assert enabled_jobs("jc-kickoffs") == {"jc-catalogue"}


@responses.activate
def test_cloud_probe_cli_dispatch_without_apif_key(tmp_path, module, monkeypatch):
    from zucai.cloud_feeds import main

    archive = seed_cloud_archive(tmp_path)
    catalogue(tmp_path)
    export_feeds(tmp_path / "seed.db", archive, date(2026, 11, 1), "with-catalogue")
    work = tmp_path / "cli"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.delenv("API_FOOTBALL_KEY", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "feeds",
            "probe",
            "--probe-kind",
            "jc-kickoffs",
            "--archive",
            str(archive),
            "--run-id",
            "cli-pilot",
            "--max-requests",
            "5",
        ],
    )
    responses.add(
        responses.GET, API + "getMatchHeadV1.qry", body=(HEADS / "success.json").read_bytes()
    )
    main()
    report = json.loads((archive / "state/catalogue/cli-pilot/jc-kickoffs.json").read_bytes())
    assert report["success_count"] == 1
    assert report["requests"] == 1


@responses.activate
def test_conflicting_old_original_blocks_pilot_expansion(tmp_path, module):
    catalogue(tmp_path)
    responses.add(
        responses.GET, API + "getMatchHeadV1.qry", body=(HEADS / "success.json").read_bytes()
    )
    module.collect_kickoffs(tmp_path / "zucai.db", "pilot")
    value = json.loads((HEADS / "success.json").read_bytes())
    value["value"]["matchDateTime"] = "2026-10-31 23:30"
    RawStore(tmp_path / "raw").save(
        "sporttery-head",
        "match-2042404",
        json.dumps(value).encode(),
        "json",
        url=API + "getMatchHeadV1.qry?source=web&sportteryMatchId=2042404",
        status_code=200,
        captured_at=datetime(2026, 11, 1, tzinfo=UTC),
    )
    assert (
        module.plan_kickoffs(tmp_path)["pending"][0]["reason"] == "conflicting_jc_kickoff_evidence"
    )
    with pytest.raises(ValueError, match="pilot"):
        module.collect_kickoffs(tmp_path / "zucai.db", "full", max_requests=1200)


def test_original_partial_report_recomputed_without_latest_jc_pointer(tmp_path, module):
    catalogue(tmp_path)
    folder = tmp_path / "state/catalogue"
    (folder / "catalogue").rename(folder / "43539673769-1")
    module.CATALOGUE_SOURCE_RUN = "43539673769-1"
    save_json(folder / "latest.json", dict(version=1, providers={}))
    path = folder / "43539673769-1/jc.json"
    report = json.loads(path.read_bytes())
    report["status"] = "partial"
    save_json(path, report)
    plan = module.plan_kickoffs(tmp_path)
    assert plan["selected_count"] == 1
    assert plan["catalogue"]["original_status"] == "partial"
    assert plan["catalogue"]["recomputed_status"] == "complete"
    assert plan["catalogue"]["distinct_match_count"] == 1
    (folder / "43539673769-1/jc/2026-10/2026-10-31-p1.json").unlink()
    with pytest.raises((OSError, ValueError)):
        module.plan_kickoffs(tmp_path)


@responses.activate
def test_five_oldest_pilot_then_only_remaining_ids(tmp_path, module, monkeypatch):
    from urllib.parse import parse_qs, urlparse

    ids = tuple(range(2042404, 2042411))
    catalogue(tmp_path, ids)
    sleeps = []
    monkeypatch.setattr(module.time, "sleep", lambda seconds: sleeps.append(seconds))
    for sid in ids:
        raw = json.loads((HEADS / "success.json").read_bytes())
        raw["value"]["sportteryMatchId"] = sid
        responses.add(responses.GET, API + "getMatchHeadV1.qry", json=raw)
    pilot = module.collect_kickoffs(tmp_path / "zucai.db", "pilot")
    assert pilot["requests"] == 5 and pilot["success_count"] == 5
    assert pilot["not_requested_count"] == 2 and pilot["pilot_verified"] is True
    full = module.collect_kickoffs(tmp_path / "zucai.db", "full", max_requests=1200)
    assert full["requests"] == 2 and full["success_count"] == 7
    again = module.collect_kickoffs(tmp_path / "zucai.db", "again")
    assert again["requests"] == 0
    assert [
        int(parse_qs(urlparse(call.request.url).query)["sportteryMatchId"][0])
        for call in responses.calls
    ] == list(ids)
    assert sleeps == [1] * 7


@responses.activate
def test_failed_pilot_stops_remaining_requests(tmp_path, module):
    catalogue(tmp_path, tuple(range(2042404, 2042411)))
    responses.add(
        responses.GET, API + "getMatchHeadV1.qry", body=(HEADS / "empty.json").read_bytes()
    )
    report = module.collect_kickoffs(tmp_path / "zucai.db", "failed")
    assert report["requests"] == 1 and report["failed_count"] == 1
    assert report["not_requested_count"] == 6 and report["pilot_verified"] is False
    assert report["rows"][0]["raw_sha256"]
    assert report["rows"][0]["fetched_at_utc"]


@pytest.mark.parametrize("change", ["sha", "http", "date", "duplicate", "missing"])
def test_invalid_catalogue_never_produces_plan(tmp_path, module, change):
    catalogue(tmp_path)
    path = tmp_path / "state/catalogue/catalogue/jc/2026-10/2026-10-31-p1.json"
    meta_path = path.with_suffix(".json.meta.json")
    meta = json.loads(meta_path.read_bytes())
    if change == "missing":
        path.unlink()
    elif change == "sha":
        meta["sha256"] = "0" * 64
        save_json(meta_path, meta)
    elif change == "http":
        meta["status_code"] = 503
        save_json(meta_path, meta)
    else:
        page = json.loads(path.read_bytes())
        if change == "date":
            page["value"]["matchResult"][0]["matchDate"] = "2026-10-30"
        else:
            page["value"]["matchResult"] *= 2
            page["value"]["total"] = 2
        save_json(path, page)
        meta["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        save_json(meta_path, meta)
    with pytest.raises((OSError, ValueError)):
        module.plan_kickoffs(tmp_path)


@responses.activate
def test_reused_legacy_header_is_persisted_for_clean_restore(tmp_path, module):
    archive = seed_cloud_archive(tmp_path)
    catalogue(tmp_path)
    RawStore(tmp_path / "raw").save(
        "sporttery-head",
        "match-2042404",
        (HEADS / "success.json").read_bytes(),
        "json",
        url=API + "getMatchHeadV1.qry?source=web&sportteryMatchId=2042404",
        status_code=200,
        captured_at=datetime(2026, 11, 1, tzinfo=UTC),
    )
    first = module.collect_kickoffs(tmp_path / "seed.db", "reuse")
    assert first["requests"] == 0 and first["success_count"] == 1
    export_feeds(tmp_path / "seed.db", archive, date(2026, 11, 1), "reuse")
    restored = tmp_path / "fresh/data/zucai.db"
    restore_feeds(archive, restored)
    second = module.collect_kickoffs(restored, "second")
    assert second["requests"] == 0 and second["success_count"] == 1
    assert not responses.calls


@pytest.mark.parametrize(
    "name,value",
    [
        ("GITHUB_ACTIONS", "false"),
        ("GITHUB_EVENT_NAME", "schedule"),
        ("RUNNER_ENVIRONMENT", "github-hosted"),
    ],
)
def test_probe_requires_selfhosted_manual_actions(tmp_path, module, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match="self-hosted"):
        module.collect_kickoffs(tmp_path / "zucai.db", "forbidden")
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize(
    "clock,captured,expected",
    [
        ("2026-10-31 00:00", "2026-10-31T01:00:00+00:00", "2026-10-30T16:00:00+00:00"),
        ("2026-10-31 23:15", "2026-10-31T10:00:00+00:00", "2026-10-31T15:15:00+00:00"),
    ],
)
def test_real_midnight_and_prematch_capture_are_valid_evidence(
    tmp_path, module, clock, captured, expected
):
    catalogue(tmp_path)
    raw = json.loads((HEADS / "success.json").read_bytes())
    raw["value"]["matchDateTime"] = clock
    RawStore(tmp_path / "raw").save(
        "sporttery-head",
        "match-2042404",
        json.dumps(raw).encode(),
        "json",
        url=API + "getMatchHeadV1.qry?source=web&sportteryMatchId=2042404",
        status_code=200,
        captured_at=datetime.fromisoformat(captured),
    )
    plan = module.plan_kickoffs(tmp_path)
    assert plan["pending"] == []
    assert plan["verified"][0]["evidence"][0]["kickoff_utc"] == expected


@responses.activate
def test_conflicting_legacy_originals_survive_export_clean_restore(tmp_path, module):
    archive = seed_cloud_archive(tmp_path)
    catalogue(tmp_path)
    originals = []
    for clock in ("2026-10-31 23:15", "2026-10-31 23:30"):
        raw = json.loads((HEADS / "success.json").read_bytes())
        raw["value"]["matchDateTime"] = clock
        path = RawStore(tmp_path / "raw").save(
            "sporttery-head",
            "match-2042404",
            json.dumps(raw).encode(),
            "json",
            url=API + "getMatchHeadV1.qry?source=web&sportteryMatchId=2042404",
            status_code=200,
            captured_at=datetime(2026, 11, 1, tzinfo=UTC),
        )
        originals.append((path.read_bytes(), path.with_suffix(".json.meta.json").read_bytes()))
    before = module.plan_kickoffs(tmp_path)
    assert before["pending"][0]["reason"] == "conflicting_jc_kickoff_evidence"
    responses.add(
        responses.GET, API + "getMatchHeadV1.qry", body=(HEADS / "success.json").read_bytes()
    )
    report = module.collect_kickoffs(tmp_path / "seed.db", "conflict")
    assert report["failed_count"] == 1 and report["success_count"] == 0
    export_feeds(tmp_path / "seed.db", archive, date(2026, 11, 1), "conflict")
    restored = tmp_path / "clean/data/zucai.db"
    restore_feeds(archive, restored)
    after = module.plan_kickoffs(restored.parent)
    assert after["verified"] == []
    assert after["pending"][0]["reason"] == "conflicting_jc_kickoff_evidence"
    assert {e["kickoff_utc"] for e in after["pending"][0]["evidence"]} == {
        "2026-10-31T15:15:00+00:00",
        "2026-10-31T15:30:00+00:00",
    }
    preserved = list(
        (restored.parent / "state/catalogue/conflict/jc-heads/conflicts").glob("*.json")
    )
    assert len([p for p in preserved if not p.name.endswith(".meta.json")]) == 2
    for raw, sidecar in originals:
        assert any(
            p.read_bytes() == raw and p.with_suffix(".json.meta.json").read_bytes() == sidecar
            for p in preserved
            if not p.name.endswith(".meta.json")
        )
    with pytest.raises(ValueError, match="pilot"):
        module.collect_kickoffs(restored, "blocked-expansion", max_requests=1200)


def test_fixed_default_scope_rejects_missing_source_and_wrong_window(tmp_path, monkeypatch):
    import zucai.jc_catalogue_kickoffs as module

    catalogue(tmp_path)
    with pytest.raises(ValueError, match="fixed"):
        module.plan_kickoffs(tmp_path)
    folder = tmp_path / "state/catalogue"
    (folder / "catalogue").rename(folder / "43539673769-1")
    with pytest.raises(ValueError, match="window"):
        module.plan_kickoffs(tmp_path)


def test_fixed_62_routes_ignore_daily_switch_and_newly_added_route(tmp_path, module, monkeypatch):
    catalogue(tmp_path, (2042404, 2042405), [24, 25])
    registry = json.loads(module.REGISTRY.read_bytes())
    route = registry["providers"]["apif"]["79"]
    route["daily"] = True
    extra = json.loads(json.dumps(route))
    extra["config"]["league"] = "英超"
    extra["daily"] = False
    extra["evidence"] = [dict(e, code="999999") for e in extra["evidence"]]
    registry["providers"]["apif"]["999999"] = extra
    path = tmp_path / "registry.json"
    save_json(path, registry)
    monkeypatch.setattr(module, "REGISTRY", path)
    plan = module.plan_kickoffs(tmp_path)
    assert plan["new_route_count"] == 62
    assert [row["match_id"] for row in plan["selected"]] == [2042404]


def save_schedule(root, *, clock="23:15:00", bad_sha=False):
    raw = json.loads((HEADS / "calculator-synthetic.json").read_bytes())
    raw["value"]["matchInfoList"][0]["subMatchList"][0]["matchTime"] = clock
    path = RawStore(root / "raw").save(
        "sporttery",
        "calculator",
        json.dumps(raw).encode(),
        "json",
        url=API + "getMatchCalculatorV1.qry?poolCode=had,hhad,crs,ttg,hafu&channel=c",
        status_code=200,
        captured_at=datetime(2026, 10, 31, 10, tzinfo=UTC),
    )
    if bad_sha:
        meta = json.loads(path.with_suffix(".json.meta.json").read_bytes())
        meta["sha256"] = "0" * 64
        save_json(path.with_suffix(".json.meta.json"), meta)
    return path


@responses.activate
def test_verified_calculator_is_reused_and_durable(tmp_path, module):
    archive = seed_cloud_archive(tmp_path)
    catalogue(tmp_path)
    save_schedule(tmp_path)
    plan = module.plan_kickoffs(tmp_path)
    assert plan["pending"] == []
    assert plan["verified"][0]["evidence"][0]["basis"] == "jc_schedule_raw_verified"
    report = module.collect_kickoffs(tmp_path / "seed.db", "schedule")
    assert report["requests"] == 0 and report["success_count"] == 1
    export_feeds(tmp_path / "seed.db", archive, date(2026, 11, 1), "schedule")
    restored = tmp_path / "clean/data/zucai.db"
    restore_feeds(archive, restored)
    after = module.plan_kickoffs(restored.parent)
    assert after["pending"] == []
    assert after["verified"][0]["evidence"][0]["basis"] == "jc_schedule_raw_verified"
    assert not responses.calls


@pytest.mark.parametrize("kind", ["sha", "conflict", "sides", "date", "endpoint"])
def test_calculator_identity_sha_and_conflicts_are_checked(tmp_path, module, kind):
    catalogue(tmp_path)
    path = save_schedule(tmp_path, bad_sha=kind == "sha")
    if kind == "conflict":
        save_schedule(tmp_path, clock="23:30:00")
    elif kind in {"sides", "date", "endpoint"}:
        raw = json.loads(path.read_bytes())
        row = raw["value"]["matchInfoList"][0]["subMatchList"][0]
        if kind == "sides":
            row["homeTeamId"], row["awayTeamId"] = row["awayTeamId"], row["homeTeamId"]
        if kind == "date":
            row["matchDate"] = "2026-10-30"
        save_json(path, raw)
        metadata = json.loads(path.with_suffix(".json.meta.json").read_bytes())
        metadata["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        if kind == "endpoint":
            metadata["url"] = metadata["url"].replace("getMatchCalculatorV1", "other")
        save_json(path.with_suffix(".json.meta.json"), metadata)
    plan = module.plan_kickoffs(tmp_path)
    assert plan["verified"] == []
    assert len(plan["pending"]) == 1
    if kind == "conflict":
        assert plan["pending"][0]["reason"] == "conflicting_jc_kickoff_evidence"


def verified_db_archive(tmp_path):
    import sqlite3

    archive = seed_cloud_archive(tmp_path)
    catalogue(tmp_path)
    with sqlite3.connect(tmp_path / "seed.db") as db:
        db.executemany(
            "INSERT INTO teams(id,name,aliases) VALUES(?,?,?)",
            [(1, "加的夫城", "[]"), (2, "切尔西", "[]")],
        )
        db.execute(
            "INSERT INTO matches(match_id,source,league,home_team_id,away_team_id,"
            "kickoff_utc,status,result_provider,result_checked_utc) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "jc:2042404",
                "jc",
                "英联赛杯",
                1,
                2,
                "2026-10-31T15:15:00Z",
                "finished",
                "jc",
                "2026-11-01T01:00:00Z",
            ),
        )
        db.execute(
            "INSERT INTO results(match_id,home_goals,away_goals,available_utc,available_source) "
            "VALUES(?,?,?,?,?)",
            ("jc:2042404", 1, 3, "2026-11-01T01:00:00Z", "captured"),
        )
    export_feeds(tmp_path / "seed.db", archive, date(2026, 11, 1), "db-evidence")
    return archive


@responses.activate
def test_limited_same_jc_db_time_is_verified_and_restorable(tmp_path, module):
    archive = verified_db_archive(tmp_path)
    plan = module.plan_kickoffs(archive)
    assert plan["pending"] == []
    evidence = plan["verified"][0]["evidence"][0]
    assert evidence["basis"] == "db_same_jc_id_verified"
    assert evidence["result_checked_utc"] == "2026-11-01T01:00:00Z"
    assert "fetched_at_utc" not in evidence
    for key in (
        "manifest_sha256",
        "matches_sha256",
        "teams_sha256",
        "catalogue_sha256",
        "aliases_sha256",
    ):
        assert len(evidence[key]) == 64
    database = tmp_path / "work/data/zucai.db"
    restore_feeds(archive, database)
    report = module.collect_kickoffs(database, "db-reuse", archive=archive)
    assert report["requests"] == 0 and report["success_count"] == 1
    export_feeds(database, archive, date(2026, 11, 1), "db-reuse")
    restored = tmp_path / "clean/data/zucai.db"
    restore_feeds(archive, restored)
    after = module.plan_kickoffs(restored.parent)
    assert after["pending"] == []
    assert after["verified"][0]["evidence"][0]["basis"] == "db_same_jc_id_verified"
    assert module.collect_kickoffs(restored, "again")["requests"] == 0
    assert not responses.calls


@pytest.mark.parametrize(
    "kind",
    [
        "bad_sha",
        "non_jc",
        "non_jc_provider",
        "wrong_day",
        "wrong_league",
        "wrong_team",
        "duplicate_owner",
    ],
)
def test_db_evidence_remains_limited_to_verified_same_id(tmp_path, module, kind):
    archive = verified_db_archive(tmp_path)
    path = archive / "state/matches.jsonl"
    row = json.loads(path.read_bytes())
    if kind == "non_jc":
        row[1] = "apif"
    if kind == "non_jc_provider":
        row[9] = "apif"
    if kind == "wrong_day":
        row[5] = "2026-10-30T15:15:00Z"
    if kind == "wrong_league":
        row[2] = "国际赛"
    if kind == "wrong_team":
        row[3], row[4] = row[4], row[3]
    if kind == "duplicate_owner":
        team = archive / "state/teams.jsonl"
        team.write_text(team.read_text() + '[3,"Other","[\\"加的夫城\\"]"]\n')
    path.write_text(json.dumps(row) + "\n")
    if kind != "bad_sha":
        meta = json.loads((archive / "feed_manifest.json").read_bytes())
        for key in ["state/matches.jsonl", "state/teams.jsonl"]:
            meta["files"][key] = hashlib.sha256((archive / key).read_bytes()).hexdigest()
        if kind == "duplicate_owner":
            meta["counts"]["teams"] += 1
        save_json(archive / "feed_manifest.json", meta)
    if kind == "bad_sha":
        with pytest.raises(ValueError, match="SHA"):
            module.plan_kickoffs(archive)
    else:
        assert module.plan_kickoffs(archive)["verified"] == []


def test_independent_raw_time_conflict_overrides_db_time(tmp_path, module):
    archive = verified_db_archive(tmp_path)
    save_schedule(archive, clock="23:30:00")
    plan = module.plan_kickoffs(archive)
    assert plan["verified"] == []
    assert plan["pending"][0]["reason"] == "conflicting_jc_kickoff_evidence"


@pytest.mark.parametrize("kind", ["columns", "counts", "duplicate_match", "duplicate_team"])
def test_db_snapshot_schema_counts_and_duplicate_ids_fail_closed(tmp_path, module, kind):
    archive = verified_db_archive(tmp_path)
    meta_path = archive / "feed_manifest.json"
    meta = json.loads(meta_path.read_bytes())
    if kind == "columns":
        meta["columns"]["matches"][0], meta["columns"]["matches"][1] = (
            meta["columns"]["matches"][1],
            meta["columns"]["matches"][0],
        )
    elif kind == "counts":
        meta["counts"]["matches"] += 1
    else:
        table = "matches" if kind == "duplicate_match" else "teams"
        path = archive / f"state/{table}.jsonl"
        path.write_bytes(path.read_bytes() + path.read_bytes().splitlines(keepends=True)[0])
        meta["files"][f"state/{table}.jsonl"] = hashlib.sha256(path.read_bytes()).hexdigest()
        meta["counts"][table] += 1
    save_json(meta_path, meta)
    with pytest.raises(ValueError, match="columns|count|duplicate"):
        module.plan_kickoffs(archive)


@responses.activate
def test_db_and_calculator_conflict_survives_clean_restore(tmp_path, module):
    archive = verified_db_archive(tmp_path)
    save_schedule(archive, clock="23:30:00")
    database = tmp_path / "work/data/zucai.db"
    restore_feeds(archive, database)
    responses.add(
        responses.GET, API + "getMatchHeadV1.qry", body=(HEADS / "success.json").read_bytes()
    )
    report = module.collect_kickoffs(database, "mixed-conflict", archive=archive)
    assert report["success_count"] == 0 and report["failed_count"] == 1
    export_feeds(database, archive, date(2026, 11, 1), "mixed-conflict")
    fresh = tmp_path / "fresh/data/zucai.db"
    restore_feeds(archive, fresh)
    plan = module.plan_kickoffs(fresh.parent)
    assert plan["verified"] == []
    assert plan["pending"][0]["reason"] == "conflicting_jc_kickoff_evidence"
    bases = {e["basis"] for e in plan["pending"][0]["evidence"]}
    assert bases == {"db_same_jc_id_verified", "jc_schedule_raw_verified", "jc_head_raw_verified"}


@responses.activate
def test_db_core_is_deduplicated_across_manifest_changes_and_reverified(tmp_path, module):
    archive = verified_db_archive(tmp_path)
    for batch in ("one", "two"):
        database = tmp_path / batch / "data/zucai.db"
        restore_feeds(archive, database)
        assert module.collect_kickoffs(database, batch, archive=archive)["requests"] == 0
        export_feeds(database, archive, date(2026, 11, 1), batch)
    cores = list((archive / "state/catalogue").glob("*/jc-db-matches.jsonl"))
    assert len(cores) == 1
    assert len(list((archive / "state/catalogue").glob("*/jc-db-manifests/*.json"))) == 2
    cores[0].write_bytes(cores[0].read_bytes() + b" ")
    with pytest.raises(ValueError, match="SHA"):
        module.plan_kickoffs(archive)


@pytest.mark.parametrize("fixture", ["wrong-id", "reversed", "wrong-day"])
@responses.activate
def test_trusted_head_contradiction_blocks_db_and_survives_restore(tmp_path, module, fixture):
    archive = verified_db_archive(tmp_path)
    original = RawStore(archive / "raw").save(
        "sporttery-head",
        "match-2042404",
        (HEADS / f"{fixture}.json").read_bytes(),
        "json",
        url=API + "getMatchHeadV1.qry?source=web&sportteryMatchId=2042404",
        status_code=200,
        captured_at=datetime(2026, 11, 1, tzinfo=UTC),
    )
    original_raw = original.read_bytes()
    original_meta = original.with_suffix(".json.meta.json").read_bytes()
    before = module.plan_kickoffs(archive)
    assert before["verified"] == []
    assert before["pending"][0]["reason"] == "conflicting_jc_kickoff_evidence"
    database = tmp_path / "work/data/zucai.db"
    restore_feeds(archive, database)
    responses.add(
        responses.GET, API + "getMatchHeadV1.qry", body=(HEADS / "success.json").read_bytes()
    )
    report = module.collect_kickoffs(database, "contradiction", archive=archive)
    assert report["requests"] == 1 and report["success_count"] == 0
    assert report["failed_count"] == 1
    export_feeds(database, archive, date(2026, 11, 1), "contradiction")
    restored = tmp_path / "clean/data/zucai.db"
    restore_feeds(archive, restored)
    after = module.plan_kickoffs(restored.parent)
    assert after["verified"] == []
    assert after["pending"][0]["reason"] == "conflicting_jc_kickoff_evidence"
    proofs = [
        e for e in after["pending"][0]["evidence"] if e["basis"] == "jc_head_raw_contradiction"
    ]
    assert len(proofs) == 1
    saved = restored.parent / proofs[0]["path"]
    assert saved.read_bytes() == original_raw
    assert saved.with_suffix(".json.meta.json").read_bytes() == original_meta
    with pytest.raises(ValueError, match="pilot"):
        module.collect_kickoffs(restored, "blocked", max_requests=1200)


@pytest.mark.parametrize("kind", ["empty", "http", "sha"])
def test_untrusted_or_empty_head_does_not_permanently_block_db(tmp_path, module, kind):
    archive = verified_db_archive(tmp_path)
    raw = (HEADS / ("empty.json" if kind == "empty" else "wrong-id.json")).read_bytes()
    path = RawStore(archive / "raw").save(
        "sporttery-head",
        "match-2042404",
        raw,
        "json",
        url=API + "getMatchHeadV1.qry?source=web&sportteryMatchId=2042404",
        status_code=503 if kind == "http" else 200,
        captured_at=datetime(2026, 11, 1, tzinfo=UTC),
    )
    if kind == "sha":
        meta = json.loads(path.with_suffix(".json.meta.json").read_bytes())
        meta["sha256"] = "0" * 64
        save_json(path.with_suffix(".json.meta.json"), meta)
    plan = module.plan_kickoffs(archive)
    assert plan["pending"] == [] and len(plan["verified"]) == 1
    assert len(plan["issues"]) == 1


def test_head_sidecar_is_not_parsed_as_another_original(tmp_path, module):
    archive = verified_db_archive(tmp_path)
    RawStore(archive / "raw").save(
        "sporttery-head",
        "match-2042404",
        (HEADS / "success.json").read_bytes(),
        "json",
        url=API + "getMatchHeadV1.qry?source=web&sportteryMatchId=2042404",
        status_code=200,
        captured_at=datetime(2026, 11, 1, tzinfo=UTC),
    )
    plan = module.plan_kickoffs(archive)
    assert len(plan["verified"]) == 1
    assert plan["issues"] == []


@responses.activate
def test_preserved_original_paths_fit_existing_windows_runner(tmp_path, module):
    archive = verified_db_archive(tmp_path)
    RawStore(archive / "raw").save(
        "sporttery-head",
        "match-2042404",
        (HEADS / "success.json").read_bytes(),
        "json",
        url=API + "getMatchHeadV1.qry?source=web&sportteryMatchId=2042404",
        status_code=200,
        captured_at=datetime(2026, 11, 1, tzinfo=UTC),
    )
    save_schedule(archive)
    database = tmp_path / "work/data/zucai.db"
    restore_feeds(archive, database)
    run_id = "43583697515-1"
    report = module.collect_kickoffs(database, run_id, archive=archive)
    base = "D:/actions-runner/_work/FOOTBALL/FOOTBALL/work-jc-catalogue-43583697515-1/data/"
    evidence = [e for row in report["rows"] for e in row["evidence"] if "path" in e]
    assert {e["basis"] for e in evidence} == {"jc_head_raw_verified", "jc_schedule_raw_verified"}
    for proof in evidence:
        assert len(base + proof["path"] + ".meta.json") < 260
        pair = hashlib.sha256((proof["sha256"] + proof["metadata_sha256"]).encode()).hexdigest()
        expected = (
            f"2042404-{pair}.json" if proof["basis"] == "jc_head_raw_verified" else f"{pair}.json"
        )
        assert Path(proof["path"]).name == expected
    assert not responses.calls


@pytest.mark.parametrize("support", ["db", "head"])
@pytest.mark.parametrize("kind", ["sides", "date", "duplicate"])
@responses.activate
def test_trusted_schedule_contradiction_blocks_other_proofs_and_restores(
    tmp_path, module, support, kind
):
    if support == "db":
        archive = verified_db_archive(tmp_path)
        origin = archive
        database = tmp_path / "work/data/zucai.db"
        restore_feeds(archive, database)
    else:
        archive = seed_cloud_archive(tmp_path)
        catalogue(tmp_path)
        origin = tmp_path
        database = tmp_path / "seed.db"
        RawStore(origin / "raw").save(
            "sporttery-head",
            "match-2042404",
            (HEADS / "success.json").read_bytes(),
            "json",
            url=API + "getMatchHeadV1.qry?source=web&sportteryMatchId=2042404",
            status_code=200,
            captured_at=datetime(2026, 11, 1, tzinfo=UTC),
        )
    path = save_schedule(origin)
    raw = json.loads(path.read_bytes())
    rows = raw["value"]["matchInfoList"][0]["subMatchList"]
    if kind == "sides":
        rows[0]["homeTeamId"], rows[0]["awayTeamId"] = rows[0]["awayTeamId"], rows[0]["homeTeamId"]
    elif kind == "date":
        rows[0]["matchDate"] = "2026-10-30"
    else:
        rows.append(dict(rows[0], matchTime="23:30:00"))
    save_json(path, raw)
    meta = json.loads(path.with_suffix(".json.meta.json").read_bytes())
    meta["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    save_json(path.with_suffix(".json.meta.json"), meta)
    original = path.read_bytes()
    original_meta = path.with_suffix(".json.meta.json").read_bytes()
    plan = module.plan_kickoffs(origin)
    assert plan["verified"] == []
    assert plan["pending"][0]["reason"] == "conflicting_jc_kickoff_evidence"
    responses.add(
        responses.GET, API + "getMatchHeadV1.qry", body=(HEADS / "success.json").read_bytes()
    )
    report = module.collect_kickoffs(database, "schedule-conflict", archive=origin)
    assert report["success_count"] == 0 and report["failed_count"] == 1
    export_feeds(database, archive, date(2026, 11, 1), "schedule-conflict")
    fresh = tmp_path / "clean/data/zucai.db"
    restore_feeds(archive, fresh)
    after = module.plan_kickoffs(fresh.parent)
    assert after["verified"] == []
    assert after["pending"][0]["reason"] == "conflicting_jc_kickoff_evidence"
    proof = [
        e for e in after["pending"][0]["evidence"] if e["basis"] == "jc_schedule_raw_contradiction"
    ]
    assert len(proof) == 1
    saved = fresh.parent / proof[0]["path"]
    assert saved.parent.name == "jc-schedules"
    assert saved.read_bytes() == original
    assert saved.with_suffix(".json.meta.json").read_bytes() == original_meta
    with pytest.raises(ValueError, match="pilot"):
        module.collect_kickoffs(fresh, "blocked", max_requests=1200)


@pytest.mark.parametrize("kind", ["http", "sha", "empty"])
def test_untrusted_or_empty_schedule_does_not_create_permanent_hold(tmp_path, module, kind):
    archive = verified_db_archive(tmp_path)
    path = save_schedule(archive, bad_sha=kind == "sha")
    meta = json.loads(path.with_suffix(".json.meta.json").read_bytes())
    if kind == "http":
        meta["status_code"] = 503
    elif kind == "empty":
        raw = json.loads(path.read_bytes())
        raw["value"]["matchInfoList"] = []
        save_json(path, raw)
        meta["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    save_json(path.with_suffix(".json.meta.json"), meta)
    plan = module.plan_kickoffs(archive)
    assert len(plan["verified"]) == 1 and plan["pending"] == []
