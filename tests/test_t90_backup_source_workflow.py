"""The public probe has a manual, isolated runner with a fixed request scope."""

import json
import sys
from types import ModuleType

import pytest

from test_stats_workflow import OPERATIONS, condition, evaluate, jobs, script, step
from test_t61_workflow import enabled_jobs

KIND = "backup-sources"


@pytest.mark.parametrize("ref", ["main", "football-ingest-run-t90", "football-ingest-verify-t90"])
def test_manual_probe_only_runs_the_read_only_collection_runner(ref):
    assert enabled_jobs(KIND, **{"github.ref_name": ref}) == {"backup-source-probe"}
    job = jobs()["backup-source-probe"]
    assert "runs-on: [self-hosted, football-local]" in job
    assert "contents: read" in job and "persist-credentials: false" in job
    assert "secrets." not in job and "data/football-feeds" not in job
    assert "publish-football-feeds.sh" not in job
    collect = step("Probe public backup schedules", job)
    assert "DISPATCH_OPTIONS: ${{ inputs.options }}" in collect
    assert "-m zucai.cloud_backup_source_probe --out $output" in script(collect)
    artifact = step("Preserve backup source probe evidence", job)
    assert "uses: actions/upload-artifact@v4" in artifact
    path = "${{ runner.temp }}/backup-sources-${{ github.run_id }}-${{ github.run_attempt }}"
    assert f"path: {path}" in artifact


def test_public_probe_cannot_run_for_other_events_or_operations():
    expression = condition(jobs()["backup-source-probe"])
    for event in ("schedule", "push", "pull_request"):
        assert "backup-source-probe" not in enabled_jobs(KIND, **{"github.event_name": event})
    for operation in (*OPERATIONS, "daily", "candidate-review"):
        if operation != "probe":
            enabled = enabled_jobs(KIND, **{"inputs.operation": operation})
            assert "backup-source-probe" not in enabled
    assert not evaluate(expression, **{"inputs.operation": "probe"})


@pytest.mark.parametrize(
    ("event", "actions", "operation", "options", "error"),
    [
        ("workflow_dispatch", "true", "probe", {"probe_kind": KIND}, None),
        ("workflow_dispatch", "false", "probe", {"probe_kind": KIND}, ValueError),
        ("schedule", "true", "probe", {"probe_kind": KIND}, ValueError),
        ("workflow_dispatch", "true", "daily", {"probe_kind": KIND}, SystemExit),
        ("workflow_dispatch", "true", "probe", {"probe_kind": "okooo-sfc"}, SystemExit),
        ("workflow_dispatch", "true", "probe", "not JSON", SystemExit),
        (
            "workflow_dispatch",
            "true",
            "probe",
            {"probe_kind": KIND, "url": "https://example.invalid/"},
            SystemExit,
        ),
    ],
)
def test_cli_checks_manual_dispatch_and_options_before_collecting(
    tmp_path, monkeypatch, event, actions, operation, options, error
):
    from zucai.cloud_backup_source_probe import main

    calls = []
    collector = ModuleType("zucai.backup_source_probe")
    collector.collect_backup_source_probe = lambda *, output: calls.append(output)
    monkeypatch.setitem(sys.modules, collector.__name__, collector)
    monkeypatch.setenv("GITHUB_ACTIONS", actions)
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setenv("ZUCAI_OPERATION", operation)
    monkeypatch.setenv(
        "DISPATCH_OPTIONS", options if isinstance(options, str) else json.dumps(options)
    )
    out = tmp_path / "probe evidence"
    if error is not None:
        with pytest.raises(error):
            main(["--out", str(out)])
        assert calls == []
    else:
        main(["--out", str(out)])
        assert calls == [out]
