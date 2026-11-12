"""Restore an archive read-only, then publish only the independent scoreboard files."""

import argparse
import hashlib
import json
import logging
import os
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from zucai.cloud_feeds import digest, restore_feeds
from zucai.forecast_archive import _path, verify_forecasts
from zucai.forecast_publication import publication_times
from zucai.parlay import paper as parlay_paper
from zucai.parlay.archive import load_documents
from zucai.scoreboard import paper as paper_accounts
from zucai.scoreboard.experimental import (
    load_experiments,
    render_experiments,
    summarize_experiments,
)
from zucai.scoreboard.paper import (
    account_statistics,
    advance_ledger,
    profit_evidence,
)
from zucai.scoreboard.render import render_scoreboard
from zucai.scoreboard.score import (
    instant,
    load_forecasts,
    prematch_rows,
    read_results,
    record_key,
    score_forecasts,
    summarize_scores,
)
from zucai.scoreboard.sfc import read_settled, render_diagnostics, settle

LOG = logging.getLogger(__name__)
FILES = ("scored.jsonl", "ledger.jsonl", "scoreboard.md", "summary.json")


def _json(value):
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode()


def _jsonl(rows):
    return b"".join(
        (json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode()
        for row in rows
    )


def _append_ledger_view(original, rows):
    """Retain old receipts byte-for-byte and persist newly eligible identities once."""
    known = {json.loads(line)["record_id"] for line in original.splitlines()}
    added = [row for row in rows if row["record_id"] not in known]
    if not added:
        return original
    separator = b"\n" if original and not original.endswith(b"\n") else b""
    return original + separator + _jsonl(added)


def read_state(archive):
    paths = {name: _path(archive, "scoreboard/" + name) for name in FILES}
    if not paths["summary.json"].exists():
        if any(p.exists() for p in paths.values()):
            raise ValueError("记分板文件不完整，缺少 summary.json")
        return [], [], None, b""
    summary = json.loads(paths["summary.json"].read_bytes())
    if summary.get("schema_version") != 1 or set(summary.get("files", {})) != set(FILES[:-1]):
        raise ValueError("记分板清单格式无效")
    for name, sha in summary["files"].items():
        if not paths[name].is_file() or digest(paths[name]) != sha:
            raise ValueError(f"记分板 SHA256 不符：{name}")
    scored_raw = paths["scored.jsonl"].read_bytes()
    if scored_raw and not scored_raw.endswith(b"\n"):
        raise ValueError("评分 JSONL 缺少行终止符")
    scored = [json.loads(line) for line in scored_raw.splitlines()]
    ledger = [json.loads(line) for line in paths["ledger.jsonl"].read_bytes().splitlines()]
    return scored, ledger, summary, scored_raw


def _publish(archive, contents, *, sfc_raw=None, parlay_raw=None):
    root = _path(archive, "scoreboard")
    paths = {name: _path(root, name) for name in FILES}
    if parlay_raw is not None:
        contents = {**contents, "parlay-ledger": parlay_raw}
        paths["parlay-ledger"] = _path(archive, parlay_paper.LEDGER)
    if sfc_raw is not None:
        contents = {**contents, "sfc-settled": sfc_raw}
        paths["sfc-settled"] = _path(archive, "sfc/settled.jsonl")
    backups = {name: path.read_bytes() if path.exists() else None for name, path in paths.items()}
    try:
        with TemporaryDirectory(prefix=".scoreboard-", dir=archive) as temporary:
            stage = Path(temporary)
            for name, raw in contents.items():
                (stage / name).write_bytes(raw)
            root.mkdir(exist_ok=True)
            for name, path in paths.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                (stage / name).replace(path)
    except BaseException:
        for name, raw in backups.items():
            if raw is None:
                paths[name].unlink(missing_ok=True)
            else:
                paths[name].write_bytes(raw)
        if root.is_dir() and not any(root.iterdir()):
            root.rmdir()
        raise


def run(archive: Path, *, as_of=None, replay_days=None):
    archive = Path(archive).resolve()
    generated = datetime.now(UTC)
    at = instant(as_of) if as_of is not None else generated
    if at > generated:
        raise ValueError("记分板截点不能晚于实际运行时间")
    if replay_days is not None and not 1 <= replay_days <= 366:
        raise ValueError("--replay-days 必须在 1–366 之间")
    existing, ledger, previous, scored_raw = read_state(archive)
    settled, settled_raw = read_settled(archive, previous)
    if previous and instant(previous["as_of_utc"]) > at:
        raise ValueError("不能倒退已有记分板的截点，请用独立归档副本回放")
    forecast_manifest = verify_forecasts(archive)
    publications = publication_times(archive, forecast_manifest)
    parlay_documents = load_documents(archive, forecast_manifest, at, publications=publications)
    parlay_ledger = parlay_paper.read_ledger(archive, previous)
    has_parlays = bool(parlay_documents) or "parlay_sd" in (previous or {})
    forecasts = load_forecasts(archive, at, publications=publications)
    experimental, experiment_file_audit = load_experiments(
        archive, forecast_manifest, at, publications=publications
    )
    production_existing = [
        r for r in existing if r.get("validation_status") != "experimental_unadopted"
    ]
    experiment_existing = [
        r for r in existing if r.get("validation_status") == "experimental_unadopted"
    ]
    inputs = {
        name: digest(archive / name)
        for name in ("feed_manifest.json", "forecast_manifest.json")
        if (archive / name).exists()
    }
    selected = (
        forecasts
        if replay_days is None
        else [
            r for r in forecasts if instant(r["generated_utc"]) >= at - timedelta(days=replay_days)
        ]
    )
    with TemporaryDirectory(prefix="zucai-scoreboard-") as temporary:
        database = Path(temporary) / "data/zucai.db"
        restore_feeds(archive, database)
        before = digest(database)
        results = read_results(database)
        scored, audit = score_forecasts(selected, results, production_existing, at)
        experiment_selected = experimental
        if replay_days is not None:
            experiment_selected = [
                r
                for r in experiment_selected
                if instant(r["generated_utc"]) >= at - timedelta(days=replay_days)
            ]
        experiment_scored, experiment_audit = score_forecasts(
            experiment_selected, results, experiment_existing, at, experimental=True
        )
        experiment_audit["invalid_files"] = experiment_file_audit
        original_ledger_raw = (
            (archive / "scoreboard/ledger.jsonl").read_bytes() if previous else None
        )
        ledger = advance_ledger(selected, results, ledger, at)
        account_ledger, paper_publication_audit = paper_accounts.publication_view(
            forecasts, results, ledger, at
        )
        parlay_selected = [
            d
            for d in parlay_documents
            if replay_days is None
            or instant(d["generated_utc"]) >= at - timedelta(days=replay_days)
        ]
        original_parlay_raw = (
            (archive / parlay_paper.LEDGER).read_bytes()
            if (archive / parlay_paper.LEDGER).exists()
            else None
        )
        parlay_ledger = parlay_paper.advance_ledger(parlay_selected, results, parlay_ledger, at)
        parlay_view, parlay_publication_audit = parlay_paper.publication_view(
            parlay_documents, results, parlay_ledger, at
        )
        if digest(database) != before:
            raise ValueError("记分板计算改变了恢复库")
    latest = {r["match_id"]: r for r in prematch_rows(forecasts, at)}
    audit["active_keys"] = [record_key(r["match_id"], r["batch_id"]) for r in latest.values()]
    probability = summarize_scores(
        scored, at, active_keys=audit["active_keys"], forecast_rows=forecasts
    )
    starts = [
        instant(r["training_cutoff_utc"])
        for r in (forecast_manifest or {}).get("runs", {}).values()
        if r.get("experimental_protocol") == "t34a-strict-v1"
    ]
    experiments = summarize_experiments(
        experimental,
        experiment_scored,
        scored,
        audit["active_keys"],
        at,
        activated=min(starts) if starts else None,
    )
    # Include zero-sample leagues, including models whose batch had no fixtures.
    empty = summarize_scores([], at)["total"]
    for run_info in (forecast_manifest or {}).get("runs", {}).values():
        if instant(run_info["generated_utc"]) <= at:
            for league, model in run_info["leagues"].items():
                probability["leagues"].setdefault(
                    league,
                    {
                        **empty,
                        "p2_labels": [model.get("p2_label", "P2 实验")],
                        "prospective_verified": False,
                        "drift": {
                            "warning": False,
                            "four_weeks_draw_bias": False,
                            "last_100_log_loss": False,
                            "weeks": [],
                        },
                    },
                )
    accounts = account_statistics(account_ledger, at)
    audit["paper_publication"] = paper_publication_audit
    sfc_added, sfc_summary = settle(archive, results, settled, at)
    sfc_raw = settled_raw + _jsonl(sfc_added)
    sfc_summary["settled_sha256"] = hashlib.sha256(sfc_raw).hexdigest()
    audit["paper_skips"] = dict(Counter(r["reason"] for r in ledger if r["record_type"] == "skip"))
    audit["late_batches"] = sum(
        r["record_type"] == "batch" and r["reason"] == "late_batch_ignored" for r in ledger
    )
    audit["settlement_corrections"] = sorted(
        {
            e["match_id"]
            for r in ledger
            if r["record_type"] == "ticket" and r["settlement"]
            for e in r["settlement"]["results"]
            if e["match_id"] in results
            and any(e[k] != results[e["match_id"]][k] for k in ("home_goals", "away_goals"))
        }
    )
    summary = {
        "schema_version": 1,
        "generated_utc": generated.isoformat(),
        "as_of_utc": at.isoformat(),
        "bet_allowed": False,
        "default_threshold": "1.05",
        "edge_rule": "model_probability * frozen_sp > threshold",
        "replay_days": replay_days,
        "input_manifests": inputs,
        "probability": probability,
        "accounts": accounts,
        "sfc": sfc_summary,
        "audit": audit,
        "experiments": experiments,
        "experimental_audit": experiment_audit,
        "gates": {
            "prospective_verified": any(
                v["prospective_verified"] for v in probability["leagues"].values()
            ),
            "simulated_profit_evidence": profit_evidence(accounts["1.05"]["A"]),
        },
    }
    parlay_raw = (
        (
            _append_ledger_view(original_parlay_raw, parlay_view)
            if original_parlay_raw is not None and parlay_publication_audit["reselected_in_memory"]
            else _jsonl(parlay_ledger)
        )
        if has_parlays
        else None
    )
    if has_parlays:
        summary["parlay_sd"] = parlay_paper.summarize(parlay_view, results, at)
        summary["parlay_sd"]["publication_audit"] = parlay_publication_audit
        summary["parlay_sd"]["ledger_sha256"] = hashlib.sha256(parlay_raw).hexdigest()
    markdown = (
        render_scoreboard(summary)
        + render_diagnostics(summary)
        + render_experiments(experiments)
        + parlay_paper.render(summary.get("parlay_sd"))
    )
    contents = {
        "scored.jsonl": scored_raw
        + _jsonl(scored[len(production_existing) :])
        + _jsonl(experiment_scored[len(experiment_existing) :]),
        "ledger.jsonl": _append_ledger_view(original_ledger_raw, account_ledger)
        if original_ledger_raw is not None and paper_publication_audit["reselected_in_memory"]
        else _jsonl(ledger),
        "scoreboard.md": markdown.encode(),
    }
    summary["files"] = {name: hashlib.sha256(raw).hexdigest() for name, raw in contents.items()}
    contents["summary.json"] = _json(summary)
    if any(digest(archive / name) != sha for name, sha in inputs.items()):
        raise ValueError("输入归档在计算期间发生变化")
    _publish(archive, contents, sfc_raw=sfc_raw, parlay_raw=parlay_raw)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as output:
            output.write("\n".join(markdown.splitlines()[:60]) + "\n")
    LOG.info(
        "记分板：前瞻 %d 场，本次新增 %d；默认 A 账户 %d 注、回报率 %s",
        probability["total"]["n"],
        audit["newly_scored"],
        accounts["1.05"]["A"]["bets"],
        accounts["1.05"]["A"]["roi"],
    )
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run"])
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument(
        "--as-of", type=instant, help="可选历史截点；只能在独立副本回看，不能倒退已发布记分板"
    )
    parser.add_argument(
        "--replay-days", type=int, help="只对最近 N 天已冻结批次补记；不生成或伪造旧预测"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    run(args.archive, as_of=args.as_of, replay_days=args.replay_days)
