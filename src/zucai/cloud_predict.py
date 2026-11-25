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

from zucai.cloud_feeds import archive_info, digest, restore_feeds
from zucai.db import get_engine
from zucai.digest_context import audit_rows, jc_coverage
from zucai.forecast_archive import _component, publish_forecasts, verify_forecasts
from zucai.models import Match, SfcIssue
from zucai.nations import LEAGUE as NATIONS_LEAGUE
from zucai.predict.command import train
from zucai.predict.dixon_coles import DixonColes
from zucai.predict.model_definitions import LEGACY_LEAGUES, T63_LEAGUES, entry_definition_metadata
from zucai.predict.parameters import (
    model_readiness,
    parameters_path,
    read_parameters,
    resolve_parameters,
)
from zucai.predict.pooled import POOL, SOURCES, model_targets
from zucai.predict.training_sources import apif_history_sources
from zucai.report.batch import predict_batch
from zucai.report.command import _deadline
from zucai.report.render import load_batch, write_report
from zucai.scoreboard.evidence import read_evidence
from zucai.sfc_sequence import period_windows, sequence
from zucai.sources.base import _atomic_write
from zucai.sources.sfc import refresh_snapshot
from zucai.sources.sporttery import BEIJING

LOG = logging.getLogger(__name__)
DISCLAIMER = "非购买建议 · BET 禁用 · P2 标签含义见 README"
EXPERIMENT_UNAVAILABLE_REASONS = (
    "experiment_timeout",
    "experiment_cancelled",
    "experiment_failed",
    "experiment_publication_failed",
)


def _now():
    return datetime.now(UTC)


def _json(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )


def _summary(archive, run):
    lines = [DISCLAIMER, "", f"### 定时预测：{run['run_id']} · {run['status']}", ""]
    if run["sporttery_stale"]:
        lines += [
            "竞彩数据非今日：本次竞彩 job 未成功、赛程阶段未完整成功或归档水位不是今日；"
            "按现有快照预测。",
            "",
        ]
    from zucai.digest_views import prediction_source_lines

    lines += prediction_source_lines(run)
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
    for result in run["leagues"].values():
        if result.get("training_leagues"):
            lines.append(
                "\n欧战训练语料："
                + "、".join(f"{name} {n} 场" for name, n in result["training_leagues"].items())
                + "。\n"
            )
    if candidates := run.get("source_versions", {}).get("leagues"):
        lines += ["", "七个旧规联赛的新来源候选独立冻结，标实验，不供纸面账户或14场报告使用。", ""]
        for league, result in candidates.items():
            lines.append(f"- {league}：{result['status']}，预测 {result['predicted']} 场，实验。")
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
        if forecast["predicted"]:
            scores = report.split("## 比分前五与快照\n", 1)[1].split("\n## ", 1)[0]
            lines.append("## 比分前五与快照\n" + scores)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as output:
            output.write("\n".join(lines) + "\n")


def open_national_fixtures(session, now, schedule=None):
    """Unstarted, mapped lottery fixtures, whether or not JC sells them.

    The 14-match forecast does not depend on the JC list (owner's rule, 2026-11-19): every
    mapped fixture of the period whose turn it is becomes a forecast target on its own
    Beijing kick-off day, in every run until the period closes. Nations League rows keep
    their earlier scope (every issue still open). A period listed ahead of its turn adds
    nothing.
    """
    by_day = {}
    issues = list(session.scalars(select(SfcIssue).where(SfcIssue.observed_utc <= now)))
    known = [
        dict(
            issue_no=issue.issue_no,
            sale_end_utc=issue.sale_end_utc,
            official_verification=issue.official_verification,
        )
        for issue in issues
    ]
    windows = period_windows(known, schedule or {}, now)
    state = sequence(windows.values(), now)
    in_line = set(state["in_line"])
    on_sale = in_line.union(state["ahead"])
    for issue in issues:
        if not issue.snapshot or issue.issue_no not in on_sale:
            continue
        for row in refresh_snapshot(session, issue):
            if (
                row.get("source_ended")
                or row.get("mapping") != "matched"
                or not row.get("match_id")
                or row.get("kickoff_basis") == "placeholder"
            ):
                continue
            if row["league"] != NATIONS_LEAGUE and issue.issue_no not in in_line:
                continue
            kickoff = datetime.fromisoformat(row["kickoff_utc"])
            if kickoff > now:
                by_day.setdefault(kickoff.astimezone(BEIJING).date(), set()).add(row["match_id"])
    return by_day


def run(
    archive: Path,
    params: Path | None,
    run_id: str,
    *,
    sporttery_status="unknown",
    sporttery_publish_status=None,
    feed_commit=None,
    production_state: Path | None = None,
) -> dict:
    """Freeze production, optionally retaining its exact database for later experiments."""
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
    apif_domestic = apif_history_sources()
    for league, entry in config["leagues"].items():
        _component(league)
        allowed = set(SOURCES) if league == POOL else {"jc", "fd"}
        if league == NATIONS_LEAGUE:
            allowed = {"jc", "apif"}
        if league in apif_domestic:
            allowed = apif_domestic[league]
        if not entry.get("sources") or not set(entry["sources"]) <= allowed:
            raise ValueError(f"{league} 的训练来源必须显式配置且仅限 {' / '.join(sorted(allowed))}")
        if league == POOL and "jc" not in entry["sources"]:
            raise ValueError("欧战每日预测的训练来源须包含 jc，才能注册三项竞彩赛事")
        if league in LEGACY_LEAGUES and set(entry["sources"]) != {"fd", "jc"}:
            raise ValueError("七个旧规联赛的主模型须保留 fd,jc；新来源走独立候选冻结")
    if production_state is not None:
        production_state = Path(production_state).resolve()
        production_state.mkdir(parents=True)
    cutoff = _now()
    with TemporaryDirectory(prefix="zucai-forecast-") as temporary:
        stage = Path(temporary)
        database = (production_state or stage / "work/data") / "zucai.db"
        feed = restore_feeds(archive, database)
        database_sha = digest(database)
        models = stage / "models" / run_id
        models.mkdir(parents=True)
        outcomes, fitted = {}, {}
        own_shares = {}
        for league, entry in config["leagues"].items():
            selected = resolve_parameters(league, as_of=cutoff, params_file=params)
            model_path = f"models/{run_id}/{league}.pkl"
            train_path = f"models/{run_id}/{league}.train.json"
            result = {
                **entry_definition_metadata(league, entry),
                "training_json_path": train_path,
                "training_cutoff_utc": cutoff.isoformat(),
                "training_database_sha256": database_sha,
                "sources": sorted(entry["sources"]),
                "half_life_days": selected["half_life_days"],
                "parameter_source": selected["source"],
                "predicted": 0,
                "p2_ready": False,
                "p2_label": "实验" if league in T63_LEAGUES else "P2 实验",
                "paper_eligible": False,
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
                own_shares[league] = training.get("half_share_training")
                result.update(
                    status="trained",
                    model_path=model_path,
                    sha256=digest(stage / model_path),
                    training_rows=training["training_rows"],
                )
                if getattr(model, "pooled", False):
                    result["training_leagues"] = model.training_leagues
                LOG.info("%s 训练成功：%s 场", league, training["training_rows"])
            except Exception as exc:
                # One league must not prevent independently fitted leagues from being published.
                LOG.exception("%s 训练失败，继续其他联赛", league)
                (stage / model_path).unlink(missing_ok=True)
                result.update(status="failed", reason=f"{type(exc).__name__}: {exc}")
                _json(stage / train_path, result)
            outcomes[league] = result
        prediction_time = _now()
        prospective_evidence = read_evidence(archive, prediction_time)
        for league, model in fitted.items():
            outcomes[league].update(
                model_readiness(model, as_of=prediction_time, params_file=params)
            )
        outcome_keys = {
            target: name for name, model in fitted.items() for target in model_targets(model)
        }
        day = prediction_time.astimezone(BEIJING).date()
        result = {
            "run_id": run_id,
            "event_name": os.environ.get("GITHUB_EVENT_NAME") or "local",
            "operation": os.environ.get("ZUCAI_OPERATION") or None,
            "experimental_protocol": "t34a-strict-v1",
            "training_cutoff_utc": cutoff.isoformat(),
            "database_sha256": database_sha,
            "params_sha256": params_sha,
            "feed_manifest_sha256": digest(archive / "feed_manifest.json"),
            "sporttery_publish_status": sporttery_publish_status,
            "feed_snapshot": {
                "commit": feed_commit,
                "run_id": feed.get("run"),
                "created_utc": feed.get("created_utc"),
                "schedule_updated_utc": feed.get("collection", {})
                .get("jc", {})
                .get("schedule_updated_utc"),
                "results_updated_utc": feed.get("collection", {})
                .get("jc", {})
                .get("results_updated_utc"),
            },
            "sporttery_stale": sporttery_publish_status == "failure"
            or sporttery_status != "success"
            or feed["through_beijing"] != day.isoformat()
            or feed.get("collection", {}).get("jc", {}).get("schedule_status", "complete")
            != "complete",
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
                    from zucai.predict.hafu_share import combine_shares, tiered_share

                    combined = combine_shares(list(own_shares.values()))
                    half_share_hints = {}
                    for league, model in fitted.items():
                        hints = tiered_share(
                            own_shares.get(league),
                            combined,
                            pooled=getattr(model, "pooled", False),
                        )
                        half_share_hints[league] = hints
                        outcomes[league]["half_share"] = hints
                        if hints is None:
                            LOG.warning("%s 半场进球占比不可估计", league)
                    catalog = audit_rows(
                        archive, feed, "jc_catalog", prediction_time, all_shards=True
                    )
                    coverage = jc_coverage(prediction_time, catalog)
                    result["jc_extra_dates"] = sorted(
                        d.isoformat() for d, ids in coverage.items() if ids is not None
                    )
                    schedule = feed.get("collection", {}).get("sfc", {}).get("official_schedule")
                    national = open_national_fixtures(session, prediction_time, schedule)
                    result["sfc_extra_dates"] = sorted(
                        d.isoformat() for d in national.keys() - coverage.keys()
                    )
                    for forecast_day in sorted(coverage.keys() | national.keys()):
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
                        if forecast_day not in coverage:
                            keys = []
                        elif coverage[forecast_day] is not None:
                            keys = [key for key in keys if key in coverage[forecast_day]]
                        sfc_only = national.get(forecast_day, set()).difference(keys)
                        keys.extend(sorted(sfc_only))
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
                            prospective_evidence=prospective_evidence,
                            half_share_hints=half_share_hints,
                        )
                        if forecast_day not in coverage:
                            batch["input"]["scope"] = "sfc_only"
                        elif coverage[forecast_day] is not None:
                            batch["input"]["scope"] = "jc_spillover"
                            batch["input"]["jc_scope_match_ids"] = sorted(coverage[forecast_day])
                        for prediction in batch["predictions"]:
                            if prediction["match_id"] in sfc_only:
                                prediction["sfc_only"] = True
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
                                outcomes[outcome_keys[row["league"]]]["predicted"] += 1
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
        if production_state is None:
            result = _freeze_additions(archive, stage, database, feed, batches, result, config)
    _summary(archive, result)
    LOG.info(
        "预测归档 %s：%s；%d/%d 个联赛训练成功",
        run_id,
        result["status"],
        len(fitted),
        len(outcomes),
    )
    return result


def _freeze_additions(archive, stage, database, feed, batches, result, config, *, state=None):
    run_id = result["run_id"]
    cutoff = datetime.fromisoformat(result["training_cutoff_utc"])
    # The approved legacy stream is already immutable. Source candidates use
    # the identical training/prediction cutoffs and never replace that stream.
    candidate_stage = "training"
    try:
        from zucai.source_version_archive import (
            attach_source_versions,
            record_source_version_failure,
        )
        from zucai.source_version_training import predict_candidates, train_candidates

        candidates, candidate_models, candidate_params = train_candidates(
            database, stage, run_id, cutoff, config, trainer=train
        )
        if candidates:
            candidate_stage = "prediction"
            candidate_engine = get_engine(database)
            try:
                with candidate_engine.connect() as connection:
                    connection.exec_driver_sql("PRAGMA query_only=ON")
                    connection.exec_driver_sql("BEGIN")
                    with Session(bind=connection) as session:
                        documents = {
                            batch["input"]["kickoff_date_beijing"]: predict_candidates(
                                session,
                                primary=batch,
                                outcomes=candidates,
                                fitted=candidate_models,
                                params_path=candidate_params,
                                clock=_now,
                            )
                            for batch in batches
                        }
            finally:
                candidate_engine.dispose()
            candidate_stage = "freeze"
            result = attach_source_versions(
                archive, stage, run_id, candidates, documents, clock=_now
            )
    except Exception as exc:
        LOG.exception("新来源候选冻结失败；已冻结旧版与原实验联赛结果保持原样")
        try:
            result = record_source_version_failure(
                archive, run_id, candidate_stage, exc, clock=_now
            )
        except Exception:
            LOG.exception("新来源候选失败状态写入失败；已冻结主运行保持原样")

    from zucai.experimental_archive import freeze_parallel
    from zucai.parlay.archive import capture_injuries

    parlay_injuries = {}

    def on_restored(working):
        parlay_injuries.update(capture_injuries(working, batches))
        if state is not None:
            raw = json.dumps(parlay_injuries, ensure_ascii=False, allow_nan=False).encode()
            _atomic_write(state / "parlay-injuries.json", raw)

    try:
        result = freeze_parallel(
            archive, database, feed, batches, result, clock=_now, on_restored=on_restored
        )
    except Exception:
        if state is not None:
            raise  # The workflow discards this worktree and records the failure independently.
        LOG.exception("实验归档失败，已冻结的正式预测不受影响")
    return _freeze_parlay(archive, database, result, parlay_injuries)


def _freeze_parlay(archive, database, result, injuries):
    from zucai.parlay.archive import freeze

    try:
        return freeze(archive, database, result["run_id"], injuries=injuries, clock=_now)
    except Exception:
        LOG.exception("纸面复式归档失败，已冻结的正式与实验预测不受影响")
        return result


def _resume_run(archive, run_id, production_state):
    manifest = verify_forecasts(archive)
    if not manifest or run_id not in manifest["runs"]:
        raise ValueError("找不到本次已冻结的正式预测")
    run = manifest["runs"][run_id]
    database = production_state / "zucai.db"
    if not database.is_file() or digest(database) != run["database_sha256"]:
        raise ValueError("实验阶段的训练库与正式冻结记录不符")
    if digest(archive / "feed_manifest.json") != run["feed_manifest_sha256"]:
        raise ValueError("实验阶段的采集快照与正式冻结记录不符")
    batches = [
        load_batch(archive / forecast["path"] / "forecast.json")
        for forecast in sorted(run["forecasts"].values(), key=lambda item: item["path"])
    ]
    return run, database, batches


def run_experiments(archive, params, run_id, production_state):
    """Continue the same frozen run in the separately publishable experiment worktree."""
    archive, state = Path(archive).resolve(), Path(production_state).resolve()
    run, database, batches = _resume_run(archive, run_id, state)
    config, params_sha = read_parameters(parameters_path(params))
    if params_sha != run["params_sha256"]:
        raise ValueError("实验阶段的参数与正式冻结记录不符")
    feed = archive_info(archive)
    with TemporaryDirectory(prefix="zucai-forecast-extras-") as temporary:
        return _freeze_additions(
            archive, Path(temporary), database, feed, batches, run, config, state=state
        )


def record_experiment_unavailable(archive, run_id, production_state, *, reason):
    """Keep missing evidence for this run without replacing any frozen probabilities."""
    from zucai.experimental_archive import freeze_unavailable

    if reason not in EXPERIMENT_UNAVAILABLE_REASONS:
        raise ValueError("无效实验未生成原因")
    archive, state = Path(archive).resolve(), Path(production_state).resolve()
    run, database, batches = _resume_run(archive, run_id, state)
    missing = [
        batch
        for batch in batches
        if "experimental_path" not in run["forecasts"][batch["input"]["kickoff_date_beijing"]]
    ]
    if missing:
        run = freeze_unavailable(archive, missing, run, reason=reason, clock=_now)
    injuries_path = state / "parlay-injuries.json"
    injuries = json.loads(injuries_path.read_bytes()) if injuries_path.is_file() else {}
    return _freeze_parlay(archive, database, run, injuries)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run", "experiments", "experiment-unavailable"])
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--params", type=Path, help="默认遵循 ZUCAI_PARAMS / T16 定位规则")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--sporttery-status", default="unknown")
    parser.add_argument(
        "--sporttery-publish-status", default=os.environ.get("SPORTTERY_PUBLISH_STATUS") or None
    )
    parser.add_argument("--feed-commit")
    parser.add_argument("--production-state", type=Path)
    parser.add_argument("--reason", choices=EXPERIMENT_UNAVAILABLE_REASONS)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.command != "run" and args.production_state is None:
        parser.error("独立实验阶段需要 --production-state")
    if args.command == "experiments":
        result = run_experiments(args.archive, args.params, args.run_id, args.production_state)
        if "experimental_release" not in result and (
            summary := os.environ.get("GITHUB_STEP_SUMMARY")
        ):
            with Path(summary).open("a", encoding="utf-8") as output:
                output.write("\n实验未生成。缺席原因见本班实验归档和计算日志。\n")
        return
    if args.command == "experiment-unavailable":
        if args.reason is None:
            parser.error("实验未生成需要 --reason")
        record_experiment_unavailable(
            args.archive, args.run_id, args.production_state, reason=args.reason
        )
        return
    run(
        args.archive,
        args.params,
        args.run_id,
        sporttery_status=args.sporttery_status,
        sporttery_publish_status=args.sporttery_publish_status,
        feed_commit=args.feed_commit,
        production_state=args.production_state,
    )


if __name__ == "__main__":
    main()
