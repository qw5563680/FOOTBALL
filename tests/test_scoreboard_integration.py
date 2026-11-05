"""New report annotations, unchanged old exports, and the actual workflow conditions."""

import json
import re
from copy import deepcopy
from pathlib import Path

import pytest

from test_scoreboard_command import files
from test_scoreboard_command import scoreboard_archive as scoreboard_archive
from test_scoreboard_score import FIXTURES
from zucai.report.render import load_batch, write_report


def test_report_evidence_uses_hashed_scored_rows_not_edited_summary_counts(scoreboard_archive):
    from datetime import timedelta

    from test_scoreboard_score import NOW
    from zucai.scoreboard.command import run
    from zucai.scoreboard.evidence import read_evidence
    from zucai.scoreboard.score import instant

    run(scoreboard_archive, as_of=NOW)
    path = scoreboard_archive / "scoreboard/summary.json"
    summary = json.loads(path.read_bytes())
    summary["probability"]["leagues"]["测试联赛"].update(n=200, prospective_verified=True)
    path.write_text(json.dumps(summary), encoding="utf-8")
    cutoff = instant(summary["generated_utc"]) + timedelta(seconds=1)
    assert read_evidence(scoreboard_archive, cutoff)["leagues"]["测试联赛"]["n"] == 4


def test_summary_copies_existing_scorelines_without_touching_frozen_files(
    scoreboard_archive, monkeypatch, tmp_path
):
    from zucai.cloud_predict import _summary

    archive = scoreboard_archive
    path = "forecasts/2026-11-01/late"
    empty = "forecasts/2026-11-02/late"
    run = {
        "run_id": "late",
        "status": "complete",
        "sporttery_stale": False,
        "leagues": {},
        "forecasts": {
            "2026-11-01": {"path": path, "predicted": 8, "excluded": 0, "reasons": {}},
            "2026-11-02": {"path": empty, "predicted": 0, "excluded": 0, "reasons": {}},
        },
    }
    before = files(archive)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    _summary(archive, run)
    text = summary.read_text()
    section = (archive / path / "report.md").read_text().split("## 比分前五与快照\n", 1)[1]
    assert "## 比分前五与快照\n" + section in text
    assert "1:0（48.00%）" in text
    assert "比分前五" not in text.split("### 2026-11-02", 1)[1]
    assert files(archive) == before


def test_new_report_freezes_prospective_evidence_old_report_stays_identical(tmp_path):
    from zucai.scoreboard.evidence import annotate_batch

    batch = json.loads((FIXTURES / "forecasts.json").read_bytes())[1]
    write_report(batch, tmp_path / "old")
    original = files(tmp_path / "old")
    annotated = deepcopy(batch)
    annotate_batch(annotated, None)
    write_report(annotated, tmp_path / "new")
    text = (tmp_path / "new/report.md").read_text()
    assert "P2 可用 · 仅历史证据" in text
    assert all(not r["prospective_verified"] for r in annotated["predictions"])
    evidence = {
        "as_of_utc": "2026-10-31T12:00:00Z",
        "generated_utc": "2026-10-31T12:01:00Z",
        "summary_sha256": "f" * 64,
        "leagues": {"测试联赛": {"n": 200, "model_ll": 1.0, "uniform_ll": 1.0986122886681098}},
    }
    annotate_batch(annotated, evidence)
    write_report(annotated, tmp_path / "verified")
    assert "P2 可用 · 前瞻已验证" in (tmp_path / "verified/report.md").read_text()
    assert all(r["p2_ready"] for r in annotated["predictions"])
    assert annotated["bet_allowed"] is False
    write_report(load_batch(tmp_path / "old/forecast.json"), tmp_path / "old-copy")
    assert files(tmp_path / "old-copy") == original
    evidence["generated_utc"] = "2026-11-02T00:00:00Z"
    annotate_batch(annotated, evidence)
    assert not any(r["prospective_verified"] for r in annotated["predictions"])


@pytest.mark.parametrize(
    ("event", "operation", "cancelled", "predict", "expected"),
    [
        ("schedule", "", False, "skipped", True),
        ("schedule", "", False, "success", True),
        ("schedule", "", False, "failure", True),
        ("schedule", "", True, "success", False),
        ("workflow_dispatch", "predict", False, "success", True),
        ("workflow_dispatch", "scoreboard", False, "skipped", True),
        ("workflow_dispatch", "backfill", False, "skipped", False),
        ("workflow_dispatch", "verify", False, "skipped", False),
        ("workflow_dispatch", "apis", False, "skipped", False),
        ("push", "", False, "skipped", False),
    ],
)
def test_scoreboard_runs_both_schedules_and_manual_score_or_predict(
    event, operation, cancelled, predict, expected
):
    text = (Path(__file__).parents[1] / ".github/workflows/football-feeds.yml").read_text()
    assert "  scoreboard:\n" in text
    job = re.search(r"^  scoreboard:\n(.*?)(?=^  \w+:\n|\Z)", text, re.M | re.S).group(1)
    expr = re.search(r"if: \$\{\{ (.*?) \}\}", job).group(1)
    for key, value in {
        "github.event_name": event,
        "github.event.schedule": "40 2 * * *",
        "inputs.operation": operation,
        "needs.predict.result": predict,
        "needs.schedule-gate.outputs.run_early": "",
        "needs.schedule-gate.result": "skipped",
    }.items():
        expr = expr.replace(key, repr(value))
    expr = (
        expr.replace("!cancelled()", repr(not cancelled))
        .replace("&&", " and ")
        .replace("||", " or ")
    )
    assert eval(expr, {"__builtins__": {}}, {}) is expected
    assert "needs: [schedule-gate, predict]" in job and "runs-on: ubuntu-latest" in job
    assert "zucai.scoreboard run" in job and "publish-football-feeds.sh" in job
