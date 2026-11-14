"""Offline, read-only T33 experiments. Never registered in the daily prediction path."""

import argparse
import hashlib
import json
import logging
import math
import os
import sqlite3
import subprocess
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

import zstandard
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from zucai.features.stats import StatsFeatures
from zucai.models import Odds
from zucai.predict.backtest import read_history
from zucai.predict.base import utc
from zucai.predict.dixon_coles import InsufficientHistory
from zucai.predict.experiments.evaluate import evaluate_group, load_protocol
from zucai.predict.experiments.groups import write_group_report
from zucai.predict.experiments.metrics import VARIANTS, diagnostics, is_common_experiment, summarize
from zucai.result_source_groups import known_result_groups
from zucai.result_source_links import load_result_links

LOG = logging.getLogger(__name__)
DEFAULT_PROTOCOL = (
    Path(__file__).resolve().parents[4] / "docs/experiments/T33-evaluation-protocol.json"
)


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def serializable(value):
    """JSON has no infinity: preserve its meaning explicitly, never clip the loss."""
    if isinstance(value, dict):
        return {str(k): serializable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [serializable(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return "Infinity" if value > 0 else "-Infinity" if value < 0 else "NaN"
    return value


def write_json(path, value):
    Path(path).write_text(
        json.dumps(serializable(value), ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def write_rows(path, rows):
    with (
        Path(path).open("wb") as target,
        zstandard.ZstdCompressor(level=9).stream_writer(target) as out,
    ):
        for row in rows:
            out.write(
                (
                    json.dumps(
                        serializable(row),
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode()
            )


def attach_market(session, rows, features):
    """Post-fit comparison only: declared, identity-checked links to a jc fixture."""
    proofs = load_result_links(session)
    identity_matches = getattr(features, "_original", features).matches
    reverse = {}
    for game in features.matches.values():
        if game["source"] == "jc" and game.get("counterpart_id"):
            reverse.setdefault(game["counterpart_id"], []).append(game)
    links = {}
    for row_index, row in enumerate(rows):
        original = features.matches[row["match_id"]]
        linked = reverse.get(row["match_id"], [])[:]
        direct = features.matches.get(original.get("counterpart_id"))
        if direct and direct["source"] == "jc":
            linked.append(direct)
        cutoff = min(utc(row["feature_cutoff"]), original["kickoff_utc"])
        members = known_result_groups(identity_matches, proofs, cutoff).get(row["match_id"], ())
        for key in members:
            other = features.matches[key]
            if other["source"] == "jc" and key in known_result_groups(
                identity_matches, proofs, min(cutoff, other["kickoff_utc"])
            ).get(row["match_id"], ()):
                linked.append(other)
        valid = {
            g["match_id"]: g
            for g in linked
            if g["league"] == original["league"]
            and g["home_team_id"] == original["home_team_id"]
            and g["away_team_id"] == original["away_team_id"]
            and abs((g["kickoff_utc"] - original["kickoff_utc"]).total_seconds()) <= 10800
        }
        if len(valid) == 1:
            game = next(iter(valid.values()))
            links[row_index] = game
    quotes = {}
    ids = {game["match_id"] for game in links.values()}
    if ids:
        query = select(Odds.match_id, Odds.snapshot_utc, Odds.selection, Odds.price).where(
            Odds.match_id.in_(ids), Odds.pool == "had", Odds.line == 0
        )
        for key, stamp, selection, price in session.execute(query):
            if selection in "hda":
                quotes.setdefault(key, {}).setdefault(utc(stamp), {})[selection] = price
    for row_index, row in enumerate(rows):
        row["variants"]["market"] = None
        game = links.get(row_index)
        if game is None or row["actual"] is None:
            continue
        cutoff = min(utc(row["feature_cutoff"]), game["kickoff_utc"])
        eligible = [
            (stamp, p)
            for stamp, p in quotes.get(game["match_id"], {}).items()
            if stamp < cutoff
            and set(p) == set("hda")
            and all(math.isfinite(v) and v > 1 for v in p.values())
        ]
        if not eligible:
            continue
        stamp, prices = max(eligible, key=lambda pair: pair[0])
        inverse = [1 / prices[k] for k in "hda"]
        row["variants"]["market"] = dict(
            probabilities=[v / sum(inverse) for v in inverse],
            model_type="MARKET",
            match_id=game["match_id"],
            snapshot_utc=stamp.isoformat(),
            method="jc_had_inverse_odds_normalization",
            prices=prices,
        )


def group_summary(rows, protocol):
    options = dict(
        replicates=protocol["bootstrap"]["replicates"], seed=protocol["bootstrap"]["seed"]
    )
    summary = diagnostics(rows, **options)
    common = [row for row in rows if is_common_experiment(row)]
    summary["common_experiment"] = {name: summarize(common, name) for name in VARIANTS}
    summary["seasons"] = {
        season: diagnostics([r for r in rows if r["season"] == season], **options)
        for season in sorted({r["season"] for r in rows})
    }
    summary["fallback_reasons"] = {
        reason: sum(r.get("fallback_reason") == reason for r in rows)
        for reason in sorted({r["fallback_reason"] for r in rows if r.get("fallback_reason")})
    }
    return summary


def run(
    database,
    out,
    *,
    protocol_path=DEFAULT_PROTOCOL,
    providers=None,
    leagues=None,
    source_commit="unknown",
    code_commit=None,
):
    database, out = Path(database).resolve(strict=True), Path(out).resolve()
    if out.exists():
        raise FileExistsError("实验输出已存在，禁止覆盖冻结结果")
    protocol = load_protocol(protocol_path)
    providers = providers or list(protocol["providers"])
    if not providers or any(p not in protocol["providers"] for p in providers):
        raise ValueError("供应商超出冻结协议")
    allowed = {league for p in providers for league in protocol["providers"][p]["leagues"]}
    if leagues and not set(leagues).issubset(allowed):
        raise ValueError("联赛超出冻结协议")
    before = sha256(database)
    uri = database.as_uri() + "?mode=ro"
    engine = create_engine("sqlite+pysqlite://", creator=lambda: sqlite3.connect(uri, uri=True))
    code_commit = (
        code_commit or subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        with TemporaryDirectory(prefix=".t33-", dir=out.parent) as temp, Session(engine) as session:
            session.connection().exec_driver_sql("PRAGMA query_only=ON")
            destination = Path(temp)
            combined, reports, counts = {}, {}, {}
            for provider in providers:
                features = StatsFeatures(session, provider=provider)
                counts[provider] = sum(len(v) for v in features.statistics.values())
                for league in protocol["providers"][provider]["leagues"]:
                    if leagues and league not in leagues:
                        continue
                    scope = f"{provider}-{league}"
                    try:
                        history = read_history(session, league=league, source="fd")
                    except InsufficientHistory as exc:
                        reports[scope] = dict(status="not_covered", reason=str(exc))
                        continue
                    LOG.info("T33 starting %s", scope)
                    output = evaluate_group(history, features, protocol=protocol)
                    reports[scope] = {}
                    for mode, result in output.items():
                        # No market can reach the fitter or the window selector.
                        attach_market(session, result["predictions"], features)
                        combined.setdefault((provider, mode), []).extend(result["predictions"])
                        prefix = f"{scope}-{mode}"
                        write_rows(
                            destination / f"{prefix}-predictions.jsonl.zst", result["predictions"]
                        )
                        write_rows(destination / f"{prefix}-folds.jsonl.zst", result["folds"])
                        selection = result["selection"]
                        for window, trial in selection["trials"].items():
                            write_rows(
                                destination / f"{prefix}-selection-{window}.jsonl.zst",
                                trial["predictions"],
                            )
                            write_rows(
                                destination / f"{prefix}-selection-{window}-folds.jsonl.zst",
                                trial["folds"],
                            )
                        reports[scope][mode] = dict(
                            selected_window=selection["window"],
                            selection_evidence_sufficient=selection["evidence_sufficient"],
                            selection_reason=selection["reason"],
                            selection_metrics={
                                n: t["metrics"] for n, t in selection["trials"].items()
                            },
                            **group_summary(result["predictions"], protocol),
                        )
            summaries = {
                f"{provider}-{mode}": group_summary(rows, protocol)
                for (provider, mode), rows in combined.items()
            }
            manifest = dict(
                created_utc=datetime.now(UTC).isoformat(),
                source_commit=source_commit,
                code_commit=code_commit,
                database_sha256=before,
                protocol_sha256=sha256(protocol_path),
                feature_stat_rows_loaded=counts,
                protocol_version=protocol["protocol_version"],
                source_files_sha256={
                    str(p.relative_to(Path(__file__).resolve().parents[3])): sha256(p)
                    for p in sorted(Path(__file__).resolve().parents[2].rglob("*.py"))
                },
                dependencies={
                    name: version(name) for name in ("numpy", "scipy", "pandas", "penaltyblog")
                },
                providers=providers,
                leagues=leagues,
                read_only=True,
                production_adoption=False,
                bet_allowed=False,
                unseen_prospective_validation=False,
                availability_warning="backfill_replay是假设历史可用，不能证明当时版本；strict排除回填",
                baseline=(
                    "unchanged DixonColes implementation, fixed90; not exact online tuned artifact"
                ),
            )
            write_json(destination / "input_manifest.json", manifest)
            write_json(destination / "protocol.json", protocol)
            write_json(
                destination / "summary.json",
                dict(
                    scopes=reports,
                    pooled=summaries,
                    cups=dict(
                        status="not_covered",
                        reason="本轮杯赛统计不足；未拟合杯赛技术模型或转用国内结论",
                    ),
                ),
            )
            write_group_report(destination, generator_commit=code_commit)
            session.rollback()
            if sha256(database) != before:
                raise RuntimeError("只读实验的输入库哈希发生变化，拒绝发布")
            checksums = [f"{sha256(p)}  {p.name}\n" for p in sorted(destination.iterdir())]
            (destination / "SHA256SUMS").write_text("".join(checksums), encoding="utf-8")
            os.rename(destination, out)
            LOG.info("T33 archived %s; database unchanged %s", out, before)
            return dict(
                output=str(out),
                source_commit=source_commit,
                database_sha256=before,
                scopes=list(reports),
            )
    finally:
        engine.dispose()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--protocol", default=str(DEFAULT_PROTOCOL))
    parser.add_argument("--provider", nargs="+", choices=("fd", "apif"))
    parser.add_argument("--leagues", nargs="+")
    parser.add_argument("--source-commit", default="unknown")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(
        args.db,
        args.out,
        protocol_path=args.protocol,
        providers=args.provider,
        leagues=args.leagues,
        source_commit=args.source_commit,
    )


if __name__ == "__main__":
    main()
