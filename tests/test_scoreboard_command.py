"""Real feed restore and forecast verification, entirely from checked-in synthetic inputs."""

import hashlib
import json
from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest

from test_scoreboard_score import FIXTURES, NOW, samples
from zucai.cloud_feeds import export_feeds
from zucai.db import get_engine, init_db, session_scope
from zucai.forecast_archive import publish_forecasts
from zucai.models import Match, Result, Team
from zucai.report.render import write_report


@pytest.fixture
def scoreboard_archive(tmp_path, request):
    archive = tmp_path / "archive"
    db = tmp_path / "seed.db"
    engine = get_engine(db)
    init_db(engine)
    rows, results = samples()
    unique = {r["match_id"]: r for r in rows}
    with session_scope(engine) as session:
        for key, row in unique.items():
            home, away = Team(name=row["home"]), Team(name=row["away"])
            session.add_all([home, away])
            session.flush()
            session.add(
                Match(
                    match_id=key,
                    source="jc",
                    league=row["league"],
                    home_team_id=home.id,
                    away_team_id=away.id,
                    kickoff_utc=row["kickoff_utc"],
                    status="finished" if key in results else "scheduled",
                )
            )
            session.flush()
            if key in results:
                session.add(
                    Result(
                        **{
                            k: results[key][k]
                            for k in (
                                "match_id",
                                "home_goals",
                                "away_goals",
                                "available_utc",
                                "available_source",
                            )
                        }
                    )
                )
    engine.dispose()
    export_feeds(db, archive, date(2026, 11, 1), "seed")
    for batch in json.loads((FIXTURES / "forecasts.json").read_bytes()):
        run_id = batch["batch_id"]
        stage = tmp_path / "stage" / run_id
        model_path = f"models/{run_id}/测试联赛.pkl"
        train_path = f"models/{run_id}/测试联赛.train.json"
        (stage / model_path).parent.mkdir(parents=True)
        raw = ("opaque-model-" + run_id).encode()
        (stage / model_path).write_bytes(raw)
        (stage / train_path).write_text("{}")
        sha = hashlib.sha256(raw).hexdigest()
        batch["models"][0]["model_version"] = sha
        batch["models"][0].update(getattr(request, "param", {}))
        for row in batch["predictions"]:
            row["model_version"] = sha
            row.update(getattr(request, "param", {}))
        forecasts = {}
        for day in ("2026-11-01", "2026-11-02"):
            frozen = deepcopy(batch)
            if day == "2026-11-02":
                frozen["predictions"] = []
                frozen["batch_id"] += "-empty"
            directory = f"forecasts/{day}/{run_id}"
            write_report(frozen, stage / directory)
            forecasts[day] = {
                "path": directory,
                "batch_id": frozen["batch_id"],
                "generated_utc": frozen["generated_utc"],
            }
        league = {
            "status": "trained",
            "model_path": model_path,
            "training_json_path": train_path,
            "sha256": sha,
            "training_database_sha256": "b" * 64,
        }
        run = {
            "run_id": run_id,
            "generated_utc": batch["generated_utc"],
            "database_sha256": "b" * 64,
            "leagues": {"测试联赛": league},
            "forecasts": forecasts,
        }
        publish_forecasts(archive, stage, run)
    return archive


def files(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_archive_roundtrip_four_outputs_exact_disclaimer_idempotency_and_no_mutation(
    scoreboard_archive, monkeypatch, tmp_path
):
    from zucai.scoreboard.command import run

    archive = scoreboard_archive
    before = files(archive)
    step = tmp_path / "step.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(step))
    summary = run(archive, as_of=NOW)
    assert summary["probability"]["total"]["n"] == 4
    assert summary["gates"]["prospective_verified"] is False
    assert summary["gates"]["simulated_profit_evidence"] is False
    assert summary["bet_allowed"] is False
    assert summary["edge_rule"] == "model_probability * frozen_sp > threshold"
    board = archive / "scoreboard"
    assert set(p.name for p in board.iterdir()) == {
        "scored.jsonl",
        "ledger.jsonl",
        "summary.json",
        "scoreboard.md",
    }
    text = (board / "scoreboard.md").read_text()
    assert text.startswith("非购买建议 · BET 禁用 · 虚拟资金模拟 · 竞彩含抽水\n")
    assert "单关偏差" in text and "假设均可单关" in text
    assert "1.05" in text.split("## 附表")[0]
    assert "概率 × 冻结 SP > 1.05" in text and "预期收益超过 5%" in text
    assert "收益超过 105%" not in text
    assert "每周资金" in text and "10 桶校准" in text and "模型版本" in text and "按月" in text
    assert step.read_text().splitlines() == text.splitlines()[:60]
    scored, ledger = (board / "scored.jsonl").read_bytes(), (board / "ledger.jsonl").read_bytes()
    run(archive, as_of=NOW)
    assert (board / "scored.jsonl").read_bytes() == scored
    assert (board / "ledger.jsonl").read_bytes() == ledger
    assert all((archive / name).read_bytes() == raw for name, raw in before.items())


def test_empty_scores_still_show_zero_and_false_gates(scoreboard_archive):
    from datetime import UTC, datetime

    from zucai.scoreboard.command import run

    summary = run(scoreboard_archive, as_of=datetime(2026, 11, 1, 10, tzinfo=UTC), replay_days=7)
    assert summary["probability"]["total"]["n"] == 0
    assert summary["probability"]["leagues"]["测试联赛"]["n"] == 0
    assert summary["probability"]["total"]["model"]["log_loss"] is None
    assert not any(summary["gates"].values())


def test_corrupt_ledger_fails_before_replacing_any_other_file(scoreboard_archive):
    from zucai.scoreboard.command import run

    run(scoreboard_archive, as_of=NOW)
    ledger = scoreboard_archive / "scoreboard/ledger.jsonl"
    ledger.write_bytes(ledger.read_bytes() + b"{}\n")
    before = files(scoreboard_archive)
    with pytest.raises(ValueError, match="SHA256"):
        run(scoreboard_archive, as_of=NOW)
    assert files(scoreboard_archive) == before


def test_failed_commit_rolls_back_all_scoreboard_files(scoreboard_archive, monkeypatch):
    from zucai.scoreboard.command import run

    run(scoreboard_archive, as_of=NOW)
    before = files(scoreboard_archive)
    replace = Path.replace

    def fail_summary(source, target):
        if source.name == "summary.json":
            raise OSError("offline write failure")
        return replace(source, target)

    monkeypatch.setattr(Path, "replace", fail_summary)
    with pytest.raises(OSError, match="offline write"):
        run(scoreboard_archive, as_of=NOW)
    assert files(scoreboard_archive) == before
