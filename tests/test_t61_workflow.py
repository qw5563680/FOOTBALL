"""Offline dispatch execution and CLI validation for read-only and backfill catalogues."""

import json
import os
import subprocess
import sys
from types import ModuleType

import pytest

from test_cloud_feeds import seed_cloud_archive
from test_stats_workflow import condition, evaluate, jobs, script, step
from zucai.cloud_feeds import archive_info, main

PROBE_KEY = "fromJSON(inputs.options || '{}').probe_kind"


def enabled_jobs(kind, **overrides):
    context = {"inputs.operation": "probe", PROBE_KEY: kind} | overrides
    return {name for name, job in jobs().items() if evaluate(condition(job), **context)}


@pytest.mark.parametrize("ref", ["main", "football-ingest-run-t61", "football-ingest-verify-t61"])
def test_jc_catalogue_dispatch_runs_only_selfhosted_catalogue_job(ref):
    assert enabled_jobs("jc-catalogue", **{"github.ref_name": ref}) == {"jc-catalogue"}
    assert enabled_jobs("catalogue", **{"github.ref_name": ref}) == {"apis"}


@pytest.mark.parametrize("event", ["schedule", "push"])
def test_jc_catalogue_never_runs_for_schedule_or_push(event):
    assert "jc-catalogue" not in enabled_jobs("jc-catalogue", **{"github.event_name": event})


@pytest.mark.parametrize("kind", ["catalogue", "jc-catalogue"])
def test_catalogue_shell_routes_without_legacy_default_scopes(tmp_path, kind):
    title = (
        "Restore and enumerate monthly JC catalogue"
        if kind == "jc-catalogue"
        else "Probe all API league catalogues"
    )
    job = jobs()["jc-catalogue" if kind == "jc-catalogue" else "apis"]
    collect = step(title, job)
    recorder = tmp_path / "python"
    recorder.write_text('#!/bin/bash\nprintf "%s\\0" "$@" > "$CAPTURE_ARGS"\n')
    recorder.chmod(0o755)
    capture = tmp_path / "argv"
    env = dict(
        PATH=str(tmp_path) + os.pathsep + os.defpath,
        WORK_DIR=str(tmp_path / "work"),
        CAPTURE_ARGS=str(capture),
        GITHUB_RUN_ID="offline",
        GITHUB_RUN_ATTEMPT="1",
        PROBE_KIND=kind,
        PROBE_LEAGUES="",
        PROBE_SEASONS="",
        PROBE_PER_SCOPE="1",
    )
    subprocess.run(
        ["/bin/bash", "-eu", "-c", script(collect)], env=env, check=True, capture_output=True
    )
    argv = capture.read_bytes().decode().split("\0")[:-1]
    assert argv[:3] == ["-m", "zucai.cloud_feeds", "probe"]
    assert argv[argv.index("--probe-kind") + 1] == kind
    assert "--leagues" not in argv and "--seasons" not in argv and "--per-scope" not in argv
    if kind == "jc-catalogue":
        assert "--source" not in argv
        assert "runs-on: [self-hosted, football-local]" in job
        assert "API_FOOTBALL_KEY" not in job and "FOOTBALL_DATA_ORG_TOKEN" not in job
    else:
        assert "FOOTBALL_DATA_ORG_TOKEN: ${{ secrets.FOOTBALL_DATA_ORG_TOKEN }}" in collect


@pytest.mark.parametrize(
    "catalogue,resume,wanted",
    [
        ("true", "true", ["--catalogue", "--max-requests", "42", "--resume"]),
        ("true", "false", ["--catalogue", "--max-requests", "42", "--no-resume"]),
        ("false", "true", []),
    ],
)
def test_backfill_shell_forwards_budget_and_resume_only_for_catalogue(
    tmp_path, catalogue, resume, wanted
):
    collect = step("Restore and backfill verified history once", jobs()["apis"])
    recorder = tmp_path / "python"
    recorder.write_text('#!/bin/bash\nprintf "%s\\0" "$@" > "$CAPTURE_ARGS"\n')
    recorder.chmod(0o755)
    capture = tmp_path / "argv"
    env = dict(
        PATH=str(tmp_path) + os.pathsep + os.defpath,
        WORK_DIR=str(tmp_path / "work"),
        CAPTURE_ARGS=str(capture),
        GITHUB_RUN_ID="offline",
        GITHUB_RUN_ATTEMPT="1",
        BACKFILL_SOURCE="apif",
        BACKFILL_LEAGUES="",
        BACKFILL_FROM="",
        BACKFILL_TO="",
        BACKFILL_CATALOGUE=catalogue,
        BACKFILL_MAX_REQUESTS="42",
        BACKFILL_RESUME=resume,
    )
    subprocess.run(
        ["/bin/bash", "-eu", "-c", script(collect)], env=env, check=True, capture_output=True
    )
    argv = capture.read_bytes().decode().split("\0")[:-1]
    if wanted:
        assert argv[-len(wanted) :] == wanted
    else:
        assert "--catalogue" not in argv and "--max-requests" not in argv and "--resume" not in argv


@pytest.mark.parametrize(
    "options,valid",
    [
        ('{"catalogue":true}', True),
        ('{"catalogue":false}', True),
        ('{"catalogue":"true"}', False),
        ('{"catalogue":1}', False),
        ('{"probe_kind":"catalogue"}', True),
        ('{"probe_kind":"jc-catalogue"}', True),
    ],
)
def test_dispatch_validates_new_options_before_collection(tmp_path, options, valid):
    for job in ("apis", "stats-backfill", "jc-catalogue"):
        validate = step("Validate dispatch options", jobs()[job])
        env = os.environ | {"DISPATCH_OPTIONS": options, "ZUCAI_OPERATION": "backfill"}
        result = subprocess.run(
            ["/bin/bash", "-eu", "-c", script(validate)], cwd=tmp_path, env=env, capture_output=True
        )
        assert (result.returncode == 0) is valid
        assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "command,options",
    [
        ("apis", ["--catalogue"]),
        ("probe", ["--catalogue"]),
        ("backfill", ["--max-requests", "1"]),
        ("backfill", ["--resume"]),
        ("backfill", ["--no-resume"]),
        ("backfill", ["--catalogue", "--max-requests", "0"]),
        ("backfill", ["--catalogue", "--relink-jc"]),
        ("backfill", ["--catalogue", "--relink-fdorg"]),
        ("probe", ["--probe-kind", "catalogue", "--leagues", "39"]),
        ("probe", ["--probe-kind", "catalogue", "--seasons", "2026"]),
        ("probe", ["--probe-kind", "jc-catalogue", "--source", "apif"]),
        ("probe", ["--probe-kind", "jc-catalogue", "--per-scope", "2"]),
    ],
)
def test_cli_rejects_invalid_catalogue_arguments_before_restore(
    tmp_path, monkeypatch, command, options
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "github-hosted")
    monkeypatch.setattr(
        sys, "argv", ["feeds", command, "--archive", "absent", "--run-id", "invalid", *options]
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "kind,runner,module_name,collector_name",
    [
        ("catalogue", "github-hosted", "cloud_catalogue", "collect_catalogue"),
        ("jc-catalogue", "self-hosted", "cloud_jc_catalogue", "collect_jc_catalogue"),
    ],
)
def test_probe_cli_archives_generic_catalogue_collection_and_uses_correct_summary(
    tmp_path, monkeypatch, kind, runner, module_name, collector_name
):
    archive = seed_cloud_archive(tmp_path)
    before = archive_info(archive)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("RUNNER_ENVIRONMENT", runner)
    fake = ModuleType(f"zucai.{module_name}")
    summary = tmp_path / "summary.json"

    def collector(database, run_id):
        assert database.is_file() and run_id == "route"
        return dict(collection="catalogue", status="complete", requests=2, marker=kind)

    setattr(fake, collector_name, collector)
    fake.write_summary = lambda result: summary.write_text(json.dumps(result))
    if kind == "jc-catalogue":
        fake.require_jc_catalogue_actions = lambda: None
    monkeypatch.setitem(sys.modules, f"zucai.{module_name}", fake)
    monkeypatch.setattr(
        sys,
        "argv",
        ["feeds", "probe", "--archive", str(archive), "--run-id", "route", "--probe-kind", kind],
    )
    main()
    after = archive_info(archive)
    assert after["collection"] == {
        "catalogue": {
            "collection": "catalogue",
            "status": "complete",
            "requests": 2,
            "marker": kind,
        }
    }
    assert json.loads(summary.read_bytes()) == after["collection"]["catalogue"]
    assert after["counts"] == before["counts"] and after["sources"] == before["sources"]
    assert after["through_beijing"] == before["through_beijing"]


def test_catalogue_backfill_cli_preserves_existing_provider_report_contract(tmp_path, monkeypatch):
    archive = seed_cloud_archive(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "github-hosted")
    fake = ModuleType("zucai.cloud_catalogue_backfill")
    summary = tmp_path / "summary.json"

    def collector(database, run_id, **options):
        assert database.is_file()
        return dict(status="complete", run_id=run_id, requested=options)

    fake.collect_catalogue_backfill = collector
    fake.write_summary = lambda result: summary.write_text(json.dumps(result))
    monkeypatch.setitem(sys.modules, "zucai.cloud_catalogue_backfill", fake)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "feeds",
            "backfill",
            "--archive",
            str(archive),
            "--run-id",
            "resume",
            "--catalogue",
            "--source",
            "fdorg",
            "--leagues",
            "CL",
            "--from",
            "2023",
            "--to",
            "2026",
            "--max-requests",
            "42",
            "--no-resume",
        ],
    )
    main()
    report = archive_info(archive)["collection"]["fdorg"]
    assert report["requested"] == dict(
        provider="fdorg", codes="CL", first="2023", last="2026", max_requests=42, resume=False
    )
    assert json.loads(summary.read_bytes()) == report
