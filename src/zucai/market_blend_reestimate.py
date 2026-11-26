"""Propose the next T72 market weight from verified forward-only common scores."""

import argparse
import json
import logging
from datetime import UTC, datetime
from hashlib import sha256
from math import fsum
from pathlib import Path

from zucai.forecast_archive import _path
from zucai.market_blend import (
    MODEL_NAME,
    PROTOCOL_PATH,
    WEIGHT_REVIEW_STEP,
    blend_probabilities,
)
from zucai.market_blend_backtest import WEIGHTS, _loss
from zucai.scoreboard.score import instant, outcome
from zucai.scoreboard.team_market_blend import STOP_COMMON, checkpoint, read_scored

LOG = logging.getLogger(__name__)


def _digest(data):
    return sha256(data).hexdigest()


def _encode(document):
    return json.dumps(document, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()


def _selectable(row, scored_as_of):
    try:
        start, generated, published, kickoff, available, market, snapshot = (
            instant(row[key])
            for key in (
                "as_of_utc",
                "generated_utc",
                "published_utc",
                "kickoff_utc",
                "available_utc",
                "market_collected_utc",
                "market_snapshot_id",
            )
        )
        if (
            not snapshot
            == market
            <= start
            <= generated
            <= published
            < kickoff
            < available
            <= scored_as_of
            or row["available_source"] not in {"captured", "published"}
            or not row["match_id"].startswith("jc:")
            or any(type(row[k]) is not int or row[k] < 0 for k in ("home_goals", "away_goals"))
            or row["actual"] != outcome(row)
        ):
            raise ValueError
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("共同评分缺少可信的赛前快照或赛果时点") from exc
    fields = (
        "record_id",
        "match_id",
        "kickoff_utc",
        "as_of_utc",
        "generated_utc",
        "published_utc",
        "available_utc",
        "available_source",
        "market_snapshot_id",
        "market_collected_utc",
        "home_goals",
        "away_goals",
        "actual",
        "team",
        "market",
    )
    return (available, kickoff, row["match_id"]), {key: row[key] for key in fields}


def _score_evidence(archive, cutoff):
    raw = _path(archive, "scoreboard/summary.json").read_bytes()
    summary = json.loads(raw)
    at = instant(summary["as_of_utc"])
    if not at <= instant(summary["generated_utc"]) <= cutoff:
        raise ValueError("融合评分摘要晚于重估截点")
    state = summary.get("team_market_blend", {})
    if state.get("status") == "unavailable":
        raise ValueError("融合评分摘要暂不可用")
    records, score_raw = read_scored(archive, summary)
    index = {record["record_id"]: record for record in records}
    common = state.get("common_keys", [])
    if (
        not isinstance(common, list)
        or len(set(common)) != len(common)
        or state.get("common_n", 0) != len(common)
        or not set(common) <= (index.keys() & set(state.get("active_keys", [])))
    ):
        raise ValueError("融合共同评分摘要与原件不符")
    cohort = [index[key] for key in common]
    if len({row["match_id"] for row in cohort}) != len(cohort) or any(
        row.get(key) is None for row in cohort for key in ("team", "market", "fusion")
    ):
        raise ValueError("融合共同评分出现重复比赛或缺列")
    selected = [row for _, row in sorted(_selectable(row, at) for row in cohort)]
    terminal = state.get("checkpoint_300")
    if terminal is not None and checkpoint(records, terminal)["decision"]["stop_required"]:
        raise ValueError("融合实验已达到关停条件")
    if terminal is None and len(selected) >= STOP_COMMON:
        raise ValueError("融合 300 场检查点尚未核实")
    return selected, _digest(raw), _digest(score_raw), at


def _previous(path, cutoff):
    if path is None:
        return 0, None, None
    raw = path.read_bytes()
    value = json.loads(raw)
    number = value["checkpoint"]
    if (
        value.get("schema_version") != 1
        or value.get("status") != "proposed"
        or value.get("model_name") != MODEL_NAME
        or type(number) is not int
        or number < WEIGHT_REVIEW_STEP
        or number % WEIGHT_REVIEW_STEP
        or value.get("previous_checkpoint") != number - WEIGHT_REVIEW_STEP
        or not instant(value["as_of_utc"]) <= instant(value["generated_utc"]) <= cutoff
    ):
        raise ValueError("上次检查点的提案或时间无效")
    return number, value, _digest(raw)


def _grid(rows):
    return [
        (
            fsum(
                _loss(blend_probabilities(row["team"], row["market"], weight), row["actual"])
                for row in rows
            )
            / len(rows),
            weight,
        )
        for weight in WEIGHTS
    ]


def reestimate(archive, output, *, as_of, effective_from, previous=None, now=None):
    """Create a review proposal. Several checkpoints may share one read-only archive."""
    archive, output = archive.resolve(), output.resolve()
    if output.is_relative_to(archive) or output == PROTOCOL_PATH.resolve():
        raise ValueError("重估输出须在原归档及正式配置之外")
    if output.exists():
        raise FileExistsError(output)
    cutoff, effective, generated = map(instant, (as_of, effective_from, now or datetime.now(UTC)))
    if cutoff > generated or effective <= generated:
        raise ValueError("重估截点不能在未来，建议生效时间必须晚于生成时间")
    previous_number, prior, previous_sha = _previous(previous, cutoff)
    number = previous_number + WEIGHT_REVIEW_STEP
    available, summary_sha, score_sha, score_cutoff = _score_evidence(archive, cutoff)
    if len(available) < number:
        raise ValueError(f"已知共同样本 {len(available)} 场，下一检查点需 {number} 场")
    if prior and (
        prior.get("record_ids") != [row["record_id"] for row in available[:previous_number]]
        or prior.get("selection_sha256") != _digest(_encode(available[:previous_number]))
    ):
        raise ValueError("上次检查点的评分证据已变化")
    selected = available[:number]
    grid = _grid(selected)
    report = dict(
        schema_version=1,
        status="proposed",
        model_name=MODEL_NAME,
        weight_version=f"prospective-{number}",
        checkpoint=number,
        previous_checkpoint=previous_number,
        previous_report_sha256=previous_sha,
        as_of_utc=cutoff.isoformat(),
        generated_utc=generated.isoformat(),
        effective_from_utc=effective.isoformat(),
        score_as_of_utc=score_cutoff.isoformat(),
        score_sha256=score_sha,
        score_summary_sha256=summary_sha,
        selection_sha256=_digest(_encode(selected)),
        common_available=len(available),
        record_ids=[row["record_id"] for row in selected],
        market_weight=min(grid)[1],
        training_grid=[dict(weight=weight, log_loss=loss) for loss, weight in grid],
    )
    with output.open("xb") as stream:
        stream.write(_encode(report) + b"\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--as-of", required=True, help="显式重估截点（带时区）")
    parser.add_argument("--effective-from", required=True, help="拟议未来生效时间（带时区）")
    parser.add_argument("--previous", type=Path, help="上次检查点的已有提案")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    report = reestimate(
        args.archive,
        args.out,
        as_of=args.as_of,
        effective_from=args.effective_from,
        previous=args.previous,
    )
    LOG.info("已生成待审提案，第 %d 场检查点，w=%g", report["checkpoint"], report["market_weight"])


if __name__ == "__main__":
    main()
