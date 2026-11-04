"""Synthetic hash-listed coverage: repeat the snapshot without touching frozen inputs."""

import hashlib
import json
from pathlib import Path

import pytest

from test_daily_digest import NOW, snapshot
from test_daily_digest import digest_archive as digest_archive
from zucai.daily_digest import generate

FIXTURE = Path(__file__).parents[1] / "data/fixtures/t32/digest_coverage.json"


def install_coverage(archive, *, listed=True):
    name = "state/stats_coverage.json"
    target = archive / name
    target.write_bytes(FIXTURE.read_bytes())
    if listed:
        manifest = archive / "feed_manifest.json"
        feed = json.loads(manifest.read_bytes())
        feed["files"][name] = hashlib.sha256(target.read_bytes()).hexdigest()
        manifest.write_text(json.dumps(feed, ensure_ascii=False))
    return target


def test_coverage_is_only_appended_inside_anomalies_and_preserves_frozen_inputs(
    digest_archive, tmp_path
):
    baseline = generate(digest_archive, tmp_path / "baseline", now=NOW).read_text()
    install_coverage(digest_archive)
    before = snapshot(digest_archive)
    text = generate(
        digest_archive, tmp_path / "daily", now=NOW, repository="owner/repo"
    ).read_text()
    assert snapshot(digest_archive) == before
    anomaly = text.split("## 异常与采集记录", 1)[1]
    assert "### 技术数据覆盖率" in anomaly
    assert "| 联赛 / 赛季 / 端点 / 阶段 | 应取 | 已取 | 有效 | 空值 | 失败 | 未支持 |" in anomaly
    assert "| 英超 / 2025 / statistics / history | 380 | 379 | 371 | 8 | 1 | 0 |" in anomaly
    assert "| 荷甲 / 2022 / index / history | 1 | 1 | 1 | 0 | 0 | 0 |" in anomaly
    assert (
        "| &lt;测试&gt;\\|\\[队\\]&#64;x / 2024 / lineups / pre kickoff "
        "| 2 | 未记录 | 未记录 | 未记录 | 未记录 | 未记录 |" in anomaly
    )
    assert "观测 UTC：2026-11-03T09:49:59+00:00" in anomaly
    assert (
        "https://github.com/owner/repo/blob/data/football-feeds/state/stats_coverage.json"
        in anomaly
    )
    # Compare without repository links to isolate the new anomaly block exactly.
    relative = generate(digest_archive, tmp_path / "relative", now=NOW).read_text()
    start = relative.index("### 技术数据覆盖率")
    end = relative.index("仅转述冻结数据", start)
    assert relative[:start] + relative[end:] == baseline
    assert "381" not in anomaly  # requests are not used to recompute six frozen counts.


def test_unlisted_coverage_cannot_influence_digest(digest_archive, tmp_path):
    baseline = generate(digest_archive, tmp_path / "before", now=NOW).read_bytes()
    install_coverage(digest_archive, listed=False)
    assert generate(digest_archive, tmp_path / "after", now=NOW).read_bytes() == baseline


def test_coverage_hash_failure_preserves_existing_digest(digest_archive, tmp_path):
    coverage = install_coverage(digest_archive)
    out = tmp_path / "daily"
    generate(digest_archive, out, now=NOW)
    before = snapshot(out)
    coverage.write_text("{}")
    with pytest.raises(ValueError, match="云端数据校验失败"):
        generate(digest_archive, out, now=NOW)
    assert snapshot(out) == before
