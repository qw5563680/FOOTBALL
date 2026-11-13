"""Exercise candidate dispatch isolation and the checked-in shell without network."""

import json
import os
import re
import subprocess

import pytest

from test_stats_workflow import OPERATIONS, WORKFLOW, condition, evaluate, jobs, script, step

REFS = ("main", "football-ingest-run-t59", "football-ingest-verify-t59")


def enabled_jobs(**context):
    return {name for name, job in jobs().items() if bool(evaluate(condition(job), **context))}


@pytest.mark.parametrize("ref", REFS)
@pytest.mark.parametrize("gate", [False, True])
def test_candidate_dispatch_never_runs_collection_or_followups_even_on_legacy_tags(ref, gate):
    assert enabled_jobs(
        **{
            "github.ref_name": ref,
            "inputs.operation": "candidate-review",
            "inputs.gate": gate,
        }
    ) == {"candidate-review"}


@pytest.mark.parametrize("operation", (*OPERATIONS, "daily"))
@pytest.mark.parametrize("ref", REFS)
def test_prior_dispatch_routes_are_preserved(operation, ref):
    # Legacy tags intentionally still add their original jobs for old operations.
    expected = {
        "apis": {"apis"},
        "sync": {"sporttery"},
        "verify": {"apis"},
        "predict": {"predict", "scoreboard", "digest"},
        "backfill": {"apis"},
        "scoreboard": {"scoreboard", "digest"},
        "probe": {"apis"},
        "identity": {"apis"},
        "reconcile": {"apis"},
        "relink-fdorg": {"apis"},
        "relink-jc": {"apis"},
        "digest": {"digest"},
        "stats-backfill": {"stats-backfill"},
        "daily": {"apis", "sporttery", "predict", "scoreboard", "digest"},
    }[operation].copy()
    if ref == "football-ingest-run-t59" and operation in {"apis", "verify", "predict"}:
        expected.add("sporttery")
    if ref == "football-ingest-verify-t59" and operation in {"sync", "predict"}:
        expected.add("apis")
    assert enabled_jobs(**{"github.ref_name": ref, "inputs.operation": operation}) == expected


@pytest.mark.parametrize(
    "event,cron,ref,expected",
    [
        (
            "schedule",
            "40 2 * * *",
            "main",
            {"apis", "sporttery", "predict", "scoreboard", "digest"},
        ),
        (
            "schedule",
            "40 7 * * *",
            "main",
            {"schedule-gate", "sporttery", "predict", "scoreboard", "digest"},
        ),
        (
            "schedule",
            "40 9 * * *",
            "main",
            {"apis", "sporttery", "predict", "scoreboard", "digest"},
        ),
        ("push", "", "main", set()),
        ("push", "", "football-ingest-run-t59", {"sporttery"}),
        ("push", "", "football-ingest-verify-t59", {"apis"}),
    ],
)
def test_schedules_and_legacy_push_routes_do_not_enter_review(event, cron, ref, expected):
    assert (
        enabled_jobs(
            **{
                "github.event_name": event,
                "github.event.schedule": cron,
                "github.ref_name": ref,
                "inputs.operation": "",
            }
        )
        == expected
    )


def test_review_uses_existing_input_budget_and_an_independent_secret_free_job():
    workflow = WORKFLOW.read_text()
    inputs = workflow.split("    inputs:\n", 1)[1].split("  push:", 1)[0]
    assert len(re.findall(r"^      (\w+):", inputs, re.M)) == 10
    assert "stats-backfill, candidate-review, daily]" in inputs
    assert "candidate-review" in inputs.split("      options:\n", 1)[1]
    job = jobs()["candidate-review"]
    assert "needs:" not in job and "secrets." not in job
    assert "runs-on: ubuntu-latest" in job and "python-version: '3.12'" in job
    assert "path: app\n          persist-credentials: false" in job
    assert "ref: data/football-feeds\n          path: cloud-data\n          fetch-depth: 0" in job
    assert "group: write-football-feed-data\n  cancel-in-progress: false" in workflow
    assert "pip install -e ./app" in job
    assert job.count("python -m zucai.") == 1
    for forbidden in (
        "API_FOOTBALL_KEY",
        "FOOTBALL_DATA_ORG_TOKEN",
        "cloud_feeds",
        "cloud_predict",
        "daily_digest",
        "digest_notify",
        "scoreboard",
    ):
        assert forbidden not in job
    review = step("Restore and apply approved candidate review", job)
    assert "DISPATCH_OPTIONS: ${{ inputs.options }}" in review
    assert "${{ inputs." not in script(review)


@pytest.mark.parametrize(
    "options",
    [
        '{"plan":"data/jc_candidate_reviews/2026-11-13-first.json"}',
        json.dumps({"plan": "data/jc_candidate_reviews/$(touch injected);`touch injected`.json"}),
        '{"plan":"data/jc_candidate_reviews/quote\\" and space.json"}',
        "{}",
        "[]",
        "not JSON; $(touch injected)",
    ],
)
def test_review_shell_preserves_untrusted_options_as_one_argument(tmp_path, options):
    review = step("Restore and apply approved candidate review", jobs()["candidate-review"])
    recorder = tmp_path / "python"
    recorder.write_text('#!/bin/bash\nprintf "%s\\0" "$@" > "$CAPTURE_ARGS"\n')
    recorder.chmod(0o755)
    work = tmp_path / "work with spaces"
    capture = tmp_path / "argv"
    env = dict(
        PATH=str(tmp_path) + os.pathsep + os.defpath,
        WORK_DIR=str(work),
        CAPTURE_ARGS=str(capture),
        DISPATCH_OPTIONS=options,
        GITHUB_RUN_ID="offline;$(touch injected)",
        GITHUB_RUN_ATTEMPT="2`touch injected`",
    )
    subprocess.run(
        ["/bin/bash", "-eu", "-c", script(review)], env=env, check=True, capture_output=True
    )
    assert capture.read_bytes().decode().split("\0")[:-1] == [
        "-m",
        "zucai.cloud_candidate_review",
        "--archive",
        "../cloud-data",
        "--dispatch-options",
        options,
        "--run-id",
        "offline;$(touch injected)-2`touch injected`",
    ]
    assert work.is_dir() and not list(work.iterdir())
    # Reusing an earlier restored DB must fail before invoking the module again.
    capture.unlink()
    result = subprocess.run(
        ["/bin/bash", "-eu", "-c", script(review)], env=env, capture_output=True
    )
    assert result.returncode != 0 and not capture.exists()


def test_recorded_archive_head_and_recovery_directory_reach_existing_publisher(tmp_path):
    job = jobs()["candidate-review"]
    archive = tmp_path / "cloud-data"
    archive.mkdir()
    subprocess.run(["git", "init", "--quiet", str(archive)], check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Offline",
            "-c",
            "user.email=offline@example.invalid",
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            "archive",
        ],
        cwd=archive,
        check=True,
        capture_output=True,
    )
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=archive, text=True).strip()
    output = tmp_path / "output"
    capture = step("Record archive revision before review", job)
    assert "id: archive_base" in capture and "working-directory: cloud-data" in capture
    subprocess.run(
        ["/bin/bash", "-eu", "-c", script(capture)],
        cwd=archive,
        env=os.environ | {"GITHUB_OUTPUT": str(output)},
        check=True,
        capture_output=True,
    )
    assert output.read_text() == f"head={head}\n"
    publisher = tmp_path / "app/scripts/publish-football-feeds.sh"
    publisher.parent.mkdir(parents=True)
    publisher.write_text(
        'printf "%s\\0" "$PWD" "$PUBLISH_EXPECTED_BASE" "$PUBLISH_RECOVERY_DIR" > "$CAPTURE_ARGS"\n'
    )
    publish = step("Publish reviewed queue and audit", job)
    assert "PUBLISH_EXPECTED_BASE: ${{ steps.archive_base.outputs.head }}" in publish
    assert "working-directory: cloud-data" in publish
    assert "        if:" not in publish  # Default success() never publishes a failed apply.
    argv = tmp_path / "publish-args"
    subprocess.run(
        ["/bin/bash", "-eu", "-c", publish.split("        run: ", 1)[1].strip()],
        cwd=archive,
        env=os.environ
        | {"PUBLISH_EXPECTED_BASE": head, "WORK_DIR": "work review", "CAPTURE_ARGS": str(argv)},
        check=True,
        capture_output=True,
    )
    assert argv.read_bytes().decode().split("\0")[:-1] == [
        str(archive),
        head,
        "../work review/publication-recovery",
    ]


def test_missing_archive_head_stops_before_an_empty_publication_guard_is_recorded(tmp_path):
    capture = step("Record archive revision before review", jobs()["candidate-review"])
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    output = tmp_path / "output"
    result = subprocess.run(
        ["/bin/bash", "-eu", "-c", script(capture)],
        cwd=tmp_path,
        env=os.environ | {"GITHUB_OUTPUT": str(output)},
        capture_output=True,
    )
    assert result.returncode != 0 and not output.exists()


def test_failure_artifact_keeps_only_review_and_publication_evidence():
    job = jobs()["candidate-review"]
    artifact = step("Keep candidate review evidence even on failure", job)
    assert "if: ${{ always() }}" in artifact
    assert "uses: actions/upload-artifact@v4" in artifact and "retention-days: 90" in artifact
    paths = artifact.split("          path: |\n", 1)[1].split("          retention-days:", 1)[0]
    assert {line.strip() for line in paths.splitlines()} == {
        "${{ env.WORK_DIR }}/approved-plan.json",
        "${{ env.WORK_DIR }}/summary.md",
        "${{ env.WORK_DIR }}/publication-recovery",
        "cloud-data/state/audit/jc_candidate_reviews",
        "cloud-data/state/alias_candidates/jc.json",
        "cloud-data/feed_manifest.json",
    }
