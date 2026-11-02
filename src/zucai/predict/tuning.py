"""Season S selects half-life; the next eligible season measures the candidate."""

import math
from datetime import UTC, datetime
from pathlib import Path

from zucai.predict import parameters
from zucai.predict.backtest import evaluate
from zucai.predict.base import utc
from zucai.predict.parameters import TRAINING_POLICY, p2_gate, write_parameters
from zucai.predict.seasons import (
    latest_complete_seasons,
    next_season,
    parse_seasons,
    season_bounds,
    season_is_complete_eligible,
)


def rolling_origin(
    history, closing, *, grid, seasons=None, as_of, fold="monthly", draw_calibration=None
):
    if fold != "monthly":
        raise ValueError("网格搜索必须使用monthly；最终评估另用weekly")
    grid = sorted(set(float(value) for value in grid))
    if not grid or any(not math.isfinite(value) or value <= 0 for value in grid):
        raise ValueError("grid须为正数半衰期列表")
    league = history.league.iloc[0]
    end = utc(as_of)
    selected = parse_seasons(seasons, league)
    if selected is None:
        selected = latest_complete_seasons(end, league, count=3)
    windows = []
    for season in selected:
        if not season_is_complete_eligible(season, league):
            raise ValueError(f"{season} 过渡赛制不能作为完整选参季")
        low, origin = season_bounds(season, league)
        if origin >= end:
            raise ValueError("选参季须已结束且留有下一季验证窗口")
        validation_season = next_season(season, league)
        while not season_is_complete_eligible(validation_season, league):
            validation_season = next_season(validation_season, league)
        validation_start, validation_stop = season_bounds(validation_season, league)
        if validation_start >= end:
            raise ValueError(f"{validation_season} 验证季尚未开始，须指定晚于该季起点的截点")
        validation_end = min(end, validation_stop)
        # Physically isolate selection before any transition or later-season labels.
        corpus = history.loc[
            (history.kickoff_utc < origin) & (history.available_utc < origin)
        ].copy()
        candidates = []
        for days in grid:
            trial = evaluate(
                corpus,
                {},
                since=low,
                as_of=origin,
                seasons=[season],
                fold="monthly",
                half_life_days=days,
                draw_calibration=draw_calibration is True,
            )
            candidates.append(
                dict(
                    half_life_days=days,
                    model=trial["model"],
                    eligible=trial["in_period"] - trial["unavailable_labels"],
                    skipped=trial["skipped"],
                    folds=len(trial["folds"]),
                )
            )
        valid = [c for c in candidates if c["model"]["n"] > 0 and c["model"]["n"] == c["eligible"]]
        if not valid:
            raise ValueError(f"{season} 无完整评分的候选，拒绝按缺失样本选参")
        best = min(valid, key=lambda c: (c["model"]["log_loss"], c["half_life_days"]))
        # The switch is selected before S+1, never from validation results.
        calibrate = (
            abs(best["model"]["draw_bias"]) > 0.03 if draw_calibration is None else draw_calibration
        )
        validation = evaluate(
            history,
            {},
            closing=closing,
            since=validation_start,
            as_of=validation_end,
            seasons=[validation_season],
            fold=fold,
            half_life_days=best["half_life_days"],
            draw_calibration=calibrate,
        )
        windows.append(
            dict(
                selection_season=season,
                validation_season=validation_season,
                selection_as_of_utc=origin.isoformat(),
                half_life_days=best["half_life_days"],
                draw_calibration=calibrate,
                selection_draw_bias=best["model"]["draw_bias"],
                candidates=candidates,
                validation=validation,
            )
        )
    return dict(
        league=league,
        grid=grid,
        selection_fold="monthly",
        selection_basis=(
            "minimum season-S walk-forward model log loss; "
            "complete equal samples; tie: shorter half-life"
        ),
        calibration_policy=(
            "enable in S+1 only if selected uncalibrated monthly S draw bias exceeds 3pp"
            if draw_calibration is None
            else "explicit_always_on"
            if draw_calibration
            else "explicit_always_off"
        ),
        windows=windows,
        half_life_days=windows[-1]["half_life_days"],
    )


def tune(
    *,
    db=None,
    league,
    source="jc,fd",
    grid=(90, 180, 270, 365, 540, 730),
    seasons=None,
    as_of=None,
    params_file=None,
    draw_calibration=None,
):
    from zucai.predict.command import load_corpus

    if draw_calibration is not None and (
        params_file is None
        or parameters.parameters_path(params_file)
        in {
            parameters.parameters_path(),
            parameters.PARAMS_PATH.expanduser().resolve(),
            (Path.cwd() / "data/model/params.json").resolve(),
        }
    ):
        raise ValueError("诊断模式不能写入默认参数文件，请指定 --params-file")
    if (
        db is not None
        and params_file is not None
        and Path(db).resolve() == Path(params_file).resolve()
    ):
        raise ValueError("参数输出与数据库路径相同")
    cutoff = utc(as_of if as_of is not None else datetime.now(UTC))
    history, _market, closing, digest = load_corpus(db, league, source)
    result = rolling_origin(
        history,
        closing,
        grid=grid,
        seasons=seasons,
        as_of=cutoff,
        draw_calibration=draw_calibration,
    )
    result["database_sha256"] = digest
    result["as_of_utc"] = cutoff.isoformat()
    mapping = {w["validation_season"]: w["half_life_days"] for w in result["windows"]}
    calibration_mapping = {w["validation_season"]: w["draw_calibration"] for w in result["windows"]}
    weekly = evaluate(
        history,
        {},
        closing=closing,
        seasons=list(mapping),
        as_of=cutoff,
        fold="weekly",
        parameters_by_season=mapping,
        calibration_by_season=calibration_mapping,
    )
    result["weekly"] = weekly
    gate = p2_gate(weekly, league=league, as_of=cutoff)
    generated = datetime.now(UTC).isoformat()
    entry = dict(
        half_life_days=result["half_life_days"],
        selection_basis=result["selection_basis"],
        calibration_policy=result["calibration_policy"],
        selection_season=result["windows"][-1]["selection_season"],
        selection_as_of_utc=result["windows"][-1]["selection_as_of_utc"],
        evaluation_windows=[
            {
                k: w[k]
                for k in (
                    "selection_season",
                    "validation_season",
                    "selection_as_of_utc",
                    "half_life_days",
                    "draw_calibration",
                    "selection_draw_bias",
                )
            }
            for w in result["windows"]
        ],
        generated_utc=generated,
        database_sha256=digest,
        training_policy=TRAINING_POLICY,
        sources=history.attrs["sources"],
        unknown_team_prior="weakest_quartile_half_shrinkage",
        draw_calibration=result["windows"][-1]["draw_calibration"],
        p2_ready=gate["p2_ready"],
        p2_evidence=gate,
    )
    write_parameters(league, entry, params_file)
    result["parameters"] = entry
    return result
