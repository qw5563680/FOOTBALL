"""Recompute the existing P2 gate in a new directory without selecting parameters."""

import argparse
import copy
import json
from datetime import UTC, datetime
from pathlib import Path

from zucai.predict.command import train
from zucai.predict.parameters import p2_gate, parameters_path, read_parameters
from zucai.predict.seasons import latest_complete_seasons
from zucai.predict.training_sources import actual_result_leagues, apif_history_sources


def revalidate(*, db, output, league, as_of, params_file=None):
    """A candidate artifact only; never overwrite the submitted parameter file."""
    output = Path(output).resolve()
    if output.exists() or output == Path(db).resolve():
        raise ValueError("重产目录必须是独立的新目录")
    if league not in actual_result_leagues():
        raise ValueError("此入口仅重产 T63 的15联赛证据")
    source_path = parameters_path(params_file)
    document, digest = read_parameters(source_path)
    source_bytes = source_path.read_bytes()
    entry = copy.deepcopy(document["leagues"][league])
    sources = sorted(apif_history_sources()[league])
    if sorted(entry["sources"]) != sources:
        raise ValueError("参数来源集合与T63显式政策不一致")
    output.mkdir(parents=True)
    result = train(
        db=db,
        league=league,
        source=",".join(sources),
        as_of=as_of,
        seasons=latest_complete_seasons(as_of, league),
        params_file=source_path,
        output=output / "model.pkl",
        predictions_out=output / "predictions.jsonl",
    )
    gate = p2_gate(result["backtest"], league=league, as_of=as_of)
    entry.update(
        generated_utc=datetime.now(UTC).isoformat(),
        database_sha256=result["training_database_sha256"],
        p2_ready=gate["p2_ready"],
        p2_evidence=gate,
    )
    candidate = copy.deepcopy(document)
    candidate["leagues"][league] = entry
    report = dict(
        league=league,
        source_params_sha256=digest,
        parameter_selection_performed=False,
        gate=gate,
        training=result,
        note="候选证据须审查；未修改正式参数；BET禁用",
    )
    for name, value in (("candidate-params.json", candidate), ("evaluation.json", report)):
        (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    if source_path.read_bytes() != source_bytes:
        raise ValueError("输入参数在运行期间改变")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--league", required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--params-file", type=Path)
    args = parser.parse_args(argv)
    revalidate(
        db=args.db,
        output=args.out,
        league=args.league,
        as_of=args.as_of,
        params_file=args.params_file,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
