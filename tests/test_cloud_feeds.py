import csv
import json
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import responses

FIXTURES = Path(__file__).parents[1] / "data/fixtures"
API = "https://webapi.sporttery.cn/gateway/uniform/football/"


def feed_json(name):
    return json.loads((FIXTURES / name).read_bytes())


def seed_cloud_archive(tmp_path):
    from zucai.cloud_feeds import export_feeds
    from zucai.db import get_engine, init_db

    database = tmp_path / "seed.db"
    engine = get_engine(database)
    init_db(engine)
    engine.dispose()
    archive = tmp_path / "archive"
    export_feeds(database, archive, date(2026, 11, 1), "seed")
    return archive


def run_cloud_sync(monkeypatch, work, archive, through):
    from zucai.cloud_feeds import main

    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("ZUCAI_DB", str(work / "data/zucai.db"))
    monkeypatch.setattr(
        sys,
        "argv",
        ["feeds", "sync", "--archive", str(archive), "--through", through, "--run-id", "trial"],
    )
    monkeypatch.setattr("zucai.cloud_feeds.time.sleep", lambda _: None)
    main()


def mock_calendar(api):
    calendar = feed_json("sporttery_calculator_20261101.json")
    # Keep sample fixtures unstarted regardless of the machine's current clock.
    for group in calendar["value"]["matchInfoList"]:
        for game in group["subMatchList"]:
            game["matchDate"] = "2099-11-01"
    api.add(responses.GET, API + "getMatchCalculatorV1.qry", json=calendar)


def mock_result_day(api, day, games):
    body = feed_json("sporttery_results_20261031.json")
    original = body["value"]["matchResult"][0]
    rows = [dict(original, matchDate=day, **changes) for changes in games]
    body["value"].update(matchResult=rows, total=len(rows))
    api.add(
        responses.GET,
        API + "getUniformMatchResultV1.qry",
        json=body,
        match=[responses.matchers.query_param_matcher({"matchBeginDate": day}, strict_match=False)],
    )
    for row in rows:
        if row["sectionsNo1"] == "9:0":
            continue  # This invalid row is rejected before requesting a header.
        head = feed_json("sporttery_head_2042404.json")
        head["value"].update(sportteryMatchId=row["matchId"], matchDateTime=day + " 10:30")
        api.add(
            responses.GET,
            API + "getMatchHeadV1.qry",
            json=head,
            match=[
                responses.matchers.query_param_matcher(
                    {"sportteryMatchId": str(row["matchId"])}, strict_match=False
                )
            ],
        )


def test_cloud_sync_publishes_good_results_and_quotes_despite_one_rejected_row(
    tmp_path, monkeypatch, caplog
):
    from zucai.cloud_feeds import archive_info, restore_feeds

    archive = seed_cloud_archive(tmp_path)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    with responses.RequestsMock() as api:
        mock_calendar(api)
        mock_result_day(api, "2026-10-30", [{"matchId": 4}])
        mock_result_day(api, "2026-10-31", [{"matchId": 5, "sectionsNo1": "9:0"}])
        mock_result_day(api, "2026-11-01", [{"matchId": 7}])
        run_cloud_sync(monkeypatch, tmp_path / "run", archive, "2026-11-01")
    meta = archive_info(archive)
    assert meta["counts"]["results"] == 2
    assert meta["counts"]["odds"] > 0
    assert meta["collection_status"] == "partial"
    assert meta["pending_result_count"] == 1
    assert meta["pending_result_dates"] == ["2026-10-31"]
    assert "待补抓" in caplog.text and "partial" in summary.read_text(encoding="utf8")
    assert "state/rejected_results.csv" in meta["files"]
    restore_feeds(archive, tmp_path / "restored/data/zucai.db")
    with closing(sqlite3.connect(tmp_path / "restored/data/zucai.db")) as db:
        assert db.execute("select match_id from results order by match_id").fetchall() == [
            ("jc:4",),
            ("jc:7",),
        ]
    assert (tmp_path / "restored/data/rejected_results.csv").is_file()


def test_old_rejection_is_retried_until_accepted_even_if_missing_from_next_page(
    tmp_path, monkeypatch
):
    from zucai.cloud_feeds import archive_info, export_feeds

    # The rejected fixture has no matches row, so the scheduled-only retry cannot find it.
    archive = seed_cloud_archive(tmp_path)
    audit = tmp_path / "rejected_results.csv"
    audit.write_text(
        "date,match_id,observed_utc,reason\n2026-10-01,jc:5,2026-11-01T00:00:00+00:00,bad\n",
        encoding="utf8",
    )
    export_feeds(tmp_path / "seed.db", archive, date(2026, 11, 8), "rejected")
    for attempt, rows in enumerate(([], [{"matchId": 5}])):
        with responses.RequestsMock() as api:
            mock_calendar(api)
            mock_result_day(api, "2026-10-01", rows)
            for day in ("2026-11-07", "2026-11-08", "2026-11-09"):
                mock_result_day(api, day, [])
            run_cloud_sync(monkeypatch, tmp_path / f"retry-{attempt}", archive, "2026-11-09")
        meta = archive_info(archive)
        assert meta["pending_result_count"] == (1 if attempt == 0 else 0)
        assert meta["collection_status"] == ("partial" if attempt == 0 else "complete")
    assert meta["counts"]["results"] == 1
    with (archive / "state/rejected_results.csv").open(encoding="utf8") as f:
        assert [r["match_id"] for r in csv.DictReader(f)] == ["jc:5"]


@pytest.mark.parametrize("failure", ["pagination", "blocked"])
def test_cloud_sync_does_not_publish_after_incomplete_page_or_policy_denial(
    tmp_path, monkeypatch, failure
):
    from requests import HTTPError

    from zucai.cloud_feeds import export_feeds

    archive = seed_cloud_archive(tmp_path)
    # First window has a recoverable rejection; a later fatal failure must still block publication.
    (tmp_path / "rejected_results.csv").write_text(
        "date,match_id,observed_utc,reason\n2026-10-01,jc:5,2026-11-01T00:00:00+00:00,bad\n",
        encoding="utf8",
    )
    export_feeds(tmp_path / "seed.db", archive, date(2026, 11, 1), "rejected")
    before = {p.relative_to(archive): p.read_bytes() for p in archive.rglob("*") if p.is_file()}
    with responses.RequestsMock() as api:
        mock_calendar(api)
        mock_result_day(api, "2026-10-01", [{"matchId": 4}, {"matchId": 5, "sectionsNo1": "9:0"}])
        if failure == "blocked":
            api.add(
                responses.GET,
                API + "getUniformMatchResultV1.qry",
                status=567,
                match=[
                    responses.matchers.query_param_matcher(
                        {"matchBeginDate": "2026-10-30"}, strict_match=False
                    )
                ],
            )
        else:
            body = feed_json("sporttery_results_20261031.json")
            body["value"].update(matchResult=[], total=2)
            api.add(
                responses.GET,
                API + "getUniformMatchResultV1.qry",
                json=body,
                match=[
                    responses.matchers.query_param_matcher(
                        {"matchBeginDate": "2026-10-30"}, strict_match=False
                    )
                ],
            )
        with pytest.raises((ValueError, HTTPError)):
            run_cloud_sync(monkeypatch, tmp_path / "run", archive, "2026-11-01")
    assert {
        p.relative_to(archive): p.read_bytes() for p in archive.rglob("*") if p.is_file()
    } == before


def test_conflict_at_same_observation_stays_pending_until_a_later_verified_result(tmp_path):
    from dataclasses import replace

    from zucai.cloud_feeds import archive_info, date_windows, export_feeds
    from zucai.db import get_engine, session_scope
    from zucai.sources.results import SportteryResults, apply_result

    archive = seed_cloud_archive(tmp_path)
    engine = get_engine(tmp_path / "seed.db")
    observed = datetime(2026, 11, 1, 0, tzinfo=UTC)
    game = replace(
        SportteryResults().parse((FIXTURES / "sporttery_results_20261031.json").read_bytes())[0],
        kickoff_utc=observed - timedelta(hours=2),
    )
    with session_scope(engine) as session:
        apply_result(session, game, observed)
    # Append order is not observation order: an old replay must not replace the latest rejection.
    (tmp_path / "rejected_results.csv").write_text(
        "date,match_id,observed_utc,reason\n"
        "2026-10-31,jc:2042404,2026-11-01T00:00:00+00:00,conflicting score\n"
        "2026-10-31,jc:2042404,2026-10-31T23:00:00+00:00,older replay\n",
        encoding="utf8",
    )
    export_feeds(tmp_path / "seed.db", archive, date(2026, 11, 1), "conflict")
    assert archive_info(archive)["pending_result_count"] == 1
    assert date_windows(tmp_path / "seed.db", date(2026, 11, 8), date(2026, 11, 9), 3)[0] == (
        date(2026, 10, 31),
        date(2026, 10, 31),
    )
    with session_scope(engine) as session:
        apply_result(session, game, observed + timedelta(seconds=1))
    engine.dispose()
    export_feeds(tmp_path / "seed.db", archive, date(2026, 11, 1), "verified")
    assert archive_info(archive)["pending_result_count"] == 0


def test_private_archive_roundtrip_and_corruption_does_not_replace_existing_db(
    tmp_path, monkeypatch
):
    from zucai.cloud_feeds import export_feeds, restore_feeds
    from zucai.db import get_engine, init_db, session_scope
    from zucai.sources.sporttery import SportteryCalculator, save_schedule

    monkeypatch.chdir(tmp_path)
    source = tmp_path / "data/zucai.db"
    archive = tmp_path / "archive"
    e = get_engine(source)
    init_db(e)
    f = Path(__file__).parents[1] / "data/fixtures/sporttery_calculator_20261101.json"
    snap = datetime(2026, 11, 1, 12, 42, tzinfo=UTC)
    with session_scope(e) as s:
        save_schedule(s, SportteryCalculator().parse(f.read_bytes()), snap)
    e.dispose()
    export_feeds(source, archive, date(2026, 10, 31), "verification")
    import csv

    with (archive / "matches/2026-11.csv").open(encoding="utf-8-sig") as view:
        first = next(row for row in csv.DictReader(view) if row["match_id"] == "jc:2042420")
    assert (
        first["had_home"] == "3.46" and first["had_snapshot_utc"] == "2026-11-01T12:42:00.000000Z"
    )
    restore = tmp_path / "new/data/zucai.db"
    restore_feeds(archive, restore)

    def counts(p):
        with sqlite3.connect(p) as c:
            return [
                c.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                for t in ("teams", "matches", "odds", "results")
            ]

    assert counts(restore) == counts(source)
    damaged = next((archive / "state/odds").glob("*.jsonl"))
    damaged.write_text("[]\n", encoding="utf8")
    with pytest.raises(ValueError, match="校验"):
        restore_feeds(archive, restore)
    assert counts(restore) == counts(source)


def test_collection_window_recovers_missing_days_and_includes_unsettled_old_fixtures(tmp_path):
    from zucai.cloud_feeds import date_windows
    from zucai.db import get_engine, init_db, session_scope
    from zucai.sources.sporttery import JcMatch, save_schedule

    path = tmp_path / "data/zucai.db"
    e = get_engine(path)
    init_db(e)
    kickoff = datetime(2026, 9, 1, 12, tzinfo=UTC)
    with session_scope(e) as s:
        g = JcMatch("jc:12", kickoff.date(), "周二001", "联赛", "主队", "客队", kickoff, [])
        save_schedule(s, [g], kickoff - timedelta(hours=1))
    assert date_windows(path, date(2026, 10, 28), date(2026, 11, 1), 3) == [
        (date(2026, 9, 1), date(2026, 9, 1)),
        (date(2026, 10, 27), date(2026, 11, 1)),
    ]
    e.dispose()


def test_sync_refuses_to_replace_a_preexisting_working_database(tmp_path, monkeypatch):
    import sys

    from zucai.cloud_feeds import main

    monkeypatch.chdir(tmp_path)
    p = Path("data/zucai.db")
    p.parent.mkdir()
    p.write_bytes(b"existing")
    monkeypatch.setattr(sys, "argv", ["feeds", "sync", "--archive", "archive", "--run-id", "trial"])
    with pytest.raises(ValueError, match="已有数据库"):
        main()
    assert p.read_bytes() == b"existing"


def test_cloud_verify_only_restores_the_archive_and_does_not_call_the_source(tmp_path, monkeypatch):
    import sys

    from zucai.cloud_feeds import export_feeds, main
    from zucai.db import get_engine, init_db

    monkeypatch.chdir(tmp_path)
    base = tmp_path / "base/feeds.db"
    e = get_engine(base)
    init_db(e)
    e.dispose()
    arc = tmp_path / "archive"
    export_feeds(base, arc, date.today(), "sample")
    before = (arc / "feed_manifest.json").read_bytes()
    monkeypatch.setattr(sys, "argv", ["feeds", "verify", "--archive", str(arc), "--run-id", "test"])
    main()
    assert (tmp_path / "data/zucai.db").is_file() and (
        arc / "feed_manifest.json"
    ).read_bytes() == before


def test_site_policy_denial_is_not_retried_as_a_transient_server_error():
    from zucai.sources.base import make_session

    with make_session() as client:
        retry = client.get_adapter("https://webapi.sporttery.cn/").max_retries
        assert not retry.is_retry("GET", 567) and retry.is_retry("GET", 503)


def test_export_removes_only_its_stale_month_and_reads_older_match_schema(tmp_path, monkeypatch):
    import json

    from zucai.cloud_feeds import digest, export_feeds, restore_feeds
    from zucai.db import get_engine, init_db, session_scope
    from zucai.sources.sporttery import JcMatch, save_schedule

    monkeypatch.chdir(tmp_path)
    db = tmp_path / "data/zucai.db"
    archive = tmp_path / "archive"
    e = get_engine(db)
    init_db(e)
    clock = datetime(2026, 10, 31, 12, tzinfo=UTC)
    g = JcMatch("jc:12", clock.date(), "周六001", "联赛", "主", "客", clock, [])
    with session_scope(e) as s:
        save_schedule(s, [g], clock - timedelta(days=1))
    e.dispose()
    export_feeds(db, archive, date(2026, 10, 31), "first")
    keep = archive / "matches/field-notes.txt"
    keep.write_text("user")
    with sqlite3.connect(db) as s:
        s.execute("UPDATE matches SET kickoff_utc='2026-11-01T14:00:00.000000Z'")
    export_feeds(db, archive, date(2026, 11, 1), "second")
    assert not (archive / "matches/2026-10.csv").exists() and keep.read_text() == "user"
    # The first published seed predates the optional cross-source alias.
    m = archive / "feed_manifest.json"
    meta = json.loads(m.read_text(encoding="utf8"))
    key = "state/matches.jsonl"
    payload = archive / key
    index = meta["columns"]["matches"].index("counterpart_id")
    meta["columns"]["matches"].pop(index)
    row = json.loads(payload.read_text(encoding="utf8"))
    row.pop(index)
    payload.write_text(json.dumps(row) + "\n", encoding="utf8")
    meta["files"][key] = digest(payload)
    m.write_text(json.dumps(meta), encoding="utf8")
    target = tmp_path / "new/zucai.db"
    restore_feeds(archive, target)
    with sqlite3.connect(target) as s:
        assert s.execute("select counterpart_id from matches").fetchone() == (None,)
