"""Only offline frozen fixtures: collection success is not quote freshness."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from test_daily_digest import NOW, snapshot
from test_daily_digest import digest_archive as digest_archive
from zucai.daily_digest import generate
from zucai.scoreboard.score import instant
from zucai.sources.sporttery import SportteryCalculator

FIX = Path(__file__).parents[1] / "data/fixtures/t36_odds"
CASES = json.loads((FIX / "cases.json").read_bytes())
WARNING = "本次未取得新的有效赔率，以下使用历史快照，不代表当前可购买价格。"


def install_case(archive, case):
    """Prepare synthetic frozen input and its hashes; never mutate during generate."""
    feed_path = archive / "feed_manifest.json"
    feed = json.loads(feed_path.read_bytes())
    jc = (
        {}
        if case == "legacy"
        else {
            "status": "complete",
            "schedule_status": CASES[case].get("schedule_status", "complete"),
            "schedule_updated_utc": "2026-11-03T09:30:00Z",
            "schedule_matches": CASES[case].get("matches", 2 if CASES[case]["count"] else 0),
            "odds_snapshots": CASES[case]["count"],
            "run_id": CASES[case].get("run_id", "collection-1"),
        }
    )
    feed["collection"]["jc"] = jc
    feed_path.write_text(json.dumps(feed))
    name = "forecasts/2026-11-03/digest-late/forecast.json"
    path = archive / name
    batch = json.loads(path.read_bytes())
    base = batch["predictions"][0]
    rows = []
    for i, stamp in enumerate(CASES[case]["times"]):
        row = deepcopy(base)
        row.update(match_id=f"jc:odds-{i}", jc_num=f"周二00{i + 1}", home=f"样本主队{i}")
        row["market"] = {
            "prices": {"h": 2.1, "d": 3.2, "a": 3.4},
            "probabilities": {"h": 0.44, "d": 0.29, "a": 0.27},
            "source": "stored_had_snapshot",
        }
        if stamp is not None:
            row["market"]["snapshot_utc"] = stamp
        rows.append(row)
    batch["predictions"] = rows
    path.write_text(json.dumps(batch))
    manifest_path = path.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["files"]["forecast.json"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    index_path = archive / "forecast_manifest.json"
    index = json.loads(index_path.read_bytes())
    if case != "legacy":
        index["runs"]["digest-late"]["feed_snapshot"] = {
            "schedule_updated_utc": "2026-11-03T09:30:00Z",
        }
    for target in (path, manifest_path):
        index["files"][target.relative_to(archive).as_posix()] = hashlib.sha256(
            target.read_bytes()
        ).hexdigest()
    index_path.write_text(json.dumps(index))


@pytest.mark.parametrize("case", list(CASES))
def test_complete_digest_distinguishes_collection_and_each_frozen_quote(
    digest_archive, tmp_path, case
):
    install_case(digest_archive, case)
    before = snapshot(digest_archive)
    kwargs = dict(
        now=NOW,
        run_id="collection-1",
        jc_sync_run_id="collection",
        jc_sync_status="success",
        jc_publish_status="success",
    )
    text = generate(digest_archive, tmp_path / "daily", **kwargs).read_text()
    assert generate(digest_archive, tmp_path / "daily", **kwargs).read_text() == text
    assert snapshot(digest_archive) == before
    assert "冻结赔率采集时间（北京时间）" in text
    rows = [line for line in text.splitlines() if line.startswith("| 周二00")]
    assert len(rows) == 2
    for row, stamp in zip(rows, CASES[case]["times"], strict=True):
        if case in {"invalid", "legacy"} or stamp is None:
            assert "未知（冻结记录缺少有效赔率时间）" in row
            assert "17:30:00" not in row and "17:45:00" not in row
        else:
            from zucai.digest_display import BEIJING

            assert instant(stamp).astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S") in row
    if case == "partial":
        assert "历史快照" not in rows[0]
        assert "历史快照" in rows[1]
        assert "本批有场次沿用接口检查前的历史赔率" in text
    if case in {"closed", "deduplicated", "invalid"}:
        assert WARNING in text
        assert "新增入库有效赔率：0 条" in text
        assert "竞彩接口采集：成功" in text
    elif case in {"normal", "partial"}:
        assert WARNING not in text
        assert f"新增入库有效赔率：{CASES[case]['count']} 条" in text
        assert "不代表所有比赛均已更新" in text
        assert "竞彩接口采集：成功" in text
    else:
        assert WARNING not in text  # Missing evidence/failed initialization is not confirmed zero.
        assert "本次新增入库有效赔率：未知" in text
    assert text.startswith("非购买建议 · BET 禁用")


def test_closed_real_response_has_prices_but_no_eligible_sale_flags():
    raw = (FIX / "closed_real_trimmed.json").read_bytes()
    obj = json.loads(raw)
    rows = [row for group in obj["value"]["matchInfoList"] for row in group["subMatchList"]]
    assert len(rows) == 2
    assert all(row["matchStatus"] == "Selling" and row["sellStatus"] == 2 for row in rows)
    assert all(row["bettingSingle"] == row["bettingAllUp"] == 0 for row in rows)
    assert all(row["had"] for row in rows)
    source = SportteryCalculator()
    assert source.parse(raw, captured=instant("2026-11-06T15:26:05.030882Z")) == []
    assert len(source.catalog) == len(source.skipped) == 2
    assert {row["reason"] for row in source.skipped} == {"sell_status_not_1"}


@pytest.mark.parametrize("case", ["legacy", "invalid"])
def test_digest_only_compatibility_preserves_strict_readers_and_hash_checks(
    digest_archive, tmp_path, case
):
    from zucai.forecast_archive import verify_forecasts
    from zucai.report.render import load_batch, write_report

    install_case(digest_archive, case)
    path = digest_archive / "forecasts/2026-11-03/digest-late/forecast.json"
    with pytest.raises(ValueError, match="时间"):
        load_batch(path)
    with pytest.raises(ValueError, match="时间"):
        verify_forecasts(digest_archive)
    with pytest.raises(ValueError, match="时间"):
        write_report(json.loads(path.read_bytes()), tmp_path / "new-freeze")
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="SHA256"):
        generate(digest_archive, tmp_path / "daily", now=NOW)


def test_publication_failure_does_not_present_previous_positive_count_as_new(
    digest_archive, tmp_path
):
    install_case(digest_archive, "normal")
    text = generate(
        digest_archive,
        tmp_path / "daily",
        now=NOW,
        jc_sync_run_id="collection",
        jc_publish_status="failure",
    ).read_text()
    assert "竞彩数据发布失败" in text
    assert "本次新增入库有效赔率：未知" in text
    assert "本次新增入库有效赔率：6 条" not in text
    assert "归档最近一次新增入库有效赔率：6 条" in text


RUN_CONTEXTS = json.loads((FIX / "run_contexts.json").read_bytes())


@pytest.mark.parametrize("name", list(RUN_CONTEXTS))
def test_full_digest_with_checked_in_workflow_arguments(
    digest_archive, tmp_path, monkeypatch, name
):
    import shlex
    from string import Template

    from test_stats_workflow import jobs, step
    from zucai import daily_digest

    context = RUN_CONTEXTS[name]
    install_case(digest_archive, context["case"])
    path = digest_archive / "feed_manifest.json"
    feed = json.loads(path.read_bytes())
    feed["collection"]["jc"]["run_id"] = context["archived_run"]
    path.write_text(json.dumps(feed))
    before = snapshot(digest_archive)
    # Execute the real CLI argument wiring from the checked-in workflow, offline.
    render = step("Render daily digest from frozen archives", jobs()["digest"])
    assert "SPORTTERY_STATUS: ${{ needs.sporttery.result }}" in render
    assert "SPORTTERY_PUBLISH_STATUS: ${{ needs.sporttery.outputs.publish_status }}" in render
    command = render.split("        run: ", 1)[1].splitlines()[0]
    command = Template(command).substitute(
        GITHUB_RUN_ID=context["run"],
        GITHUB_RUN_ATTEMPT=context["attempt"],
        SPORTTERY_STATUS=context["sync_status"],
    )
    argv = shlex.split(command)[3:]
    argv[argv.index("--archive") + 1] = str(digest_archive)
    out = tmp_path / "daily"
    argv[argv.index("--out") + 1] = str(out)
    monkeypatch.setenv("SPORTTERY_PUBLISH_STATUS", context["publish_status"])
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    captured = {}

    def at_fixture_cutoff(*args, **kwargs):
        captured.update(kwargs)
        return generate(*args, **kwargs, now=NOW)

    monkeypatch.setattr(daily_digest, "generate", at_fixture_cutoff)
    assert daily_digest.main(argv) == 0
    assert captured["run_id"] == f"{context['run']}-{context['attempt']}"
    assert captured["jc_sync_run_id"] == context["run"]
    text = (out / "latest.md").read_text()
    header = text.split("## 今日与明日预测", 1)[0]
    if context["mode"] == "skipped":
        assert "本次未执行采集" in header
        assert "本次结果未确认发布" not in header
        assert "本次新增入库有效赔率：" not in header
    elif context["mode"] == "unknown":
        assert "本次新增入库有效赔率：未知" in header
        assert "本次新增入库有效赔率：6 条" not in header
        assert "本次结果未确认发布" in header
    else:
        assert "本次新增入库有效赔率：6 条" in header
        assert "归档最近一次新增入库有效赔率：" not in header
    if context["mode"] != "current":
        assert "归档最近一次竞彩接口采集：成功" in header
        assert f"归档记录 run {context['archived_run']}" in header
        assert "归档最近一次接口成功检查：2026-11-03 17:30:00（北京时间）" in header
        assert f"归档最近一次新增入库有效赔率：{CASES[context['case']]['count']} 条" in header
        assert WARNING not in header  # Historical zero must not be attributed to this attempt.
    assert daily_digest.main(argv) == 0
    assert (out / "latest.md").read_text() == text
    assert snapshot(digest_archive) == before
