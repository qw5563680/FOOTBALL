# ruff: noqa: F811

import json
from datetime import timedelta

import pytest

from test_experimental_scoreboard import scores
from test_scoreboard_command import scoreboard_archive  # noqa: F401
from test_scoreboard_score import NOW
from test_t72_team_market_scoreboard import add_blend
from zucai.predict.experiments.global_rating_contract import (
    RATING_NAME,
    RATING_RUN_KEY,
    RATING_VERSION,
)
from zucai.scoreboard import team_market_blend
from zucai.scoreboard.command import run
from zucai.scoreboard.experimental import (
    load_experiments,
    render_experiments,
    summarize_experiments,
)
from zucai.scoreboard.score import instant, score_forecasts


def test_rating_adoption_uses_one_all_leagues_group_and_keeps_existing_models():
    rows, results, experiments, _, _ = scores()
    rows[1]["league"] = results[rows[1]["match_id"]]["league"] = "意甲"
    rows[0]["metadata"] = dict(unknown_team_prior=True)
    missing = dict(rows[0], match_id="jc:missing")
    results[missing["match_id"]] = dict(results[rows[0]["match_id"]], match_id=missing["match_id"])
    unmarked = dict(rows[0], match_id="jc:old", batch_id="before-rating")
    results[unmarked["match_id"]] = dict(
        results[rows[0]["match_id"]], match_id=unmarked["match_id"]
    )
    formal, active = score_forecasts([*rows, missing, unmarked], results, [], NOW)
    current, _ = score_forecasts(experiments, results, [], NOW, experimental=True)
    baseline = summarize_experiments(experiments, current, formal, active["active_keys"], NOW)
    elo = [
        dict(
            row,
            model_name=RATING_NAME,
            model_version=RATING_VERSION,
            market=None,
            model_probabilities=dict(h=0.61, d=0.25, a=0.14),
            validation_status="experimental_unadopted",
        )
        for row in rows
    ]
    later = dict(
        elo[0],
        batch_id="experiment-later",
        generated_utc=(instant(elo[0]["generated_utc"]) + timedelta(minutes=1)).isoformat(),
    )
    late = dict(elo[0], match_id=missing["match_id"], published_utc=missing["kickoff_utc"])
    scored, _ = score_forecasts(
        [*experiments, *elo, later, late], results, [], NOW, experimental=True
    )
    report = summarize_experiments(
        [*experiments, *elo, later, late],
        scored,
        formal,
        active["active_keys"],
        NOW,
        rating_batches={rows[0]["batch_id"]},
    )
    assert report["groups"][:-1] == baseline["groups"]
    group = report["groups"][-1]
    assert group["model_name"] == RATING_NAME and group["league"] == "所有联赛"
    assert (group["production_n"], group["common_n"], group["relative_coverage"]) == (3, 2, 2 / 3)
    assert {(league, data["common_n"]) for league, data in group["by_league"].items()} == {
        ("英超", 1),
        ("意甲", 1),
    }
    assert group["default_strength"]["common_n"] == 1
    assert group["adoption"]["automatic_adoption"] is False
    assert group["half_average"]["common_n"] == 2
    assert "各半平均" not in render_experiments(report)
    assert "各半平均（对照，不是一条实验线）" in render_experiments(report, include_comparison=True)


def test_enabled_missing_attachment_still_has_formal_denominator():
    rows, _, experiments, formal, active = scores()
    report = summarize_experiments(
        experiments, [], formal, active["active_keys"], NOW, rating_batches={rows[0]["batch_id"]}
    )
    group = report["groups"][-1]
    assert group["model_name"] == RATING_NAME
    assert group["production_n"] == 2 and group["common_n"] == 0


@pytest.mark.parametrize("problem", ["cutoff", "probability", "duplicate", "identity"])
def test_invalid_rating_row_does_not_erase_valid_technical_attachment(tmp_path, problem):
    rows, _, technical, _, _ = scores()
    original = rows[0]
    cutoff = original["as_of_utc"]
    directory = tmp_path / "batch"
    directory.mkdir()
    (directory / "forecast.json").write_text(
        json.dumps(
            dict(
                predictions=[original],
                models=[
                    dict(
                        model_name="dixon-coles",
                        model_version=original["model_version"],
                        model_as_of_utc=cutoff,
                    )
                ],
            )
        )
    )
    rating = dict(
        technical[0],
        model_name=RATING_NAME,
        model_version=RATING_VERSION,
        model_type="TEAM",
        p2_label="P2 实验",
        p2_ready=False,
        paper_eligible=False,
        bet_allowed=False,
        training_availability_mode="backfill_replay",
        target_availability_mode="strict_capture_time",
        availability_mode="strict",
        training_cutoff_utc=cutoff,
        feature_cutoff=cutoff,
    )
    if problem == "cutoff":
        rating["feature_cutoff"] = (instant(cutoff) + timedelta(minutes=1)).isoformat()
    elif problem == "probability":
        rating["model_probabilities"] = dict(h=0.6, d=0.6, a=0.9)
    elif problem == "identity":
        rating["home"] = "错误主队"
    old = dict(technical[0], availability_mode="strict")
    rating_rows = [rating, dict(rating)] if problem == "duplicate" else [rating]
    path = "batch/experimental.json"
    (tmp_path / path).write_text(
        json.dumps(
            dict(
                schema_version=1,
                availability_mode="strict",
                mode="prematch",
                batch_id="late",
                generated_utc=original["generated_utc"],
                as_of_utc=cutoff,
                predictions=[old, *rating_rows],
            )
        )
    )
    manifest = dict(
        files={path: "b" * 64},
        runs={
            "late": dict(
                training_cutoff_utc=cutoff,
                **{RATING_RUN_KEY: RATING_VERSION},
                forecasts={"day": dict(path="batch", experimental_path=path, batch_id="late")},
            )
        },
    )
    loaded, audit = load_experiments(
        tmp_path,
        manifest,
        NOW,
        publications={
            path: {"published_utc": original["published_utc"]},
        },
    )
    assert [row["model_name"] for row in loaded] == ["dc-sot-v1"]
    assert len(audit) == len(rating_rows)
    assert all(row["reason"] == "invalid_rating_row" for row in audit)


@pytest.mark.parametrize("count,rating_h", [(200, 0.8), (600, 0.3)])
def test_combined_gate_and_failure_checkpoints_remain_owner_decisions(count, rating_h):
    rows, results, _, _, _ = scores()
    sample = rows[0]
    fixture = results[sample["match_id"]]
    forecasts, observed = [], {}
    for i in range(count):
        kickoff = NOW + timedelta(days=i // 100 * 7 + 1, seconds=i)
        scheduled = dict(
            sample,
            match_id=f"jc:generated-{i}",
            league="英超" if i % 2 else "意甲",
            kickoff_utc=kickoff.isoformat(),
            as_of_utc=(kickoff - timedelta(hours=2)).isoformat(),
            generated_utc=(kickoff - timedelta(hours=1)).isoformat(),
            published_utc=(kickoff - timedelta(minutes=30)).isoformat(),
            model_probabilities=dict(h=0.5, d=0.25, a=0.25),
            market=None,
        )
        forecasts.append(scheduled)
        observed[scheduled["match_id"]] = dict(
            fixture,
            match_id=scheduled["match_id"],
            league=scheduled["league"],
            kickoff_utc=scheduled["kickoff_utc"],
            available_utc=(kickoff + timedelta(hours=2)).isoformat(),
            home_goals=1,
            away_goals=0,
        )
    elo = [
        dict(
            row,
            model_name=RATING_NAME,
            model_version=RATING_VERSION,
            model_probabilities=dict(h=rating_h, d=(1 - rating_h) / 2, a=(1 - rating_h) / 2),
        )
        for row in forecasts
    ]
    at = NOW + timedelta(days=50)
    formal, active = score_forecasts(forecasts, observed, [], at)
    group = summarize_experiments(
        elo, [], formal, active["active_keys"], at, rating_batches={sample["batch_id"]}
    )["groups"][-1]
    assert group["common_n"] == count
    assert group["adoption"]["automatic_adoption"] is False
    if count == 200:
        assert not group["adoption"]["unmet"]
        assert group["adoption"]["status"] == "达到采用门槛，待所有者决策"
        assert {entry["common_n"] for entry in group["by_league"].values()} == {100}
        assert not group["checkpoints"]
    else:
        assert not group["adoption"]["checks"]["log_loss"]
        assert group["checkpoints"]["300"]["log_loss"]["interval"][0] > 0
        assert group["checkpoints"]["600"]["half_average_log_loss"]["interval"][1] >= 0
        assert all(point["failure_condition"] is True for point in group["checkpoints"].values())


def test_rating_summary_is_scoreboard_only_and_preserves_formal_and_ledger(scoreboard_archive):
    from forecast_publication_support import confirm_fixture
    from zucai.forecast_archive import attach_experiments, verify_forecasts

    archive = scoreboard_archive
    add_blend(archive, market_weight=0.5)
    first = run(archive, as_of=NOW)
    ledger = (archive / "scoreboard/ledger.jsonl").read_bytes()
    original_scores = (archive / "scoreboard/scored.jsonl").read_bytes()
    blend_scores = (archive / team_market_blend.SCORES).read_bytes()
    manifest = verify_forecasts(archive)
    marker = archive / "forecast_manifest.json"
    frozen = {}
    for info in manifest["runs"].values():
        for forecast in info["forecasts"].values():
            path = archive / forecast["path"] / "forecast.json"
            frozen[path] = path.read_bytes()
            batch = json.loads(frozen[path])
            if batch["models"]:
                info["training_cutoff_utc"] = batch["models"][0]["model_as_of_utc"]
        info["global_rating_version"] = RATING_VERSION
    marker.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    for run_id, info in manifest["runs"].items():
        cutoff = info["training_cutoff_utc"]
        documents = {}
        for day, forecast in info["forecasts"].items():
            batch = json.loads(frozen[archive / forecast["path"] / "forecast.json"])
            documents[day] = dict(
                schema_version=1,
                batch_id=batch["batch_id"],
                as_of_utc=cutoff,
                generated_utc=batch["generated_utc"],
                mode="prematch",
                availability_mode="strict",
                predictions=[
                    dict(
                        {
                            key: row[key]
                            for key in (
                                "match_id",
                                "source",
                                "league",
                                "home",
                                "away",
                                "kickoff_utc",
                                "status",
                            )
                        },
                        model_name=RATING_NAME,
                        model_version=RATING_VERSION,
                        model_type="TEAM",
                        p2_ready=False,
                        p2_label="P2 实验",
                        bet_allowed=False,
                        paper_eligible=False,
                        validation_status="experimental_unadopted",
                        availability_mode="strict",
                        training_availability_mode="backfill_replay",
                        target_availability_mode="strict_capture_time",
                        model_probabilities=row["model_probabilities"],
                        fallback_reason=None,
                        training_cutoff_utc=cutoff,
                        feature_cutoff=cutoff,
                    )
                    for row in batch["predictions"]
                    if row["status"] == "predicted"
                ],
            )
        attach_experiments(archive, documents, run_id)
    confirm_fixture(archive)
    second = run(archive, as_of=NOW)
    assert first["probability"] == second["probability"]
    assert first["team_market_blend"]["common_n"] == 3
    assert first["team_market_blend"] == second["team_market_blend"]
    assert (archive / team_market_blend.SCORES).read_bytes() == blend_scores
    assert first["accounts"] == second["accounts"]
    assert (archive / "scoreboard/ledger.jsonl").read_bytes() == ledger
    scored = (archive / "scoreboard/scored.jsonl").read_bytes()
    assert scored.startswith(original_scores) and b"elo-global-v1" in scored
    assert "各半平均" in (archive / "scoreboard/scoreboard.md").read_text()
    assert all(path.read_bytes() == before for path, before in frozen.items())
    for path in archive.glob("forecasts/*/*/experimental.json"):
        document = json.loads(path.read_bytes())
        assert {row["model_name"] for row in document["predictions"]} <= {RATING_NAME}
    rows = [json.loads(line) for line in scored.splitlines()]
    assert {row["model_name"] for row in rows} <= {"dixon-coles", RATING_NAME}
