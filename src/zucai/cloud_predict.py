"""Train configured leagues and freeze two Beijing days from an offline feed snapshot."""

import argparse
import json
import logging
import os
import re
from collections import Counter
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import select
from sqlalchemy.orm import Session

from zucai.cloud_feeds import digest, restore_feeds
from zucai.db import get_engine
from zucai.forecast_archive import _component, publish_forecasts, verify_forecasts
from zucai.models import Match
from zucai.predict.command import train
from zucai.predict.dixon_coles import DixonColes
from zucai.predict.parameters import (
    model_readiness,
    parameters_path,
    read_parameters,
    resolve_parameters,
)
from zucai.report.batch import predict_batch
from zucai.report.command import _deadline
from zucai.report.render import write_report
from zucai.sources.sporttery import BEIJING

LOG = logging.getLogger(__name__)
DISCLAIMER = "非购买建议 · BET 禁用 · P2 标签含义见 README"


def _now():
    return datetime.now(UTC)


def _json(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )


def _summary(archive, run):
    lines = [DISCLAIMER, "", f"### 定时预测：{run['run_id']} · {run['status']}", ""]
    if run["sporttery_stale"]:
        lines += ["竞彩数据非今日：本次竞彩 job 未成功或归档水位不是今日；按现有快照预测。", ""]
    lines += [
        "56 天回测仅作冒烟检查，不是 P2 证据；P2 标签由已发布策略和模型共同判定。",
        "",
        "| 联赛 | 训练状态 | 训练场数 | 训练截点（北京） | 半衰期 / 来源 | P2 标签 | 本批预测 |",
        "| --- | --- | ---: | --- | --- | --- | ---: |",
    ]
    for league, result in run["leagues"].items():
        cutoff = datetime.fromisoformat(result["training_cutoff_utc"]).astimezone(BEIJING)
        reason = result.get("reason", "成功").replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {league} | {reason} | {result.get('training_rows', '—')} | {cutoff.isoformat()} | "
            f"{result['half_life_days']:g} 天 / {result['parameter_source']} | "
            f"{result['p2_label']} | {result['predicted']} |"
        )
    for day, forecast in run["forecasts"].items():
        lines += [
            "",
            f"### {day}（北京开球日）",
            "",
            f"预测 {forecast['predicted']} 场；排除 {forecast['excluded']} 场；"
            f"原因：{json.dumps(forecast['reasons'], ensure_ascii=False)}。",
            "",
        ]
        report = (archive / forecast["path"] / "report.md").read_text(encoding="utf-8")
        # Reuse the actual T06 table, including its EV and exclusion rendering rules.
        lines.append(report.split("## 纯模型与市场对照\n", 1)[1].split("## 比分前五与快照", 1)[0])
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as output:
            output.write("\n".join(lines) + "\n")


def run(archive: Path, params: Path | None, run_id: str, *, sporttery_status="unknown") -> dict:
    archive, params = Path(archive).resolve(), parameters_path(params)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", run_id):
        raise ValueError("无效 run ID")
    old = verify_forecasts(archive)
    if old and run_id in old["runs"]:
        raise ValueError(f"run ID 已存在：{run_id}")
    if not params.is_file():
        raise ValueError(f"参数文件不存在：{params}")
    config, params_sha = read_parameters(params)
    if not config["leagues"]:
        raise ValueError("参数文件没有联赛条目")
    for league, entry in config["leagues"].items():
        _component(league)
        if not entry.get("sources") or not set(entry["sources"]) <= {"jc", "fd"}:
            raise ValueError(f"{league} 的训练来源必须显式配置且仅限 jc / fd")
    cutoff = _now()
    with TemporaryDirectory(prefix="zucai-forecast-") as temporary:
        stage = Path(temporary)
        database = stage / "work/data/zucai.db"
        feed = restore_feeds(archive, database)
        database_sha = digest(database)
        models = stage / "models" / run_id
        models.mkdir(parents=True)
        outcomes, fitted = {}, {}
        for league, entry in config["leagues"].items():
            selected = resolve_parameters(league, as_of=cutoff, params_file=params)
            model_path = f"models/{run_id}/{league}.pkl"
            train_path = f"models/{run_id}/{league}.train.json"
            result = {
                "training_json_path": train_path,
                "training_cutoff_utc": cutoff.isoformat(),
                "training_database_sha256": database_sha,
                "sources": sorted(entry["sources"]),
                "half_life_days": selected["half_life_days"],
                "parameter_source": selected["source"],
                "predicted": 0,
                "p2_ready": False,
                "p2_label": "P2 实验",
            }
            try:
                training = train(
                    db=database,
                    league=league,
                    source=",".join(entry["sources"]),
                    since=cutoff - timedelta(days=56),
                    as_of=cutoff,
                    output=stage / model_path,
                    params_file=params,
                )
                model = DixonColes.load(stage / model_path)
                training["model_path"] = model_path
                training["smoke_backtest_since_utc"] = (cutoff - timedelta(days=56)).isoformat()
                training["smoke_backtest_is_p2_evidence"] = False
                _json(stage / train_path, training)
                fitted[league] = model
                result.update(
                    status="trained",
                    model_path=model_path,
                    sha256=digest(stage / model_path),
                    training_rows=training["training_rows"],
                )
                LOG.info("%s 训练成功：%s 场", league, training["training_rows"])
            except Exception as exc:
                # One league must not prevent independently fitted leagues from being published.
                LOG.exception("%s 训练失败，继续其他联赛", league)
                (stage / model_path).unlink(missing_ok=True)
                result.update(status="failed", reason=f"{type(exc).__name__}: {exc}")
                _json(stage / train_path, result)
            outcomes[league] = result
        prediction_time = _now()
        for league, model in fitted.items():
            outcomes[league].update(
                model_readiness(model, as_of=prediction_time, params_file=params)
            )
        day = prediction_time.astimezone(BEIJING).date()
        result = {
            "run_id": run_id,
            "training_cutoff_utc": cutoff.isoformat(),
            "database_sha256": database_sha,
            "params_sha256": params_sha,
            "feed_manifest_sha256": digest(archive / "feed_manifest.json"),
            "sporttery_stale": sporttery_status != "success"
            or feed["through_beijing"] != day.isoformat(),
            "status": "complete" if len(fitted) == len(outcomes) else "partial",
            "leagues": outcomes,
            "forecasts": {},
        }
        batches = []
        engine = get_engine(database)
        try:
            with engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA query_only=ON")
                connection.exec_driver_sql("BEGIN")
                with Session(bind=connection) as session:
                    for offset in range(2):
                        forecast_day = day + timedelta(days=offset)
                        start = datetime.combine(forecast_day, time(), BEIJING)
                        keys = list(
                            session.scalars(
                                select(Match.match_id)
                                .where(
                                    Match.source == "jc",
                                    Match.kickoff_utc >= start,
                                    Match.kickoff_utc < start + timedelta(days=1),
                                )
                                .order_by(Match.kickoff_utc, Match.match_id)
                            )
                        )
                        batch = predict_batch(
                            session,
                            model_files=[stage / outcomes[k]["model_path"] for k in fitted],
                            as_of=prediction_time,
                            generated=prediction_time,
                            match_ids=keys,
                            params_file=params,
                            market_weight=0,
                            persist=False,
                            allow_empty=True,
                        )
                        batch["input"]["kickoff_date_beijing"] = forecast_day.isoformat()
                        batch["batch_id"] = f"{run_id}-{forecast_day}"
                        completed = _now()
                        batch["generated_utc"] = completed.isoformat()
                        _deadline(batch, completed)
                        directory = f"forecasts/{forecast_day}/{run_id}"
                        write_report(batch, stage / directory)
                        batches.append(batch)
                        reasons = Counter(
                            row["reason"]
                            for row in batch["predictions"]
                            if row["status"] == "excluded"
                        )
                        predicted = 0
                        for row in batch["predictions"]:
                            if row["status"] == "predicted":
                                outcomes[row["league"]]["predicted"] += 1
                                predicted += 1
                        result["forecasts"][forecast_day.isoformat()] = {
                            "path": directory,
                            "batch_id": batch["batch_id"],
                            "generated_utc": batch["generated_utc"],
                            "predicted": predicted,
                            "excluded": sum(reasons.values()),
                            "reasons": dict(reasons),
                        }
        finally:
            engine.dispose()

        def check_snapshot_and_deadlines():
            if digest(database) != database_sha or digest(params) != params_sha:
                raise ValueError("训练库或参数文件在运行期间发生变化，拒绝发布")
            for batch in batches:
                _deadline(batch, _now())

        check_snapshot_and_deadlines()
        result["generated_utc"] = _now().isoformat()
        publish_forecasts(archive, stage, result, before_commit=check_snapshot_and_deadlines)
    _summary(archive, result)
    LOG.info(
        "预测归档 %s：%s；%d/%d 个联赛训练成功",
        run_id,
        result["status"],
        len(fitted),
        len(outcomes),
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run"])
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--params", type=Path, help="默认遵循 ZUCAI_PARAMS / T16 定位规则")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--sporttery-status", default="unknown")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    run(args.archive, args.params, args.run_id, sporttery_status=args.sporttery_status)


if __name__ == "__main__":
    main()
