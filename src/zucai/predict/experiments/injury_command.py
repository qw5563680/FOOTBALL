"""Read-only T38 injury experiment; full evidence is an immutable Release ZIP."""

import argparse
import io
import json
import logging
import os
import sqlite3
import subprocess
import zipfile
from collections import Counter
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd
import zstandard
from sqlalchemy import URL, create_engine, select
from sqlalchemy.orm import Session

from zucai import stats_release
from zucai.models import Odds, Result
from zucai.predict.base import utc
from zucai.predict.experiments.command import sha256, write_json, write_rows
from zucai.predict.experiments.history import unified_history
from zucai.predict.experiments.injury_evaluate import (
    AVAILABILITY_MODES,
    COLUMNS,
    DEFAULT_PROTOCOL,
    evaluate_group,
    group_summary,
    load_protocol,
)

LOG = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[4]


def read_aligned_history(session, games, league):
    """Keep target denominators, reject conflicting labels, never infer known time."""
    labels = {row.match_id: row for row in session.scalars(select(Result))}
    rows, rejected = [], Counter()
    for game in games:
        if game["league"] != league:
            continue
        label = labels.get(game["match_id"])
        siblings = [labels[key] for key in game["history_match_ids"] if key in labels]
        reason = None
        if len({(row.home_goals, row.away_goals) for row in siblings}) > 1:
            reason = "counterpart_result_conflict"
        elif label is None:
            reason = "missing_representative_result"
        known = utc(label.available_utc) if label and label.available_utc else pd.NaT
        if reason:
            rejected[reason] += 1
            known = pd.NaT
        rows.append(
            dict(
                **{
                    key: game[key]
                    for key in (
                        "match_id",
                        "source",
                        "league",
                        "home",
                        "away",
                        "home_team_id",
                        "away_team_id",
                        "kickoff_utc",
                        "earliest_kickoff_utc",
                        "history_match_ids",
                    )
                },
                available_utc=known,
                available_source=label.available_source if label else None,
                home_goals=label.home_goals if label else None,
                away_goals=label.away_goals if label else None,
                label_rejection_reason=reason,
            )
        )
    frame = pd.DataFrame(
        rows,
        columns=(
            "match_id",
            "source",
            "league",
            "home",
            "away",
            "home_team_id",
            "away_team_id",
            "kickoff_utc",
            "earliest_kickoff_utc",
            "history_match_ids",
            "available_utc",
            "available_source",
            "home_goals",
            "away_goals",
            "label_rejection_reason",
        ),
    )
    for column in ("kickoff_utc", "earliest_kickoff_utc", "available_utc"):
        frame[column] = pd.to_datetime(frame[column], utc=True)
    return frame, dict(rejected)


def attach_market(session, rows, statistics):
    """Only called after every scope's fits and selections have finished."""
    import math

    reverse = {}
    for game in statistics.matches.values():
        if game["source"] == "jc" and game.get("counterpart_id"):
            reverse.setdefault(game["counterpart_id"], []).append(game)
    links = {}
    for row in rows:
        original = statistics.matches[row["match_id"]]
        siblings = set(original.get("history_match_ids", [row["match_id"]]))
        possible = [statistics.matches[key] for key in siblings if key in statistics.matches]
        possible.extend(game for key in siblings for game in reverse.get(key, []))
        direct = statistics.matches.get(original.get("counterpart_id"))
        if direct:
            possible.append(direct)
        linked = {}
        for game in possible:
            if (
                game["source"] == "jc"
                and game["league"] == original["league"]
                and game["home_team_id"] == original["home_team_id"]
                and game["away_team_id"] == original["away_team_id"]
                and abs((game["kickoff_utc"] - original["kickoff_utc"]).total_seconds()) <= 10800
            ):
                linked[game["match_id"]] = game
        if len(linked) == 1:
            links[row["match_id"]] = next(iter(linked.values()))
    quotes = {}
    if links:
        query = select(Odds.match_id, Odds.snapshot_utc, Odds.selection, Odds.price).where(
            Odds.match_id.in_({game["match_id"] for game in links.values()}),
            Odds.pool == "had",
            Odds.line == 0,
        )
        for key, stamp, selection, price in session.execute(query):
            if selection in {"h", "d", "a"}:
                quotes.setdefault(key, {}).setdefault(utc(stamp), {})[selection] = price
    for row in rows:
        row["variants"]["market"] = None
        game = links.get(row["match_id"])
        if game is None or row["actual"] is None:
            continue
        cutoff = min(utc(row["feature_cutoff"]), game["kickoff_utc"])
        eligible = [
            (stamp, prices)
            for stamp, prices in quotes.get(game["match_id"], {}).items()
            if stamp < cutoff
            and set(prices) == set("hda")
            and all(math.isfinite(price) and price > 1 for price in prices.values())
        ]
        if eligible:
            stamp, prices = max(eligible, key=lambda pair: pair[0])
            inverse = [1 / prices[key] for key in "hda"]
            row["variants"]["market"] = dict(
                probabilities=[value / sum(inverse) for value in inverse],
                model_type="MARKET",
                match_id=game["match_id"],
                snapshot_utc=stamp.isoformat(),
                method="jc_had_inverse_odds_normalization",
                prices=prices,
            )


def _read_rows(path):
    with (
        Path(path).open("rb") as source,
        zstandard.ZstdDecompressor().stream_reader(source) as stream,
    ):
        return [json.loads(line) for line in io.TextIOWrapper(stream, encoding="utf-8")]


def _lightweight(rows):
    return [
        {
            **{
                key: row[key]
                for key in (
                    "match_id",
                    "kickoff_utc",
                    "actual",
                    "league",
                    "season",
                    "cold_start",
                    "statistics_missing",
                    "injuries_missing",
                    "experimental",
                    "missing_reasons",
                )
            },
            "variants": {
                name: None
                if item is None
                else {
                    "probabilities": item["probabilities"],
                    "score_brier": item.get("score_brier"),
                }
                for name, item in row["variants"].items()
            },
        }
        for row in rows
    ]


def _readme(summary, manifest, *, archive_name, release_tag, repository, archive_sha=None):
    pooled = summary["pooled"]
    lines = [
        "# T38 injury experiment",
        "",
        pooled["conclusion"],
        "",
        f"训练：backfill_replay；目标：strict_capture_time。目标 {pooled['targets']} 场，"
        f"真实共同样本 {pooled['common_targets']} 场。",
        "训练伤停为历史名单假设，统计为开球+3h假设；不证明当时版本已知。",
        "周初拟合、逐场开球取特征，并非当时在线冻结预测；合成测试不构成精度证据。",
        "没有概率回退；缺失保持 null。未自动采用，BET=false，P2=false。",
        "",
        "| column | full n / targets | full LL | common n | common LL | common Brier |",
        "| --- | --- | --- | --- | --- | --- |",
    ]

    def number(value):
        return "null" if value is None else f"{value:.8g}"

    for name in COLUMNS:
        full, common = pooled["models"][name], pooled["common_experiment"][name]
        lines.append(
            f"| {name} | {full['n']} / {full['total']} | {number(full['log_loss'])} | "
            f"{common['n']} | {number(common['log_loss'])} | {number(common['brier'])} |"
        )
    lines += [
        "",
        "六项配对差与 UTC 周块 bootstrap（2000次、seed 3301、95% CI），",
        "联赛/赛季/冷启动/统计缺失/伤停缺失分组及全部分母见 ZIP 内 summary.json。",
        "",
        f"- 数据提交：`{manifest['source_commit']}`",
        f"- 代码提交：`{manifest['code_commit']}`",
        f"- 输入数据库 SHA256：`{manifest['database_sha256']}`",
        f"- 冻结协议 SHA256：`{manifest['protocol_sha256']}`",
        f"- 全量附件：`{archive_name}`（Release `{release_tag}`）",
    ]
    if repository:
        lines.append(f"- Release：https://github.com/{repository}/releases/tag/{release_tag}")
    if archive_sha:
        lines.append(f"- 附件 SHA256：`{archive_sha}`")
    lines += [
        "",
        "主分支 reports 仅归档本 README.md 与 SHA256SUMS；完整证据仅放 Release 附件。",
        "本地生成不代表已上传；只有 --publish 校验上传成功才写 publication.json。",
        "",
    ]
    return "\n".join(lines)


def _zip_evidence(source, path):
    with zipfile.ZipFile(path, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(source.rglob("*")):
            if file.is_file():
                info = zipfile.ZipInfo(file.relative_to(source).as_posix(), (1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                with file.open("rb") as incoming, archive.open(info, "w") as outgoing:
                    while block := incoming.read(1024 * 1024):
                        outgoing.write(block)


def _input_hashes(database):
    from zucai.injuries_storage import AUDIT_NAME

    files = [
        database,
        Path(str(database) + "-wal"),
        database.parent / "state/audit" / AUDIT_NAME,
        database.parent / "state/audit/stats_ingest_audit.csv",
    ]
    return {
        path.relative_to(database.parent).as_posix(): sha256(path)
        for path in files
        if path.exists()
    }


def run(
    database,
    out,
    *,
    source_commit,
    code_commit=None,
    protocol_path=DEFAULT_PROTOCOL,
    leagues=None,
    publish=False,
    repository=None,
    release=None,
    run_id=None,
):
    database, out = Path(database).resolve(strict=True), Path(out).resolve()
    if out.exists():
        raise FileExistsError("T38 实验输出已存在，禁止覆盖冻结结果")
    if out.is_relative_to(ROOT / "reports"):
        raise ValueError("reports 只能保存 README 与 SHA256SUMS；完整实验请输出到 work")
    protocol = load_protocol(protocol_path)
    allowed = protocol["providers"]["fd"]["leagues"]
    selected = allowed if leagues is None else leagues
    if not selected or not set(selected).issubset(allowed) or len(set(selected)) != len(selected):
        raise ValueError("联赛超出 T38 冻结协议或重复")
    if publish and not all((repository, release, run_id)):
        raise ValueError("--publish 需要 --repository、--release 与 --run-id")
    release = release or datetime.now(UTC).strftime("experiments-%Y%m")
    stats_release.validate_tag(release)
    if not release.startswith("experiments-"):
        raise ValueError("T38 全量产物只能放 experiments-YYYYMM Release")
    run_id = run_id or datetime.now(UTC).strftime("local-%Y%m%dT%H%M%S%fZ")
    stats_release.validate_name(run_id)
    archive_name = f"t38-injuries-{run_id}.zip"
    before = _input_hashes(database)
    code_commit = (
        code_commit
        or subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    )
    uri = database.as_uri() + "?mode=ro"
    engine = create_engine(
        URL.create("sqlite+pysqlite", database=str(database)),
        creator=lambda: sqlite3.connect(uri, uri=True),
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        with TemporaryDirectory(prefix=".t38-", dir=out.parent) as temporary:
            stage = Path(temporary)
            evidence = stage / "evidence"
            evidence.mkdir()
            scopes, report_paths = {}, {}
            with Session(engine) as session:
                session.connection().exec_driver_sql("PRAGMA query_only=ON")
                from zucai.features.injuries import InjuryFeatures

                statistics, games = unified_history(session, provider="fd")
                injuries = InjuryFeatures(session)
                for league in selected:
                    LOG.info("T38 starting fd/%s", league)
                    history, label_rejections = read_aligned_history(session, games, league)
                    result = evaluate_group(history, statistics, injuries, protocol=protocol)
                    scope = f"fd-{league}"
                    directory = evidence / scope
                    directory.mkdir()
                    write_rows(directory / "predictions.jsonl.zst", result["predictions"])
                    write_rows(directory / "folds.jsonl.zst", result["folds"])
                    write_rows(
                        directory / "training-features.jsonl.zst", result["training_features"]
                    )
                    selection = result["selection"]
                    for window, trial in selection["trials"].items():
                        write_rows(
                            directory / f"selection-{window}.jsonl.zst", trial["predictions"]
                        )
                        write_rows(
                            directory / f"selection-{window}-folds.jsonl.zst", trial["folds"]
                        )
                    scopes[scope] = dict(
                        selected_window=selection["window"],
                        selection_evidence_sufficient=selection["evidence_sufficient"],
                        selection_reason=selection["reason"],
                        selection_common_candidate_n=selection["common_candidate_n"],
                        selection_common_candidate_ids=selection["common_candidate_ids"],
                        selection_metrics={
                            key: trial["metrics"] for key, trial in selection["trials"].items()
                        },
                        selection_full_target_metrics={
                            key: trial["full_target_metrics"]
                            for key, trial in selection["trials"].items()
                        },
                        label_rejections=label_rejections,
                        fit_status_counts=dict(
                            Counter(
                                f"{name}:{info['status']}"
                                for fold in result["folds"]
                                for name, info in fold["model_status"].items()
                            )
                        ),
                        selection_fit_status_counts=dict(
                            Counter(
                                f"{name}:{info['status']}"
                                for trial in selection["trials"].values()
                                for fold in trial["folds"]
                                for name, info in fold["model_status"].items()
                            )
                        ),
                    )
                    report_paths[scope] = directory / "predictions.jsonl.zst"
                    del result
                # This is the first possible odds read: every league and both
                # selection windows have already finished every model fit.
                pooled = []
                for scope, path in report_paths.items():
                    rows = _read_rows(path)
                    attach_market(session, rows, statistics)
                    write_rows(path, rows)
                    scopes[scope].update(group_summary(rows, protocol))
                    pooled.extend(_lightweight(rows))
                manifest = dict(
                    created_utc=datetime.now(UTC).isoformat(),
                    source_commit=source_commit,
                    code_commit=code_commit,
                    database_sha256=before[database.name],
                    input_files_sha256=before,
                    protocol_sha256=sha256(protocol_path),
                    protocol_version=protocol["protocol_version"],
                    source_files_sha256={
                        str(path.relative_to(ROOT)): sha256(path)
                        for path in sorted((ROOT / "src/zucai").rglob("*.py"))
                    },
                    dependencies={
                        name: version(name)
                        for name in ("numpy", "scipy", "pandas", "SQLAlchemy", "zstandard")
                    },
                    provider="fd",
                    leagues=selected,
                    host="dc-sot-v1",
                    candidate="dc-sot-injuries-v1",
                    read_only=True,
                    bet_allowed=False,
                    p2_ready=False,
                    production_adoption=False,
                    unseen_prospective_validation=False,
                    history_source="aligned_exact_ids",
                    feature_cutoff_basis="earliest_aligned_fixture_kickoff",
                    fit_cutoff_basis="monthly_selection_weekly_evaluation_before_fold_start",
                    injury_replay_assumption=protocol["injury_replay_assumption"],
                    availability_warning="训练历史名单/开球+3h为假设；目标只用实际捕获时刻，不声称在线冻结",
                    market_read_phase="after_all_league_fits_and_window_selection",
                    release_tag=release,
                    archive_name=archive_name,
                    **AVAILABILITY_MODES,
                )
                summary = dict(scopes=scopes, pooled=group_summary(pooled, protocol))
                session.rollback()
            engine.dispose()
            if _input_hashes(database) != before:
                raise RuntimeError("只读实验输入数据库或 WAL 哈希变化，拒绝归档与发布")
            write_json(evidence / "input_manifest.json", manifest)
            write_json(evidence / "protocol.json", protocol)
            write_json(evidence / "summary.json", summary)
            inner_sums = "".join(
                f"{sha256(path)}  {path.relative_to(evidence).as_posix()}\n"
                for path in sorted(evidence.rglob("*"))
                if path.is_file()
            )
            (evidence / "SHA256SUMS").write_text(inner_sums, encoding="utf-8")
            archive = stage / archive_name
            _zip_evidence(evidence, archive)
            archive_sha = sha256(archive)
            (stage / "SHA256SUMS").write_text(f"{archive_sha}  {archive_name}\n", encoding="utf-8")
            (stage / "README.md").write_text(
                _readme(
                    summary,
                    manifest,
                    archive_name=archive_name,
                    archive_sha=archive_sha,
                    release_tag=release,
                    repository=repository,
                ),
                encoding="utf-8",
            )
            # Reserve the output directory with O_EXCL-equivalent mkdir, so a
            # concurrent writer cannot have an empty directory silently replaced.
            out.mkdir(exist_ok=False)
            for path in stage.iterdir():
                os.rename(path, out / path.name)
            publication = None
            if publish:
                store = stats_release.ReleaseStore(repository=repository)
                asset = stats_release.upload_verified(store, release, out / archive_name)
                checksum = stats_release.upload_checksums(store, release, out, run_id, [asset])
                publication = dict(asset=asset, checksum_asset=checksum)
                write_json(out / "publication.json", publication)
            LOG.info("T38 archived %s; database unchanged %s", out, before[database.name])
            return dict(
                output=str(out),
                archive=str(out / archive_name),
                archive_sha256=archive_sha,
                database_sha256=before[database.name],
                source_commit=source_commit,
                status=summary["pooled"]["status"],
                common_targets=summary["pooled"]["common_targets"],
                publication=publication,
            )
    finally:
        engine.dispose()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--leagues", nargs="+")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--repository")
    parser.add_argument("--release")
    parser.add_argument("--run-id")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(
        args.db,
        args.out,
        source_commit=args.source_commit,
        leagues=args.leagues,
        publish=args.publish,
        repository=args.repository,
        release=args.release,
        run_id=args.run_id,
    )


if __name__ == "__main__":
    main()
