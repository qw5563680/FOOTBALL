"""Parallel-only scoring and fixed adoption gates; never supplies the paper ledger."""

import json
import math
from collections import Counter

from zucai.forecast_publication import publication_times
from zucai.predict.experiments.global_rating_contract import (
    RATING_NAME,
    RATING_RUN_KEY,
    RATING_VERSION,
)
from zucai.predict.experiments.metrics import paired, summarize
from zucai.predict.experiments.parallel import NAMES, specifications
from zucai.prediction_disclosure import uses_unknown_team_prior
from zucai.scoreboard.score import instant, prematch_rows, record_key
from zucai.sources.sporttery import BEIJING

RULES = dict(
    version="t34a-adoption-v1",
    minimum_common_n=200,
    minimum_relative_coverage=0.90,
    log_loss_difference="experiment_minus_production < 0",
    bootstrap_upper="95% upper < 0",
    bootstrap=dict(unit="utc_week", replicates=2000, seed=3301),
    calibration=(
        "10 equal-width bins pooled over H/D/A; "
        "sum(n_bin*abs(mean_p-observed))/sum(n_bin); "
        "experiment <= production on common sample"
    ),
    coverage=(
        "same-batch valid experiment / scored production since protocol activation "
        "in the supported league; late/missing attachments stay in denominator"
    ),
    action="达到采用门槛，待所有者决策",
    automatic_adoption=False,
)


def _invalid_json_constant(value):
    raise ValueError(f"实验 JSON 含非法数值：{value}")


def _verify_rating_row(row, counterpart, cutoff, run, training_cutoffs):
    if (
        run.get(RATING_RUN_KEY) != RATING_VERSION
        or row.get("model_version") != RATING_VERSION
        or row.get("p2_label") != "P2 实验"
        or row.get("model_type") != "TEAM"
        or row.get("bet_allowed") is not False
        or row.get("paper_eligible") is not False
        or row.get("training_availability_mode") != "backfill_replay"
        or row.get("target_availability_mode") != "strict_capture_time"
        or counterpart.get("status") != "predicted"
        or counterpart.get("model_name") != "dixon-coles"
        or any(
            instant(value) != cutoff
            for value in (
                run.get("training_cutoff_utc"),
                row.get("training_cutoff_utc"),
                row.get("feature_cutoff"),
            )
        )
    ):
        raise ValueError("跨联赛评分须与同批正式预测使用同一训练截点和实验状态")
    if instant(training_cutoffs.get(counterpart.get("model_version"))) != cutoff and not (
        row["status"] == "missing" and row.get("fallback_reason") == "primary_cutoff_mismatch"
    ):
        raise ValueError("跨联赛评分与正式模型的训练截点不一致")


def _validate_experiment_row(row, identities, seen):
    if (
        not isinstance(row, dict)
        or row.get("model_name") not in NAMES
        or not isinstance(row.get("model_version"), str)
        or not row["model_version"]
        or row.get("status") not in ("predicted", "missing", "excluded")
        or not isinstance(row.get("p2_label", "P2 实验"), str)
        or row.get("market") is not None
        or (row.get("fallback_reason") is not None and not isinstance(row["fallback_reason"], str))
    ):
        raise ValueError("实验预测行格式无效")
    if row["status"] != "predicted" and not row.get("fallback_reason"):
        raise ValueError("实验空缺必须有原因")
    counterpart = identities.get(row["match_id"])
    if (
        counterpart is None
        or any(row[k] != counterpart[k] for k in ("source", "league", "home", "away"))
        or instant(row["kickoff_utc"]) != instant(counterpart["kickoff_utc"])
    ):
        raise ValueError("实验与同批正式目标身份不符")
    key = row["match_id"], row["model_name"]
    if key in seen:
        raise ValueError("同一比赛/模型含重复实验预测")
    if (
        row.get("p2_ready") is not False
        or row.get("validation_status") != "experimental_unadopted"
        or row.get("availability_mode") != "strict"
    ):
        raise ValueError("实验预测必须 strict / P2 实验")
    p = row.get("model_probabilities")
    if row["status"] == "predicted":
        if (
            not isinstance(p, dict)
            or set(p) != set("hda")
            or any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in p.values())
            or abs(sum(p.values()) - 1) > 1e-8
        ):
            raise ValueError("实验概率无效")
    elif p is not None:
        raise ValueError("实验空缺不得携带回退概率")
    return key, counterpart


def _load_experiment_file(archive, manifest, forecast, at, publications, run, audit):
    """Validate the attachment, keeping named rating row failures isolated."""
    path = forecast["experimental_path"]
    doc = json.loads((archive / path).read_bytes(), parse_constant=_invalid_json_constant)
    if (
        not isinstance(doc, dict)
        or doc.get("schema_version") != 1
        or doc.get("availability_mode") != "strict"
        or doc["batch_id"] != forecast["batch_id"]
        or doc.get("mode") != "prematch"
        or not isinstance(doc.get("predictions"), list)
    ):
        raise ValueError("实验冻结格式或批次无效")
    cutoff = instant(doc["as_of_utc"])
    stamp = instant(doc["generated_utc"])
    evidence = publications.get(path)
    published = instant(evidence["published_utc"]) if evidence else None
    if published is not None and published < stamp:
        published = None
    if stamp > instant(at):
        return []
    original = json.loads((archive / forecast["path"] / "forecast.json").read_bytes())
    identities = {r["match_id"]: r for r in original["predictions"]}
    training_cutoffs = {
        model["model_version"]: model.get("model_as_of_utc")
        for model in original.get("models", ())
        if model["model_name"] == "dixon-coles"
    }
    rows, seen = [], set()
    rating_counts = Counter(
        row["match_id"]
        for row in doc["predictions"]
        if isinstance(row, dict)
        and row.get("model_name") == RATING_NAME
        and isinstance(row.get("match_id"), str)
    )
    for row in doc["predictions"]:
        is_rating = isinstance(row, dict) and row.get("model_name") == RATING_NAME
        try:
            key, counterpart = _validate_experiment_row(row, identities, seen)
            if is_rating:
                if rating_counts[row["match_id"]] > 1:
                    raise ValueError("同一比赛含重复跨联赛评分预测")
                _verify_rating_row(row, counterpart, cutoff, run, training_cutoffs)
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            if not is_rating:
                raise
            audit.append(
                dict(
                    forecast_path=path,
                    batch_id=forecast["batch_id"],
                    match_id=row.get("match_id"),
                    reason="invalid_rating_row",
                    detail=f"{type(exc).__name__}: {str(exc)[:200]}",
                )
            )
            continue
        seen.add(key)
        if published is None or published >= instant(row["kickoff_utc"]):
            row = dict(
                row,
                status="missing",
                model_probabilities=None,
                top_score=None,
                fallback_reason="publication_unknown"
                if published is None
                else "published_after_kickoff",
            )
        rows.append(
            dict(
                row,
                batch_id=doc["batch_id"],
                generated_utc=stamp.isoformat(),
                published_utc=published.isoformat() if published else None,
                publication_evidence=evidence,
                as_of_utc=doc["as_of_utc"],
                mode=doc["mode"],
                forecast_path=path,
                forecast_sha256=manifest["files"][path],
            )
        )
    return rows


def load_experiments(archive, manifest, at, *, publications=None):
    publications = publication_times(archive, manifest) if publications is None else publications
    rows, audit = [], []
    for run in (manifest or {}).get("runs", {}).values():
        for forecast in run["forecasts"].values():
            path = forecast.get("experimental_path")
            if not path:
                continue
            try:
                loaded = _load_experiment_file(
                    archive, manifest, forecast, at, publications, run, audit
                )
            except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
                audit.append(
                    dict(
                        forecast_path=path,
                        batch_id=forecast["batch_id"],
                        reason="invalid_experimental_file",
                        detail=f"{type(exc).__name__}: {str(exc)[:200]}",
                    )
                )
            else:
                rows.extend(loaded)
    return rows, audit


def select_latest(rows, at):
    selected = {}
    for row in sorted(rows, key=lambda r: (instant(r["generated_utc"]), r["batch_id"])):
        if (
            row["mode"] == "prematch"
            and instant(row["as_of_utc"]) <= instant(row["generated_utc"])
            and instant(row["as_of_utc"]) < instant(row["kickoff_utc"])
            and instant(row["generated_utc"]) <= instant(at)
            and (not row.get("published_utc") or instant(row["published_utc"]) <= instant(at))
        ):
            selected[row["match_id"], row["model_name"]] = row
    return selected


def calibration_error(stats):
    bins = stats["calibration"]
    n = sum(b["n"] for b in bins)
    return sum(b["n"] * abs(b["mean_p"] - b["observed"]) for b in bins) / n if n else None


def finite_summary(stats):
    # Keep an infinite loss explicit, without breaking the strict JSON writer.
    return {
        k: ("Infinity" if v > 0 else "-Infinity")
        if isinstance(v, float) and not math.isfinite(v)
        else v
        for k, v in stats.items()
    }


def adoption(comparison, experiment_ece, production_ece, relative_coverage):
    ll = comparison.get("log_loss") or {}
    interval = ll.get("interval")
    checks = dict(
        common_n=comparison["n"] >= RULES["minimum_common_n"],
        log_loss=ll.get("difference") is not None and ll["difference"] < 0,
        interval=bool(interval and interval[1] < 0),
        calibration=experiment_ece is not None
        and production_ece is not None
        and experiment_ece <= production_ece,
        coverage=relative_coverage is not None
        and relative_coverage >= RULES["minimum_relative_coverage"],
    )
    return dict(
        checks=checks,
        remaining_common_n=max(0, RULES["minimum_common_n"] - comparison["n"]),
        unmet=[k for k, v in checks.items() if not v],
        status=RULES["action"] if all(checks.values()) else "证据不足，保持 P2 实验",
        automatic_adoption=False,
    )


def summarize_experiments(
    forecasts, scored, production, active_production, at, *, activated=None, rating_batches=None
):
    latest = select_latest(forecasts, at)
    latest_valid = {
        (r["match_id"], r["model_name"]): r for r in prematch_rows(forecasts, at, by_model=True)
    }
    attempts = {(r["match_id"], r["model_name"], r["batch_id"]): r for r in forecasts}
    active = {
        record_key(r["match_id"], r["batch_id"], r["model_name"]) for r in latest_valid.values()
    }
    exp_scores = {
        r["record_id"]: r
        for r in scored
        if r["record_id"] in active and instant(r["available_utc"]) <= instant(at)
    }
    originals = [
        r
        for r in production
        if r["record_id"] in set(active_production) and instant(r["available_utc"]) <= instant(at)
    ]
    if activated is None:
        activated = min(
            (instant(r["as_of_utc"]) for r in forecasts if r["model_name"] != RATING_NAME),
            default=None,
        )
    today = instant(at).astimezone(BEIJING).date()
    groups = []
    for name, _, _, league, _ in specifications():
        population, pairs = [], []
        for baseline in originals:
            experiment = attempts.get((baseline["match_id"], name, baseline["batch_id"]))
            if (
                baseline["league"] != league
                or activated is None
                or instant(baseline["generated_utc"]) < instant(activated)
            ):
                continue
            population.append(baseline)
            if experiment is None or experiment["batch_id"] != baseline["batch_id"]:
                continue
            # Pair from the exact frozen attempt, independently of the experiment's
            # own latest batch. Its B2 must not erase a valid B1 pair with production.
            if (
                prematch_rows([experiment], at, by_model=True)
                and experiment["league"] == baseline["league"]
                and instant(experiment["kickoff_utc"]) == instant(baseline["kickoff_utc"])
                and instant(experiment["generated_utc"]) < instant(baseline["available_utc"])
            ):
                pairs.append(
                    dict(
                        match_id=baseline["match_id"],
                        kickoff_utc=baseline["kickoff_utc"],
                        actual="hda".index(baseline["actual"]),
                        variants={
                            "experiment": dict(
                                probabilities=[experiment["model_probabilities"][k] for k in "hda"]
                            ),
                            "production": dict(
                                probabilities=[baseline["model_probabilities"][k] for k in "hda"]
                            ),
                        },
                    )
                )
        comparison = paired(pairs, "experiment", "production")
        exp_stats, prod_stats = summarize(pairs, "experiment"), summarize(pairs, "production")
        exp_ece, prod_ece = calibration_error(exp_stats), calibration_error(prod_stats)
        coverage = len(pairs) / len(population) if population else None
        daily = [
            r
            for r in latest.values()
            if r["model_name"] == name
            and r["league"] == league
            and instant(r["kickoff_utc"]).astimezone(BEIJING).date() == today
        ]
        own = [r for r in exp_scores.values() if r["model_name"] == name and r["league"] == league]
        own_stats = summarize(
            [
                dict(
                    actual="hda".index(r["actual"]),
                    variants={
                        "experiment": dict(
                            probabilities=[r["model_probabilities"][k] for k in "hda"]
                        )
                    },
                )
                for r in own
            ],
            "experiment",
        )
        groups.append(
            dict(
                model_name=name,
                league=league,
                label="P2 实验",
                validation_status="experimental_unadopted",
                today=dict(
                    date=str(today),
                    predicted=sum(r["status"] == "predicted" for r in daily),
                    missing=sum(r["status"] != "predicted" for r in daily),
                    reasons=dict(
                        Counter(r["fallback_reason"] for r in daily if r["status"] != "predicted")
                    ),
                ),
                own_scored_n=len(own),
                own_statistics=finite_summary(own_stats),
                production_n=len(population),
                common_n=len(pairs),
                paired=comparison,
                common_experiment=finite_summary(exp_stats),
                common_production=finite_summary(prod_stats),
                experiment_ece=exp_ece,
                production_ece=prod_ece,
                relative_coverage=coverage,
                adoption=adoption(comparison, exp_ece, prod_ece, coverage),
            )
        )
    if rating_batches is not None:
        groups.append(_rating_group(originals, attempts, latest, exp_scores, rating_batches, at))
    return dict(disclaimer="实验 · 不下注", rules=RULES, groups=groups)


def _rating_comparison(pairs, population_n):
    experiment = summarize(pairs, "experiment")
    production = summarize(pairs, "production")
    return dict(
        production_n=population_n,
        common_n=len(pairs),
        relative_coverage=len(pairs) / population_n if population_n else None,
        paired=paired(pairs, "experiment", "production"),
        common_experiment=finite_summary(experiment),
        common_production=finite_summary(production),
        experiment_ece=calibration_error(experiment),
        production_ece=calibration_error(production),
    )


def _rating_order(row):
    return instant(row["available_utc"]), instant(row["kickoff_utc"]), row["match_id"]


def _rating_checkpoints(pairs, population):
    checkpoints = {}
    for threshold in (300, 600):
        if len(pairs) < threshold:
            continue
        sample = pairs[:threshold]
        last = _rating_order(sample[-1])
        comparison = _rating_comparison(sample, sum(_rating_order(r) <= last for r in population))
        gate = adoption(
            comparison["paired"],
            comparison["experiment_ece"],
            comparison["production_ece"],
            comparison["relative_coverage"],
        )
        rating_interval = (comparison["paired"]["log_loss"] or {}).get("interval")
        average = paired(sample, "half_average", "production")
        average_interval = (average["log_loss"] or {}).get("interval")
        if threshold == 300:
            stop = rating_interval[0] > 0 if rating_interval else None
        elif not gate["unmet"]:
            stop = False
        else:
            stop = average_interval[1] >= 0 if average_interval else None
        checkpoints[str(threshold)] = dict(
            common_n=threshold,
            through_result_utc=last[0].isoformat(),
            log_loss=comparison["paired"]["log_loss"],
            half_average_log_loss=average["log_loss"],
            adoption=gate,
            failure_condition=stop,
        )
    return checkpoints


def _rating_group(originals, attempts, latest, exp_scores, batches, at):
    population = [
        row
        for row in originals
        if row["batch_id"] in batches and row["model_name"] == "dixon-coles"
    ]
    today = instant(at).astimezone(BEIJING).date()
    daily = [
        row
        for row in latest.values()
        if row["model_name"] == RATING_NAME
        and row["batch_id"] in batches
        and instant(row["kickoff_utc"]).astimezone(BEIJING).date() == today
    ]
    own = [
        row
        for row in exp_scores.values()
        if row["model_name"] == RATING_NAME and row["batch_id"] in batches
    ]
    pairs = []
    for baseline in sorted(population, key=_rating_order):
        experiment = attempts.get((baseline["match_id"], RATING_NAME, baseline["batch_id"]))
        if (
            experiment is None
            or not prematch_rows([experiment], at, by_model=True)
            or experiment["league"] != baseline["league"]
            or instant(experiment["kickoff_utc"]) != instant(baseline["kickoff_utc"])
            or instant(experiment["generated_utc"]) >= instant(baseline["available_utc"])
        ):
            continue
        rating, formal = (
            [row["model_probabilities"][key] for key in "hda"] for row in (experiment, baseline)
        )
        pairs.append(
            dict(
                match_id=baseline["match_id"],
                kickoff_utc=baseline["kickoff_utc"],
                available_utc=baseline["available_utc"],
                league=baseline["league"],
                default_strength=uses_unknown_team_prior(baseline),
                actual="hda".index(baseline["actual"]),
                variants=dict(
                    experiment=dict(probabilities=rating),
                    production=dict(probabilities=formal),
                    half_average=dict(
                        probabilities=[(a + b) / 2 for a, b in zip(rating, formal, strict=True)]
                    ),
                ),
            )
        )
    comparison = _rating_comparison(pairs, len(population))
    own_stats = summarize(
        [
            dict(
                actual="hda".index(row["actual"]),
                variants={
                    "experiment": dict(
                        probabilities=[row["model_probabilities"][key] for key in "hda"]
                    ),
                },
            )
            for row in own
        ],
        "experiment",
    )
    return dict(
        model_name=RATING_NAME,
        league="所有联赛",
        comparison_label=f"跨联赛评分 对 正式模型（{RATING_NAME}） / P2 实验",
        label="P2 实验",
        validation_status="experimental_unadopted",
        today=dict(
            date=str(today),
            predicted=sum(row["status"] == "predicted" for row in daily),
            missing=sum(row["status"] != "predicted" for row in daily),
            reasons=dict(
                Counter(row["fallback_reason"] for row in daily if row["status"] != "predicted")
            ),
        ),
        **comparison,
        own_scored_n=len(own),
        own_statistics=finite_summary(own_stats),
        adoption=adoption(
            comparison["paired"],
            comparison["experiment_ece"],
            comparison["production_ece"],
            comparison["relative_coverage"],
        ),
        by_league={
            league: _rating_comparison(
                [row for row in pairs if row["league"] == league],
                sum(row["league"] == league for row in population),
            )
            for league in sorted({row["league"] for row in population})
        },
        default_strength=_rating_comparison(
            [row for row in pairs if row["default_strength"]],
            sum(uses_unknown_team_prior(row) for row in population),
        ),
        half_average=dict(
            label="各半平均（对照，不是一条实验线）",
            common_n=len(pairs),
            paired=paired(pairs, "half_average", "production"),
            statistics=finite_summary(summarize(pairs, "half_average")),
        ),
        checkpoints=_rating_checkpoints(pairs, population),
    )


def render_experiments(summary, *, include_comparison=False):
    if not summary:
        return ""
    lines = [
        "\n## 实验模型",
        "",
        "实验 · 不下注",
        "",
        "差值 = 实验 − 正式；LL / Brier 越低越好。共同样本须同场、同批次。",
        "",
        "| 模型 / 联赛 | 统计日：有预测 / 空缺 | 空缺原因 | 累计共同样本 "
        "| LL 差 / 95%区间 | Brier 差 / 95%区间 | 距采用规则 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    labels = dict(
        common_n="共同样本未满200",
        log_loss="LL未改善",
        interval="区间未明确改善",
        calibration="校准未达到",
        coverage="覆盖未达90%",
    )

    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ")

    def metric(value):
        if not value or value.get("difference") is None:
            return "—"
        bounds = value.get("interval")
        return f"{value['difference']:.4f} / " + (
            f"[{bounds[0]:.4f}, {bounds[1]:.4f}]" if bounds else "区间不足"
        )

    for group in summary["groups"]:
        day, gate = group["today"], group["adoption"]
        reasons = {
            "strict_features_missing": "赛前可用统计不足",
            "unsupported_league": "不在本实验范围",
            "already_started": "实验生成时已开赛",
            "published_after_kickoff": "实验发布迟于开球",
            "publication_unknown": "实验发布时间未知",
            "fit_failed": "拟合失败",
            "prediction_failed": "计算失败",
            "experiment_failed": "实验数据或计算失败",
            "experiment_timeout": "实验超过时间预算",
            "experiment_cancelled": "实验运行被取消",
            "experiment_archive_failed": "实验附件上传失败",
            "experiment_publication_failed": "实验发布失败",
            "experimental_artifact_missing": "实验附件缺失",
            "missing_team_rating": "球队缺少评分",
            "target_identity_unverified": "球队身份未核实",
            "rating_fit_failed": "评分计算失败",
            "primary_cutoff_mismatch": "与正式模型的训练截点不一致",
        }
        missing = (
            "；".join(
                f"{reasons.get(key.split(':')[0], key)} {n}" for key, n in day["reasons"].items()
            )
            or "—"
        )
        gaps = []
        for key in gate["unmet"]:
            if key == "common_n":
                gaps.append(f"还差{gate['remaining_common_n']}场共同样本")
            elif key == "coverage" and group["relative_coverage"] is not None:
                gaps.append(f"覆盖{group['relative_coverage']:.1%}，需90%")
            else:
                gaps.append(labels[key])
        gap = "、".join(gaps) or gate["status"]
        lines.append(
            "| "
            + " | ".join(
                map(
                    cell,
                    [
                        group.get("comparison_label", f"{group['model_name']} / {group['league']}"),
                        f"{day['date']}：{day['predicted']} / {day['missing']}",
                        missing,
                        group["common_n"],
                        metric(group["paired"]["log_loss"]),
                        metric(group["paired"]["brier"]),
                        gap,
                    ],
                )
            )
            + " |"
        )
    if include_comparison:
        for group in summary["groups"]:
            if group["model_name"] != RATING_NAME:
                continue
            lines.extend(
                [
                    "",
                    "### 跨联赛评分描述性比较",
                    "",
                    "| 分组 | 共同样本 | LL 差 / 95%区间 | Brier 差 / 95%区间 |",
                    "| --- | ---: | --- | --- |",
                ]
            )
            for label, row in (
                *group["by_league"].items(),
                ("正式模型用了默认强度", group["default_strength"]),
                (group["half_average"]["label"], group["half_average"]),
            ):
                lines.append(
                    "| "
                    + " | ".join(
                        map(
                            cell,
                            (
                                label,
                                row["common_n"],
                                metric(row["paired"]["log_loss"]),
                                metric(row["paired"]["brier"]),
                            ),
                        )
                    )
                    + " |"
                )
            for count, point in group["checkpoints"].items():
                failed = point["failure_condition"]
                status = (
                    "达到失败条件，提交所有者决定"
                    if failed is True
                    else "区间证据不足"
                    if failed is None
                    else "当前未达到失败条件"
                )
                lines.extend(["", f"- 首批 {count} 场共同样本：{status}。"])
    return "\n".join(lines) + "\n"
