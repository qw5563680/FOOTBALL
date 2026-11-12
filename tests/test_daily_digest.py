"""Synthetic frozen archive only: rendering must never rescore or train."""

import hashlib
import json
from copy import deepcopy
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from zucai.cloud_feeds import export_feeds
from zucai.db import get_engine, init_db
from zucai.forecast_archive import publish_forecasts
from zucai.report.render import write_report

FIXTURES = Path(__file__).parents[1] / "data/fixtures/t25"
NOW = datetime(2026, 11, 3, 10, tzinfo=UTC)


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def install_scoreboard(archive, fixture="summary.json", *, scored_fixture="scored.jsonl"):
    board = archive / "scoreboard"
    board.mkdir(exist_ok=True)
    summary = json.loads((FIXTURES / fixture).read_bytes())
    for name in ("scored.jsonl", "ledger.jsonl"):
        source = scored_fixture if name == "scored.jsonl" else name
        (board / name).write_bytes((FIXTURES / source).read_bytes())
    (board / "scoreboard.md").write_text("非购买建议 · BET 禁用\n离线合成记分板。\n")
    summary["files"] = {
        name: hashlib.sha256((board / name).read_bytes()).hexdigest()
        for name in ("scored.jsonl", "ledger.jsonl", "scoreboard.md")
    }
    (board / "summary.json").write_text(json.dumps(summary, ensure_ascii=False))


def add_forecasts(archive, stage, run_id, generated, *, empty=False):
    batch = json.loads((FIXTURES / "forecast.json").read_bytes())
    batch["generated_utc"] = batch["as_of_utc"] = generated
    model_path = f"models/{run_id}/测试联赛.pkl"
    train_path = f"models/{run_id}/测试联赛.train.json"
    (stage / model_path).parent.mkdir(parents=True)
    (stage / model_path).write_bytes(b"t25-offline-model")
    (stage / train_path).write_text("{}")
    forecasts = {}
    for day in ("2026-11-03", "2026-11-04"):
        frozen = deepcopy(batch)
        frozen["batch_id"] = f"{run_id}-{day}"
        frozen["input"]["kickoff_date_beijing"] = day
        if empty or day == "2026-11-04":
            frozen["predictions"] = []
        directory = f"forecasts/{day}/{run_id}"
        write_report(frozen, stage / directory)
        forecasts[day] = {
            "path": directory,
            "batch_id": frozen["batch_id"],
            "generated_utc": generated,
        }
    publish_forecasts(
        archive,
        stage,
        {
            "run_id": run_id,
            "generated_utc": generated,
            "database_sha256": "b" * 64,
            "leagues": {
                "测试联赛": {
                    "status": "trained",
                    "model_path": model_path,
                    "training_json_path": train_path,
                    "sha256": batch["models"][0]["model_version"],
                    "training_database_sha256": "b" * 64,
                }
            },
            "forecasts": forecasts,
        },
    )

    from forecast_publication_support import confirm_fixture

    confirm_fixture(archive)


@pytest.fixture
def digest_archive(tmp_path, monkeypatch):
    # These are presentation-only score/ledger fixtures with no source forecast.
    # Publication selection is exercised end to end in test_forecast_publication;
    # keep this fixture focused on verbatim rendering of supplied eligible evidence.
    from zucai.scoreboard import paper, score

    monkeypatch.setattr(
        score, "published_score_keys", lambda rows, *_: {r["record_id"] for r in rows}
    )
    monkeypatch.setattr(
        paper,
        "publication_view",
        lambda _f, _r, ledger, _at: (ledger, {"reselected_in_memory": False}),
    )
    archive = tmp_path / "archive"
    database = tmp_path / "empty.db"
    engine = get_engine(database)
    init_db(engine)
    engine.dispose()
    collection = json.loads((FIXTURES / "collection.json").read_bytes())
    export_feeds(database, archive, date(2026, 11, 3), "fixture", collection=collection)
    # Freeze wall-clock-independent feed metadata as part of the synthetic fixture.
    path = archive / "feed_manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest["created_utc"] = "2026-11-03T09:40:00+00:00"
    path.write_text(json.dumps(manifest, ensure_ascii=False))
    add_forecasts(archive, tmp_path / "stage", "digest-late", "2026-11-03T09:45:00+00:00")
    install_scoreboard(archive)
    return archive


@pytest.mark.parametrize("scenario", ["partial", "gates", "empty"])
def test_expected_markdown_and_inputs_unchanged(digest_archive, tmp_path, scenario):
    from zucai.daily_digest import generate

    archive = digest_archive
    if scenario == "gates":
        install_scoreboard(archive, "summary_gates.json")
    elif scenario == "empty":
        add_forecasts(
            archive, tmp_path / "empty-stage", "empty", "2026-11-03T09:55:00+00:00", empty=True
        )
    before = snapshot(archive)
    output = tmp_path / "daily"
    path = generate(archive, output, now=NOW, run_id="offline-1")
    assert path.name == "2026-11-03.md"
    assert path.read_text() == (FIXTURES / f"expected_{scenario}.md").read_text()
    assert (output / "latest.md").read_bytes() == path.read_bytes()
    assert snapshot(archive) == before
    assert path.read_text().splitlines()[0] == "非购买建议 · BET 禁用"
    assert "建议购买" not in path.read_text() and "推荐" not in path.read_text()
    assert "由当日 1 场逐场评分平均，非记分板原字段" in path.read_text()


@pytest.mark.parametrize("has_yesterday", [True, False])
def test_daily_mean_uses_only_frozen_active_yesterday_values(
    digest_archive, tmp_path, has_yesterday
):
    from zucai.daily_digest import generate

    # Scores intentionally differ from probabilities: recomputing the loss must fail this test.
    rows = [json.loads(line) for line in (FIXTURES / "scored_daily.jsonl").read_text().splitlines()]
    install_scoreboard(digest_archive, scored_fixture="scored_daily.jsonl")
    summary_path = digest_archive / "scoreboard/summary.json"
    summary = json.loads(summary_path.read_bytes())
    summary["audit"]["active_keys"] = [row["record_id"] for row in rows[:4]]
    if not has_yesterday:
        summary["audit"]["active_keys"] = [row["record_id"] for row in rows[2:4]]
    summary_path.write_text(json.dumps(summary))
    before = snapshot(digest_archive)
    text = generate(digest_archive, tmp_path / "daily", now=NOW).read_text()
    assert snapshot(digest_archive) == before
    assert "累计样本：37；累计 LogLoss：0.9786；累计 Brier：0.6123" in text
    if has_yesterday:
        for key, label in (("log_loss", "LogLoss"), ("brier", "Brier")):
            expected = sum(row["model"][key] for row in rows[:2]) / 2
            assert f"当日 {label}：{expected:.4f}" in text
            for row in rows[:2]:
                assert f"{row['model'][key]}" in text  # Preserve the unrounded individual values.
        assert "由当日 2 场逐场评分平均，非记分板原字段" in text
        assert "daily-first" in text and "daily-last" in text
    else:
        assert "昨日无已结算场次" in text
        assert "当日 LogLoss：" not in text and "当日 Brier：" not in text
    assert not any(row["record_id"] in text for row in rows[2:])


def test_corruption_fails_before_replacing_digest(digest_archive, tmp_path):
    from zucai.daily_digest import generate

    output = tmp_path / "daily"
    path = generate(digest_archive, output, now=NOW)
    before = snapshot(output)
    ledger = digest_archive / "scoreboard/ledger.jsonl"
    ledger.write_bytes(ledger.read_bytes() + b"{}\n")
    with pytest.raises(ValueError, match="SHA256"):
        generate(digest_archive, output, now=NOW)
    assert path.exists() and snapshot(output) == before


def test_output_cannot_overwrite_archive_inputs(digest_archive):
    from zucai.daily_digest import generate

    with pytest.raises(ValueError, match="daily"):
        generate(digest_archive, digest_archive / "scoreboard", now=NOW)


def test_unknown_collection_is_not_complete(digest_archive, tmp_path):
    from zucai.daily_digest import generate

    path = digest_archive / "feed_manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest["collection"] = {}
    manifest["collection_status"] = "complete"
    path.write_text(json.dumps(manifest))
    output = generate(digest_archive, tmp_path / "daily", now=NOW).read_text()
    assert "jc=未记录 / fd=未记录 / fdorg=未记录 / apif=未记录" in output
    assert "配额剩余：未记录" in output


def test_missing_archives_are_explicit_not_zero(digest_archive, tmp_path):
    import shutil

    from zucai.daily_digest import generate

    for directory in ("forecasts", "models", "scoreboard"):
        shutil.rmtree(digest_archive / directory)
    (digest_archive / "forecast_manifest.json").unlink()
    output = generate(digest_archive, tmp_path / "daily", now=NOW).read_text()
    assert "尚无冻结预测" in output
    assert "尚无记分板" in output
    assert "累计样本：未提供" in output


def test_newer_future_batch_does_not_replace_current_day(digest_archive, tmp_path):
    from zucai.daily_digest import generate

    add_forecasts(
        digest_archive, tmp_path / "future", "future", "2026-11-03T10:05:00+00:00", empty=True
    )
    text = generate(digest_archive, tmp_path / "daily", now=NOW).read_text()
    assert "digest-late-2026-11-03" in text and "future-2026-11-03" not in text
    assert "1:1 6.00%" in text and "1:2 6.00%" not in text  # Exactly frozen first five.


def test_cli_read_only_and_absolute_links(digest_archive, monkeypatch):
    from zucai.daily_digest import main

    before = snapshot(digest_archive)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert (
        main(
            [
                "--archive",
                str(digest_archive),
                "--out",
                str(digest_archive / "daily"),
                "--repository",
                "qw5563680/FOOTBALL",
                "--run-id",
                "offline-cli",
            ]
        )
        == 0
    )
    text = (digest_archive / "daily/latest.md").read_text()
    assert (
        "https://github.com/qw5563680/FOOTBALL/blob/data/football-feeds/scoreboard/summary.json"
        in text
    )
    assert all((digest_archive / name).read_bytes() == raw for name, raw in before.items())


def test_failed_second_write_restores_both_files(digest_archive, tmp_path, monkeypatch):
    import zucai.daily_digest as module

    out = tmp_path / "daily"
    module.generate(digest_archive, out, now=NOW, run_id="first")
    before = snapshot(out)
    write = module._atomic_write
    calls = 0

    def fail_second(path, content):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("offline disk failure")
        write(path, content)

    monkeypatch.setattr(module, "_atomic_write", fail_second)
    with pytest.raises(OSError, match="disk failure"):
        module.generate(digest_archive, out, now=NOW, run_id="second")
    assert snapshot(out) == before


def test_symlink_output_and_corrupt_forecast_refused(digest_archive, tmp_path):
    from zucai.daily_digest import generate

    out = tmp_path / "alias"
    out.symlink_to(digest_archive / "scoreboard", target_is_directory=True)
    with pytest.raises(ValueError, match="符号链接"):
        generate(digest_archive, out, now=NOW)
    forecast = next((digest_archive / "forecasts").rglob("forecast.json"))
    forecast.write_bytes(forecast.read_bytes() + b" ")
    with pytest.raises(ValueError, match="SHA256"):
        generate(digest_archive, tmp_path / "daily", now=NOW)
    assert not (tmp_path / "daily").exists()


def test_started_fixture_is_excluded_not_labelled_missing_model():
    from zucai.daily_digest import _forecast_sections

    batch = json.loads((FIXTURES / "forecast.json").read_bytes())
    batch["predictions"] = [batch["predictions"][1]]
    batch["predictions"][0]["reason"] = "预测截点时已开赛"
    text = "\n".join(
        _forecast_sections([("2026-11-03", "forecast.json", batch)], date(2026, 11, 3), None)
    )
    assert "未预测：预测截点时已开赛" in text
    assert "无模型：预测截点时已开赛" not in text
