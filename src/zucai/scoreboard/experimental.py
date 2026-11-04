"""Parallel-only scoring and fixed adoption gates; never supplies the paper ledger."""

import json
import math
from collections import Counter

from zucai.predict.experiments.metrics import paired, summarize
from zucai.predict.experiments.parallel import NAMES, specifications
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


def _load_experiment_file(archive, manifest, forecast, at):
    """Validate the whole attachment before making any of its rows available."""
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
    instant(doc["as_of_utc"])
    stamp = max(
        instant(doc["generated_utc"]),
        instant(forecast.get("experimental_published_utc", doc["generated_utc"])),
    )
    if stamp > instant(at):
        return []
    original = json.loads((archive / forecast["path"] / "forecast.json").read_bytes())
    identities = {r["match_id"]: r for r in original["predictions"]}
    rows, seen = [], set()
    for row in doc["predictions"]:
        if (
            not isinstance(row, dict)
            or row.get("model_name") not in NAMES
            or not isinstance(row.get("model_version"), str)
            or not row["model_version"]
            or row.get("status") not in ("predicted", "missing", "excluded")
            or not isinstance(row.get("p2_label", "P2 实验"), str)
            or row.get("market") is not None
            or (
                row.get("fallback_reason") is not None
                and not isinstance(row["fallback_reason"], str)
            )
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
        seen.add(key)
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
                or any(
                    type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in p.values()
                )
                or abs(sum(p.values()) - 1) > 1e-8
            ):
                raise ValueError("实验概率无效")
        elif p is not None:
            raise ValueError("实验空缺不得携带回退概率")
        if stamp >= instant(row["kickoff_utc"]):
            row = dict(
                row,
                status="missing",
                model_probabilities=None,
                top_score=None,
                fallback_reason="published_after_kickoff",
            )
        rows.append(
            dict(
                row,
                batch_id=doc["batch_id"],
                generated_utc=stamp.isoformat(),
                as_of_utc=doc["as_of_utc"],
                mode=doc["mode"],
                forecast_path=path,
                forecast_sha256=manifest["files"][path],
            )
        )
    return rows


def load_experiments(archive, manifest, at):
    rows, audit = [], []
    for run in (manifest or {}).get("runs", {}).values():
        for forecast in run["forecasts"].values():
            path = forecast.get("experimental_path")
            if not path:
                continue
            try:
                loaded = _load_experiment_file(archive, manifest, forecast, at)
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


def summarize_experiments(forecasts, scored, production, active_production, at, *, activated=None):
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
    if activated is None and forecasts:
        activated = min(instant(r["as_of_utc"]) for r in forecasts)
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
    return dict(disclaimer="实验 · 不下注", rules=RULES, groups=groups)


def render_experiments(summary):
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
            "fit_failed": "拟合失败",
            "prediction_failed": "计算失败",
            "experiment_failed": "实验数据或计算失败",
            "experiment_archive_failed": "实验附件上传失败",
            "experimental_artifact_missing": "实验附件缺失",
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
                        f"{group['model_name']} / {group['league']}",
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
    return "\n".join(lines) + "\n"
