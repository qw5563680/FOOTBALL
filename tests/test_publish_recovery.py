"""Publication failures and recovery use only frozen offline fixtures and local Git."""

import json
import shutil
from pathlib import Path

from test_cloud_predict import NOW
from test_cloud_predict import scheduled_archive as scheduled_archive
from test_daily_digest import NOW as DIGEST_NOW
from test_daily_digest import digest_archive as digest_archive
from test_daily_digest import snapshot
from test_publish_script import ERRORS, execute, git
from test_publish_script import publication as publication
from test_stats_workflow import jobs, step
from zucai import cloud_predict
from zucai.cloud_feeds import restore_feeds
from zucai.daily_digest import generate
from zucai.forecast_archive import verify_forecasts


def test_forecast_records_published_input_and_publication_failure(scheduled_archive, monkeypatch):
    archive, params = scheduled_archive
    monkeypatch.setattr(cloud_predict, "_now", lambda: NOW)
    feed = json.loads((archive / "feed_manifest.json").read_bytes())
    before = snapshot(archive)
    result = cloud_predict.run(
        archive,
        params,
        "publish-failed",
        sporttery_status="failure",
        sporttery_publish_status="failure",
        feed_commit="a" * 40,
    )
    assert result["sporttery_stale"] is True
    assert result["sporttery_publish_status"] == "failure"
    assert result["feed_snapshot"] == {
        "commit": "a" * 40,
        "run_id": feed["run"],
        "created_utc": feed["created_utc"],
        "schedule_updated_utc": None,
        "results_updated_utc": None,
    }
    assert (
        verify_forecasts(archive)["runs"]["publish-failed"]["feed_snapshot"]
        == result["feed_snapshot"]
    )
    assert all((archive / path).read_bytes() == raw for path, raw in before.items())


def test_complete_digest_distinguishes_failed_publication_and_forecast_input(
    digest_archive, tmp_path
):
    manifest_path = digest_archive / "forecast_manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    run = manifest["runs"]["digest-late"]
    run["sporttery_publish_status"] = "failure"
    run["feed_snapshot"] = {
        "commit": "a" * 40,
        "run_id": "previous-collection",
        "created_utc": "2026-11-03T01:00:00Z",
        "schedule_updated_utc": "2026-11-03T00:50:00Z",
        "results_updated_utc": None,
    }
    manifest_path.write_text(json.dumps(manifest))
    before = snapshot(digest_archive)
    out = tmp_path / "digest"
    kwargs = dict(
        now=DIGEST_NOW,
        run_id="failed-publish",
        jc_sync_status="failure",
        jc_sync_run_id="failed-publish",
        jc_publish_status="failure",
    )
    text = generate(digest_archive, out, **kwargs).read_text()
    assert "竞彩数据发布失败" in text and "本次采集结果未确认发布" in text
    assert "失败阶段未记录" not in text
    assert "预测所用数据" in text and "a" * 40 in text
    assert "2026-11-03 09:00:00" in text  # Archive time, not prediction generation time.
    assert "2026-11-03 08:50:00" in text  # Genuine schedule watermark, kept separate.
    assert "旧归档未记录成功时刻" in text
    assert "previous-collection" in text
    outputs = snapshot(out)
    assert generate(digest_archive, out, **kwargs).read_text() == text
    assert snapshot(out) == outputs and snapshot(digest_archive) == before
    # A later digest-only run must still explain this frozen prediction's failed publication.
    later = generate(digest_archive, tmp_path / "later", now=DIGEST_NOW).read_text()
    assert "本批预测前竞彩数据发布失败" in later and "a" * 40 in later


def test_workflow_retains_bundle_and_transmits_publish_outcome():
    workflow = jobs()
    sporttery = workflow["sporttery"]
    assert "publish_status: ${{ steps.publish_jc.outcome }}" in sporttery
    publish = step("Publish verified data", sporttery)
    assert "id: publish_jc" in publish
    assert "PUBLISH_RECOVERY_DIR" in publish
    recovery = step("Keep committed collection recovery bundle", sporttery)
    assert "if: ${{ always() }}" in recovery
    assert "publication-recovery" in recovery and "retention-days: 90" in recovery
    for job in ("predict", "digest"):
        assert (
            "SPORTTERY_PUBLISH_STATUS: ${{ needs.sporttery.outputs.publish_status }}"
            in workflow[job]
        )
    assert "git -C ../cloud-data rev-parse HEAD" in workflow["predict"]


def test_recovered_collection_restores_and_preserves_later_ledger(
    scheduled_archive, publication, tmp_path
):
    archive, _ = scheduled_archive
    remote, other, run, _, env = publication
    # Put the real fixture export in the outgoing commit, in place of shell placeholders.
    for path in (run / "state", run / "matches"):
        shutil.rmtree(path)
    shutil.copytree(archive, run, dirs_exist_ok=True)
    recovery = tmp_path / "recovery"
    env.update(
        TEST_FETCH_FAILURES="9",
        TEST_FETCH_MESSAGE=ERRORS["transient"][-1],
        PUBLISH_RECOVERY_DIR=str(recovery),
    )
    assert execute(publication).returncode != 0
    collected = git(run, "rev-parse", "HEAD")
    expected = snapshot(archive)
    # Downstream scoring published a ledger while collection was offline.
    ledger = other / "scoreboard/ledger.jsonl"
    ledger.parent.mkdir()
    shutil.copyfile(Path(__file__).parents[1] / "data/fixtures/t25/ledger.jsonl", ledger)
    ledger_bytes = ledger.read_bytes()
    git(other, "add", ".")
    git(other, "commit", "-m", "downstream ledger")
    (remote / "hooks/pre-receive").unlink()
    git(other, "push", "origin", "HEAD")
    shutil.rmtree(run)
    git(tmp_path, "clone", "--branch", "data/football-feeds", str(remote), str(run))
    git(run, "config", "user.name", "Offline Test")
    git(run, "config", "user.email", "test@example.invalid")
    git(run, "fetch", str(recovery / "unpublished.bundle"), "HEAD")
    git(run, "cherry-pick", collected)
    env["TEST_FETCH_FAILURES"] = "0"
    assert execute(publication).returncode == 0
    restored = tmp_path / "restored.db"
    verified = restore_feeds(run, restored)
    assert verified["counts"] == json.loads((archive / "feed_manifest.json").read_bytes())["counts"]
    published = git(remote, "rev-parse", "data/football-feeds")
    assert execute(publication).returncode == 0
    assert git(remote, "rev-parse", "data/football-feeds") == published
    for path, raw in expected.items():
        assert (run / path).read_bytes() == raw
    assert (run / "scoreboard/ledger.jsonl").read_bytes() == ledger_bytes
