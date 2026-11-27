"""Offline probe entry point and feed-archive behavior, without a provider payload."""

import json
import sys
from datetime import date
from pathlib import Path

import pytest

from test_stats_workflow import evaluate, jobs, step
from zucai import cloud_feeds
from zucai.db import get_engine, init_db


def test_probe_updates_only_evidence_and_manifest(tmp_path, monkeypatch):
    from zucai import cloud_team_country_probe

    database = tmp_path / "seed.db"
    engine = get_engine(database)
    try:
        init_db(engine)
    finally:
        engine.dispose()
    archive = tmp_path / "archive"
    previous = cloud_feeds.export_feeds(
        database,
        archive,
        date(2026, 11, 27),
        "earlier",
        collection={"jc": {"status": "complete"}},
    )
    before = {p.relative_to(archive): p.read_bytes() for p in archive.rglob("*") if p.is_file()}
    evidence = "state/catalogue/123-1/apif-team-countries/index.json"
    report = {"evidence_index": evidence, "status": "partial", "requests": 2}
    summaries = []

    def collect(target, run_id):
        assert not target.exists()
        assert run_id == "123-1"
        dest = target.parent / evidence
        dest.parent.mkdir(parents=True)
        dest.write_text(json.dumps(report), encoding="utf-8")
        return report

    monkeypatch.setattr(cloud_team_country_probe, "collect_team_country_probe", collect)
    monkeypatch.setattr(cloud_team_country_probe, "write_summary", summaries.append)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "github-hosted")
    monkeypatch.chdir(tmp_path)
    argv = [
        "cloud_feeds",
        "probe",
        "--probe-kind",
        "team-countries",
        "--archive",
        str(archive),
        "--run-id",
        "123-1",
    ]
    monkeypatch.setattr(sys, "argv", argv)
    cloud_feeds.main()
    assert summaries == [report]
    assert not (tmp_path / "data/zucai.db").exists()
    current = cloud_feeds.archive_info(archive)
    assert current.pop("files") == {
        **previous.pop("files"),
        evidence: cloud_feeds.digest(archive / evidence),
    }
    assert current == previous
    for path, raw in before.items():
        if path != Path(cloud_feeds.MANIFEST):
            assert (archive / path).read_bytes() == raw
    restored = tmp_path / "restored" / "zucai.db"
    cloud_feeds.restore_feeds(archive, restored)
    assert (restored.parent / evidence).read_bytes() == (archive / evidence).read_bytes()
    with pytest.raises(ValueError, match="已存在"):
        cloud_feeds.main()


def test_manual_probe_uses_existing_hosted_job_and_explicit_probe_kind():
    workflow_job = jobs()["apis"]
    probe = step("Probe API-Football permissions without importing matches", workflow_job)
    assert "runs-on: ubuntu-latest" in workflow_job
    assert "args=(--probe-kind team-countries)" in probe
    mode = {
        "env.FEED_MODE": "probe",
        "fromJSON(inputs.options || '{}').probe_kind": "team-countries",
    }
    assert evaluate(probe.split("${{", 1)[1].split("}}", 1)[0].strip(), **mode)
