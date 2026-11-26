"""Offline checks for action-scoped GitHub API deadlines and summaries."""

import os
import subprocess
from pathlib import Path

import pytest

from test_stats_workflow import WORKFLOW, condition, jobs, script, step

API_JOBS = ("apis", "stats-backfill", "predict", "digest")
RELEASE_JOBS = ("apis", "stats-backfill", "predict")
START = 1_800_000_000


@pytest.mark.parametrize(
    ("name", "wait_minutes", "job_minutes"),
    [("apis", 55, 60), ("stats-backfill", 295, 300), ("predict", 128, 130), ("digest", 70, 75)],
)
def test_github_wait_budget_and_local_usage_path(tmp_path, name, wait_minutes, job_minutes):
    job = jobs()[name]
    setup = step("Set GitHub API budget", job)
    setup_script = script(setup.split("\n      - uses:", 1)[0])
    env_file = tmp_path / "env"
    runner_temp = tmp_path / "runner temp"
    contents = subprocess.run(
        ["bash", "-eu", "-c", f"date() {{ printf '%s\\n' {START}; }}\n" + setup_script],
        env=os.environ
        | {
            "GITHUB_ENV": str(env_file),
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_RUN_ID": "1234",
            "GITHUB_RUN_ATTEMPT": "2",
            "GITHUB_JOB": name,
        },
        text=True,
        capture_output=True,
        check=True,
    )
    assert contents.stdout == ""
    deadline = "PREDICT_OPTIONAL_DEADLINE" if name == "predict" else "ZUCAI_GITHUB_DEADLINE"
    settings = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
    assert int(settings[deadline]) == START + 60 * wait_minutes
    assert Path(settings["ZUCAI_GITHUB_USAGE_FILE"]) == (
        runner_temp / f"zucai-github-api-1234-2-{name}.jsonl"
    )
    assert f"timeout-minutes: {job_minutes}\n" in job
    assert job.index(setup) < job.index("actions/checkout@v4")
    assert ("ZUCAI_GITHUB_RESERVE: '25'" in job) is (name in RELEASE_JOBS)
    assert "secrets." not in setup


def test_all_jobs_record_github_app_requests_even_on_failure():
    for name, job in jobs().items():
        summary = step("Summarize GitHub API calls", job)
        assert condition(summary) == "always()"
        assert job.rstrip().endswith(summary.rstrip())
        assert ("zucai.github_api summary" in summary) is (name in API_JOBS)
        assert "GitHub API" in summary and "0 次" in summary
        assert "secrets." not in summary


@pytest.mark.parametrize("has_usage", [False, True])
def test_job_summary_uses_usage_file_without_contacting_github(tmp_path, has_usage):
    summary = tmp_path / "summary.md"
    usage = tmp_path / "rate.jsonl"
    python = tmp_path / "python"
    python.write_text(
        '#!/bin/sh\nset -eu\n[ "$*" = "-m zucai.github_api summary" ]\n'
        '[ -s "$ZUCAI_GITHUB_USAGE_FILE" ]\n'
        'printf "Python summary\\n" >> "$GITHUB_STEP_SUMMARY"\n'
    )
    python.chmod(0o755)
    if has_usage:
        usage.write_text("{}\n")
    subprocess.run(
        ["bash", "-eu", "-c", script(step("Summarize GitHub API calls", jobs()["predict"]))],
        env=os.environ
        | {
            "PATH": str(tmp_path) + os.pathsep + os.defpath,
            "GITHUB_STEP_SUMMARY": str(summary),
            "ZUCAI_GITHUB_USAGE_FILE": str(usage),
        },
        capture_output=True,
        check=True,
    )
    result = summary.read_text().splitlines()
    assert len(result) == 1
    if has_usage:
        assert result == ["Python summary"]
    else:
        assert "0 次" in result[0] and "未知" in result[0]


@pytest.mark.parametrize(("stage_remaining", "worker_seconds"), [(2160, 1800), (1200, 1170)])
def test_experiment_request_deadline_respects_t88_budget(stage_remaining, worker_seconds):
    source = (WORKFLOW.parents[2] / "scripts/publish-football-experiments.sh").read_text()
    function = "run_experiments() {" + source.split("run_experiments() {", 1)[1].split("\n}", 1)[0]
    output = subprocess.run(
        [
            "bash",
            "-eu",
            "-c",
            f"""
            date() {{ printf '%s\\n' {START}; }}
            timeout() {{ printf '%s\\n' "$ZUCAI_GITHUB_DEADLINE" "$3"; }}
            compute_budget=1800
            deadline={stage_remaining}
            scratch=/unused
            run_id=1234-2
            state=/unused
            SECONDS=0
            {function}
            }}
            run_experiments
            """,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert output.stdout.splitlines() == [str(START + worker_seconds - 125), f"{worker_seconds}s"]


def _powershell_scripts():
    for item in WORKFLOW.read_text(encoding="utf-8").split("      - name:"):
        if "shell: powershell" not in item or "        run: |\n" not in item:
            continue
        lines = []
        for line in item.split("        run: |\n", 1)[1].split("\n"):
            if line.strip() and not line.startswith("          "):
                break
            lines.append(line)
        yield item.split("\n", 1)[0].strip(), "\n".join(lines)


def test_powershell_step_scripts_are_ascii_only():
    """Windows PowerShell reads step scripts in the system code page, not UTF-8."""
    scripts = dict(_powershell_scripts())
    assert "Summarize GitHub API calls" in scripts
    assert [name for name, text in scripts.items() if not text.isascii()] == []


def test_summary_step_cannot_fail_a_job():
    for name, job in jobs().items():
        summary = step("Summarize GitHub API calls", job)
        assert "        continue-on-error: true\n" in summary, name
        if "shell: powershell" in summary:
            assert "ZUCAI_SUMMARY_LINE: 'GitHub 接口调用 0 次" in summary
            assert "$env:ZUCAI_SUMMARY_LINE | Out-File" in script(summary)
