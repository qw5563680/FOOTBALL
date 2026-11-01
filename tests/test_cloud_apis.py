"""Real import/export pipeline over synthetic HTTP fixtures; never live credentials."""

import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import responses

from zucai.cloud_feeds import archive_info, export_feeds, main, restore_feeds
from zucai.db import get_engine, init_db

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "data/fixtures/t14"
CLOCK = datetime(2026, 11, 2, 2, 40, tzinfo=UTC)


class FrozenTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return CLOCK.astimezone(tz) if tz else CLOCK.replace(tzinfo=None)


@pytest.fixture
def environment(tmp_path, monkeypatch):
    # Explicit fake credentials, with every HTTP call intercepted by responses.
    for key in ("FOOTBALL_DATA_ORG_TOKEN", "API_FOOTBALL_KEY"):
        monkeypatch.setenv(key, "offline-placeholder")
    for module in (
        "zucai.cloud_feeds",
        "zucai.sources.base",
        "zucai.sources.fdorg_command",
        "zucai.sources.football_data_org",
        "zucai.sources.api_football",
        "zucai.sources.apif_quota",
    ):
        monkeypatch.setattr(module + ".datetime", FrozenTime)
    monkeypatch.setattr("zucai.sources.base.RateLimiter.wait", lambda self: None)
    db = tmp_path / "seed/zucai.db"
    engine = get_engine(db)
    init_db(engine)
    engine.dispose()
    with sqlite3.connect(db) as con:
        con.execute("INSERT INTO teams VALUES (1,'Home','[]'),(2,'Away','[]')")
        con.execute(
            "INSERT INTO matches (match_id,source,league,home_team_id,away_team_id,"
            "kickoff_utc,status) VALUES "
            "('jc:1','jc','Seed',1,2,'2026-10-01T12:00:00.000000Z','scheduled')"
        )
    archive = tmp_path / "archive"
    export_feeds(db, archive, CLOCK.date(), "seed")
    return archive


def configure_fd(monkeypatch, tmp_path):
    from zucai import cloud_apis

    config = json.loads((ROOT / "data/football_data_co_uk/leagues.json").read_bytes())
    path = tmp_path / "leagues.json"
    path.write_text(json.dumps({k: config[k] for k in ("E0", "D2")}))
    monkeypatch.setattr(cloud_apis, "LEAGUES_PATH", path)


def run_apis(tmp_path, monkeypatch, archive, name, slot="early"):
    work = tmp_path / name
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("ZUCAI_DB", str(work / "data/zucai.db"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(work / "summary.md"))
    monkeypatch.setattr(
        sys, "argv", ["feeds", "apis", "--archive", str(archive), "--run-id", name, "--slot", slot]
    )
    main()
    return work


def mock_apis(api, *, fd=False, fail_fdorg=False, rejected=False, remaining=None):
    if fd:
        api.add(
            responses.GET,
            "https://www.football-data.co.uk/mmz4281/2627/E0.csv",
            body=(FIXTURES / "fd.csv").read_bytes(),
        )
    doc = json.loads((FIXTURES / "fdorg.json").read_bytes())
    if rejected:
        doc["matches"][0]["score"]["fullTime"]["home"] = -1
    api.add(
        responses.GET,
        "https://api.football-data.org/v4/matches",
        status=403 if fail_fdorg else 200,
        json={"message": "denied"} if fail_fdorg else doc,
        match=[
            responses.matchers.query_param_matcher(
                {
                    "competitions": "PL,ELC,PD,SA,BL1,FL1,DED,PPL,BSA",
                    "dateFrom": "2026-10-30",
                    "dateTo": "2026-11-06",
                }
            )
        ],
    )
    for offset in range(3):
        day = (CLOCK - timedelta(days=offset)).date().isoformat()
        doc = json.loads((FIXTURES / "apif.json").read_bytes())
        doc["parameters"]["date"] = day
        row = doc["response"][0]
        row["fixture"]["id"] += offset
        kickoff = datetime.fromisoformat(day + "T00:00:00+00:00")
        row["fixture"]["timestamp"] = int(kickoff.timestamp())
        row["fixture"]["date"] = kickoff.isoformat()
        api.add(
            responses.GET,
            "https://v3.football.api-sports.io/fixtures",
            json=doc,
            headers={}
            if remaining is None
            else {"x-ratelimit-requests-remaining": str(remaining - offset)},
            match=[responses.matchers.query_param_matcher({"date": day})],
        )


def test_full_chain_restores_three_sources_closing_prices_and_reuses_quota(
    environment, tmp_path, monkeypatch
):
    configure_fd(monkeypatch, tmp_path)
    aliases_before = {p: p.read_bytes() for p in (ROOT / "data/team_aliases").glob("*.json")}
    with responses.RequestsMock() as api:
        mock_apis(api, fd=True)
        first = run_apis(tmp_path, monkeypatch, environment, "first")
    meta = archive_info(environment)
    assert meta["counts"]["matches"] == 6
    assert meta["counts"]["closing_odds"] == 1
    assert meta["collection_status"] == "complete"
    assert meta["collection"]["apif"]["requests"] == 3
    assert meta["collection"]["fdorg"]["requests"] == 1
    assert meta["collection"]["fd"]["inserted"] == 1
    assert meta["collection"]["fd"]["downloaded_files"] == 1
    assert "requests" not in meta["collection"]["fd"]
    quota = environment / "state/api_football_quota.json"
    assert json.loads(quota.read_bytes())["used"] == 3
    assert json.loads(quota.read_bytes())["remaining"] == 97
    assert (environment / "state/audit/fd_unmatched_teams.csv").is_file()
    assert (environment / "state/audit/fdorg_unmatched_teams.csv").is_file()
    assert (environment / "state/audit/apif_unmatched_teams.csv").is_file()
    assert (environment / "state/alias_candidates/fdorg.json").is_file()
    for label in ("fdorg", "apif", "97", "入库", "链接", "拒绝"):
        assert label in (first / "summary.md").read_text()
    with responses.RequestsMock() as api:
        mock_apis(api)
        second = run_apis(tmp_path, monkeypatch, environment, "second", slot="late")
    assert json.loads(quota.read_bytes())["used"] == 6
    assert json.loads(quota.read_bytes())["remaining"] == 94
    assert archive_info(environment)["collection"]["fd"]["status"] == "skipped"
    restored = tmp_path / "restored/data/zucai.db"
    restore_feeds(environment, restored)
    with sqlite3.connect(restored) as a, sqlite3.connect(second / "data/zucai.db") as b:
        for table in ("teams", "matches", "results", "odds", "closing_odds"):
            assert (
                a.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
                == b.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
            )
    assert {p: p.read_bytes() for p in aliases_before} == aliases_before


@pytest.mark.parametrize("failure", ["http", "rows"])
def test_source_failure_or_rejected_rows_marks_partial_but_keeps_other_sources(
    environment, tmp_path, monkeypatch, failure
):
    with responses.RequestsMock() as api:
        mock_apis(api, fail_fdorg=failure == "http", rejected=failure == "rows")
        run_apis(tmp_path, monkeypatch, environment, "partial", slot="late")
    meta = archive_info(environment)
    assert meta["collection_status"] == "partial"
    assert meta["collection"]["fdorg"]["status"] == "partial"
    assert meta["sources"]["apif"]["counts"]["results"] == 3
    assert json.loads((environment / "state/api_football_quota.json").read_bytes())["used"] == 3
    if failure == "rows":
        assert "state/audit/fdorg_rejected_rows.csv" in meta["files"]
    else:
        assert "403" in str(meta["collection"]["fdorg"]["errors"])


@pytest.mark.parametrize(
    ("instant", "slot", "expected"),
    [
        ("2026-11-02T02:40:00+00:00", "early", True),
        ("2026-11-02T09:40:00+00:00", "late", False),
        ("2026-11-01T02:40:00+00:00", "early", False),
        ("2026-11-03T02:40:00+00:00", "early", False),
        ("2026-11-02T02:40:00+08:00", "early", False),
    ],
)
def test_fd_only_monday_early_utc(instant, slot, expected):
    from zucai.cloud_apis import fd_due

    assert fd_due(datetime.fromisoformat(instant), slot) is expected


def test_candidates_keep_evidence_across_runs_without_activating_aliases(
    environment, tmp_path, monkeypatch
):
    from zucai.cloud_feeds import digest

    payload = environment / "state/matches.jsonl"
    row = json.loads(payload.read_text())
    row[2] = "英超"
    row[5] = "2026-11-01T18:00:00.000000Z"
    payload.write_text(json.dumps(row) + "\n")
    meta = json.loads((environment / "feed_manifest.json").read_bytes())
    # A genuine pre-T14 archive has no per-source inventory.
    meta["version"] = 1
    meta.pop("sources")
    meta["files"]["state/matches.jsonl"] = digest(payload)
    (environment / "feed_manifest.json").write_text(json.dumps(meta))
    for number in range(2):
        with responses.RequestsMock() as api:
            mock_apis(api)
            run_apis(tmp_path, monkeypatch, environment, f"candidates-{number}", slot="late")
        candidates = json.loads((environment / "state/alias_candidates/fdorg.json").read_bytes())
        assert candidates["英超"]["90001"]["canonical"] == "Home"
        assert candidates["英超"]["90001"]["status"] == "auto_verified"
        evidence = candidates["英超"]["90001"]["evidence"]
        assert evidence[0]["jc_id"] == "jc:1"
        assert evidence[0]["fdorg_id"] == "fdorg:990001"
        assert len(evidence[0]["raw_sha256"]) == 64
        # Candidate never becomes active for the next import: JC is still not linked.
        with sqlite3.connect(tmp_path / f"candidates-{number}/data/zucai.db") as db:
            assert db.execute(
                "SELECT counterpart_id FROM matches WHERE match_id='jc:1'"
            ).fetchone() == (None,)
    assert len(evidence) == 2


def test_quota_reserve_survives_next_run_without_extra_requests(environment, tmp_path, monkeypatch):
    from zucai.cloud_feeds import digest

    quota = environment / "state/api_football_quota.json"
    quota.write_text(json.dumps(dict(date_utc="2026-11-02", used=89, remaining=11)))
    meta = json.loads((environment / "feed_manifest.json").read_bytes())
    meta["files"]["state/api_football_quota.json"] = digest(quota)
    (environment / "feed_manifest.json").write_text(json.dumps(meta))
    for index in range(2):
        with responses.RequestsMock(assert_all_requests_are_fired=False) as api:
            mock_apis(api, remaining=10)
            run_apis(tmp_path, monkeypatch, environment, f"quota-{index}", slot="late")
        meta = archive_info(environment)
        assert meta["collection_status"] == "partial"
        assert meta["collection"]["apif"]["requests"] == (1 if index == 0 else 0)
        assert meta["collection"]["apif"]["remaining_quota"] == 10
        assert json.loads(quota.read_bytes())["used"] == 90


def test_fd_current_seasons_skip_unverified_leagues():
    from zucai.cloud_apis import fd_scopes

    config = json.loads((ROOT / "data/football_data_co_uk/leagues.json").read_bytes())
    config = {key: config[key] for key in ("E0", "JPN", "D2")}
    assert dict(fd_scopes(datetime(2026, 1, 5).date(), config)) == {
        "E0": {"div": "E0", "seasons": ["2526"]},
        "JPN": {"country": "JPN", "from_season": 2026, "to_season": 2026},
    }
    assert dict(fd_scopes(datetime(2026, 11, 2).date(), config))["E0"]["seasons"] == ["2627"]


def test_api_mode_preserves_official_watermark(environment, tmp_path, monkeypatch):
    path = environment / "feed_manifest.json"
    meta = json.loads(path.read_bytes())
    meta["through_beijing"] = "2026-10-01"
    path.write_text(json.dumps(meta))
    with responses.RequestsMock() as api:
        mock_apis(api)
        run_apis(tmp_path, monkeypatch, environment, "watermark", slot="late")
    assert archive_info(environment)["through_beijing"] == "2026-10-01"


def test_empty_credentials_fail_partially_without_using_runtime_secrets(
    environment, tmp_path, monkeypatch
):
    monkeypatch.delenv("FOOTBALL_DATA_ORG_TOKEN")
    monkeypatch.delenv("API_FOOTBALL_KEY")
    run_apis(tmp_path, monkeypatch, environment, "no-credentials", slot="late")
    meta = archive_info(environment)
    assert meta["collection_status"] == "partial"
    assert meta["collection"]["fdorg"]["requests"] == 0
    assert meta["collection"]["apif"]["requests"] == 0


def test_bad_quota_is_partial_and_does_not_discard_fdorg_results(
    environment, tmp_path, monkeypatch
):
    from zucai.cloud_feeds import digest

    quota = environment / "state/api_football_quota.json"
    quota.write_text("damaged")
    meta = json.loads((environment / "feed_manifest.json").read_bytes())
    meta["files"]["state/api_football_quota.json"] = digest(quota)
    (environment / "feed_manifest.json").write_text(json.dumps(meta))
    with responses.RequestsMock(assert_all_requests_are_fired=False) as api:
        mock_apis(api)
        run_apis(tmp_path, monkeypatch, environment, "bad-quota", slot="late")
    meta = archive_info(environment)
    assert meta["collection_status"] == "partial"
    assert meta["collection"]["fdorg"]["inserted"] == 1
    assert meta["collection"]["apif"]["requests"] == 0
    assert quota.read_text() == "damaged"


def test_apif_candidates_use_restored_jc_and_remain_outside_formal_aliases(
    environment, tmp_path, monkeypatch
):
    from zucai.cloud_feeds import digest

    payload = environment / "state/matches.jsonl"
    row = json.loads(payload.read_text())
    row[2], row[5] = "日职", "2026-11-01T00:00:00.000000Z"
    payload.write_text(json.dumps(row) + "\n")
    meta = json.loads((environment / "feed_manifest.json").read_bytes())
    meta["version"] = 1
    meta.pop("sources")
    meta["files"]["state/matches.jsonl"] = digest(payload)
    (environment / "feed_manifest.json").write_text(json.dumps(meta))
    with responses.RequestsMock() as api:
        mock_apis(api)
        run_apis(tmp_path, monkeypatch, environment, "apif-aliases", slot="late")
    candidates = json.loads((environment / "state/alias_candidates/apif.json").read_bytes())
    assert candidates["日职"]["551"]["canonical"] == "Home"
    evidence = candidates["日职"]["551"]["evidence"][0]
    assert evidence["counterpart_id"] == "jc:1"
    assert evidence["apif_id"] == "apif:9902"
    assert len(evidence["raw_sha256"]) == 64


def test_later_fdorg_window_failure_preserves_first_window_and_other_sources(
    environment, tmp_path, monkeypatch
):
    work = tmp_path / "windows"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("ZUCAI_DB", str(work / "data/zucai.db"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "feeds",
            "apis",
            "--archive",
            str(environment),
            "--days",
            "7",
            "--slot",
            "late",
            "--run-id",
            "windows",
        ],
    )
    with responses.RequestsMock(assert_all_requests_are_fired=False) as api:
        mock_apis(api)
        for start, end, fail in [
            ("2026-10-26", "2026-11-05", False),
            ("2026-11-05", "2026-11-10", True),
        ]:
            doc = (
                {"message": "denied"}
                if fail
                else json.loads((FIXTURES / "fdorg.json").read_bytes())
            )
            api.add(
                responses.GET,
                "https://api.football-data.org/v4/matches",
                json=doc,
                status=403 if fail else 200,
                match=[
                    responses.matchers.query_param_matcher(
                        {
                            "competitions": "PL,ELC,PD,SA,BL1,FL1,DED,PPL,BSA",
                            "dateFrom": start,
                            "dateTo": end,
                        }
                    )
                ],
            )
        main()
    meta = archive_info(environment)
    assert meta["collection_status"] == "partial"
    assert meta["collection"]["fdorg"]["inserted"] == 1
    assert meta["collection"]["fdorg"]["requests"] == 2
    assert meta["sources"]["fdorg"]["counts"]["results"] == 1
    assert meta["sources"]["apif"]["counts"]["results"] == 3


def test_restored_api_archive_is_readable_by_four_source_training(
    environment, tmp_path, monkeypatch
):
    from zucai.db import session_scope
    from zucai.predict.backtest import read_history

    configure_fd(monkeypatch, tmp_path)
    with responses.RequestsMock() as api:
        mock_apis(api, fd=True)
        run_apis(tmp_path, monkeypatch, environment, "training")
    restored = tmp_path / "training-restored/data/zucai.db"
    restore_feeds(environment, restored)
    engine = get_engine(restored)
    try:
        with session_scope(engine) as session:
            history = read_history(session, league="英超", source="jc,fd,fdorg,apif")
        assert set(history.source) == {"fd", "fdorg"}
        assert len(history) == 2
        assert set(history.attrs["sources"]) == {"jc", "fd", "fdorg", "apif"}
    finally:
        engine.dispose()
