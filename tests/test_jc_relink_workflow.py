"""T40 manual CLI and Actions integration, using only the synthetic T09 fixture."""

import csv
import json
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path

import pytest

from test_fd_counterpart import clone, fdorg
from test_fd_counterpart import db as db
from test_stats_workflow import WORKFLOW, condition, evaluate, jobs, script, step
from zucai.cloud_feeds import archive_info, export_feeds, main, restore_feeds
from zucai.db import get_engine, init_db, session_scope, upsert_match
from zucai.models import Match, Result, Team

ROOT = Path(__file__).parents[1]
SAMPLE = json.loads((ROOT / "data/fixtures/t09/synthetic_ingest.json").read_bytes())
GAME = SAMPLE["fixture"]
WHEN = datetime.fromisoformat(GAME["kickoff_utc"])
JC_ID = f"jc:{GAME['provider_match_id']}"


def arguments(archive, *extra, command="backfill"):
    return [
        "cloud_feeds",
        command,
        "--relink-jc",
        "--archive",
        str(archive),
        "--run-id",
        "offline",
        *extra,
    ]


@pytest.mark.parametrize(
    "actions,event",
    [("", ""), ("true", "schedule"), ("true", "push"), ("false", "workflow_dispatch")],
)
def test_relink_jc_rejects_non_manual_actions_before_restoration(
    tmp_path, monkeypatch, actions, event
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_ACTIONS", actions)
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setattr(sys, "argv", arguments(tmp_path / "absent"))
    with pytest.raises(ValueError, match="workflow_dispatch"):
        main()
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize(
    "command,extra",
    [
        ("apis", []),
        ("verify", []),
        ("backfill", ["--relink-fdorg"]),
        ("backfill", ["--source", "apif"]),
        ("backfill", ["--leagues", "CL"]),
        ("backfill", ["--from", "2023"]),
        ("backfill", ["--to", "2026"]),
        ("backfill", ["--seasons", "2026"]),
        ("backfill", ["--probe-kind", "stats"]),
        ("backfill", ["--per-scope", "1"]),
    ],
)
def test_relink_jc_rejects_conflicting_arguments_before_restoration(
    tmp_path, monkeypatch, capsys, command, extra
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setattr(sys, "argv", arguments(tmp_path / "absent", *extra, command=command))
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert "unrecognized arguments" not in capsys.readouterr().err
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("provider", ["apif", "fdorg"])
def test_relink_jc_cli_preserves_evidence_and_restores_hashed_audit(
    tmp_path, monkeypatch, provider
):
    database = tmp_path / "seed/data/zucai.db"
    api_id = f"{provider}:{GAME['provider_match_id']}"
    engine = get_engine(database)
    init_db(engine)
    with session_scope(engine) as session:
        for team_id in (GAME["home_provider_id"], GAME["away_provider_id"]):
            name = SAMPLE["aliases"][GAME["league"]][str(team_id)]["canonical"]
            session.add(Team(id=team_id, name=name))
        session.flush()
        for match_id, source in ((api_id, provider), (JC_ID, "jc")):
            upsert_match(
                session,
                match_id=match_id,
                source=source,
                league=GAME["league"],
                home_team_id=GAME["home_provider_id"],
                away_team_id=GAME["away_provider_id"],
                kickoff_utc=WHEN,
                status="finished",
            )
            session.add(
                Result(
                    match_id=match_id,
                    home_goals=GAME["full"][0],
                    away_goals=GAME["full"][1],
                    ht_home_goals=GAME["half"][0],
                    ht_away_goals=GAME["half"][1],
                    available_utc=SAMPLE["delayed_finish"]["finish_seen"],
                    available_source="captured",
                )
            )
    engine.dispose()
    archive = tmp_path / "archive"
    prior = export_feeds(database, archive, WHEN.date(), "before")
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.delenv("FOOTBALL_DATA_ORG_TOKEN", raising=False)
    monkeypatch.delenv("API_FOOTBALL_KEY", raising=False)
    monkeypatch.setattr("requests.sessions.Session.request", lambda *a, **k: pytest.fail("network"))
    for attempt in (1, 2):
        work = tmp_path / f"work-{attempt}"
        work.mkdir()
        monkeypatch.chdir(work)
        monkeypatch.setenv("ZUCAI_DB", str(work / "data/zucai.db"))
        monkeypatch.setattr(sys, "argv", arguments(archive))
        main()
        after = archive_info(archive)
        assert after["counts"] == prior["counts"]
        assert after["through_beijing"] == prior["through_beijing"]
        assert set(after["collection"]) == {"jc"}
        report = after["collection"]["jc"]
        assert report["operation"] == "relink-jc" and report["requests"] == 0
        assert (report["linked"], report["already_linked"], report["skipped"]) == (
            (1, 0, 0) if attempt == 1 else (0, 1, 0)
        )
        assert report["pairs"][0]["api_id"] == api_id
        assert report["pairs"][0]["jc_id"] == JC_ID
        for source in ("fd", "fdorg", "apif"):
            assert after["sources"][source] == prior["sources"][source]
        for table in ("teams", "results", "closing_odds"):
            assert after["files"][f"state/{table}.jsonl"] == prior["files"][f"state/{table}.jsonl"]
        restored = tmp_path / f"restored-{attempt}/data/zucai.db"
        restore_feeds(archive, restored)
        audit = "state/audit/jc_relink.csv"
        assert audit in after["files"]
        assert (restored.parent / audit).read_bytes() == (archive / audit).read_bytes()
        with (archive / audit).open(newline="", encoding="utf-8") as stream:
            records = list(csv.DictReader(stream))
        assert any(row["api_id"] == api_id and row["jc_id"] == JC_ID for row in records)
        with closing(sqlite3.connect(restored)) as db:
            assert dict(db.execute("SELECT match_id,counterpart_id FROM matches")) == {
                api_id: None,
                JC_ID: api_id,
            }
    assert api_id in summary.read_text() and JC_ID in summary.read_text()
    with (archive / audit).open("ab") as stream:
        stream.write(b"tampered\n")
    with pytest.raises(ValueError, match="jc_relink.csv"):
        archive_info(archive)


def test_collection_and_backfill_summaries_count_real_late_jc_links(db, tmp_path, monkeypatch):
    from zucai.cloud_api_backfill import write_summary

    with session_scope(db) as session:
        clone(session, session.get(Match, f"fdorg:{GAME['provider_match_id']}"), JC_ID)
    archive = tmp_path / "archive"
    export_feeds(Path(db.url.database), archive, WHEN.date(), "before")

    def replay_fixture(database, now, days, slot):
        engine = get_engine(database)
        try:
            with session_scope(engine) as session:
                result = fdorg(session)
        finally:
            engine.dispose()
        return {"fdorg": result | {"status": "complete", "requests": 0, "errors": []}}

    monkeypatch.setattr("zucai.cloud_apis.collect_apis", replay_fixture)
    work = tmp_path / "daily"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("ZUCAI_DB", str(work / "data/zucai.db"))
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(
        sys, "argv", ["cloud_feeds", "apis", "--archive", str(archive), "--run-id", "offline"]
    )
    main()
    report = archive_info(archive)["collection"]["fdorg"]
    assert report["linked_late_jc"] == 1 and report["linked_existing"] == 0
    scope = report | {
        "scope": "PL/2026",
        "league": GAME["league"],
        "season": 2026,
        "parsed_matches": 1,
        "matches_after": 1,
        "results_after": 0,
    }
    write_summary(
        report | {"source": "fdorg", "scopes": [scope], "errors": [], "remaining_quota": None}
    )
    output = summary.read_text()
    assert "| fdorg | complete | 0 | 1 | 0 | 0 |" in output
    assert "| 英超/2026 | complete | 1 | 1 | 0 | 0 | 1 | 0 | 0 |" in output


@pytest.mark.parametrize("ref", ["main", "football-ingest-run-t40", "football-ingest-verify-t40"])
def test_relink_jc_workflow_runs_only_offline_relink_and_archive_steps(ref):
    context = {"inputs.operation": "relink-jc", "github.ref_name": ref}
    assert {name for name, job in jobs().items() if evaluate(condition(job), **context)} == {"apis"}
    mode = re.search(r"FEED_MODE: \$\{\{ (.*?) \}\}", WORKFLOW.read_text()).group(1)
    assert evaluate(mode, **context) == "relink-jc"
    active = [
        value
        for value in jobs()["apis"].split("      - name:")[1:]
        if "        if:" not in value
        or evaluate(condition(value), **(context | {"env.FEED_MODE": "relink-jc"}))
    ]
    assert all("secrets." not in value for value in active)
    commands = [value for value in active if "python -m zucai." in value]
    assert len(commands) == 1 and "backfill --relink-jc" in commands[0]
    assert any("publish-football-feeds.sh" in value for value in active)
    assert any("data/state" in value and "upload-artifact" in value for value in active)


@pytest.mark.parametrize("event", ["schedule", "push"])
def test_relink_jc_step_never_runs_for_nonmanual_events(event):
    mode = re.search(r"FEED_MODE: \$\{\{ (.*?) \}\}", WORKFLOW.read_text()).group(1)
    selected = evaluate(mode, **{"github.event_name": event, "inputs.operation": ""})
    relink = step("Relink existing APIs to late jc without network", jobs()["apis"])
    assert not evaluate(condition(relink), **{"env.FEED_MODE": selected})


def test_relink_jc_checked_in_shell_invokes_offline_command(tmp_path):
    relink = step("Relink existing APIs to late jc without network", jobs()["apis"])
    recorder = tmp_path / "python"
    recorder.write_text('#!/bin/bash\nprintf "%s\\0" "$@" > "$CAPTURE_ARGS"\n')
    recorder.chmod(0o755)
    capture = tmp_path / "argv"
    env = dict(
        PATH=str(tmp_path) + os.pathsep + os.defpath,
        WORK_DIR=str(tmp_path / "work"),
        CAPTURE_ARGS=str(capture),
        GITHUB_RUN_ID="offline",
        GITHUB_RUN_ATTEMPT="2",
    )
    subprocess.run(
        ["/bin/bash", "-eu", "-c", script(relink)], env=env, check=True, capture_output=True
    )
    assert capture.read_bytes().decode().split("\0")[:-1] == [
        "-m",
        "zucai.cloud_feeds",
        "backfill",
        "--relink-jc",
        "--archive",
        "../cloud-data",
        "--run-id",
        "offline-2",
    ]
