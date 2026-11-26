"""Pre-registered, offline T72 retrospective using publisher closing comparisons."""

import argparse
import hashlib
import json
import logging
from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from math import fsum, log
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from zucai.market_blend import blend_probabilities

LOG = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = ROOT / "docs/experiments/T72-team-market-blend-protocol.md"
ARCHIVE_COMMIT = "a87f27574cafb3297c7e68ca0363679cb471a203"
ARCHIVE_MANIFEST_SHA256 = "0352dfa9ba9416e4b51ee47bceda6952e841138877538c45048077ca84cfbd40"
PARAMETERS_SHA256 = "12cafa6c60adc19a6629da74f6cc40d360cb8c34ef3997da1f26a01099dfa888"
BASE_COMMIT = "8b7dd50feaaf83dac939e55dc433e723a993d490"
LEAGUES = ("英超", "西甲", "德甲", "意甲", "法甲", "葡超", "巴甲")
OUTCOMES = "hda"
WEIGHTS = tuple(i / 100 for i in range(101))


@dataclass(frozen=True)
class Phase:
    name: str
    start: str
    end: str


PHASES = (
    Phase("training", "2024-07-01T00:00:00+00:00", "2025-07-01T00:00:00+00:00"),
    Phase("validation", "2025-07-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    Phase("holdout", "2026-01-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00"),
)


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path, document):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(document, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write("\n")


def _stamp(text):
    instant = datetime.fromisoformat(text)
    if instant.utcoffset() is None:
        raise ValueError("时间必须带时区")
    return instant.astimezone(UTC)


def _exclusion(row):
    if row["actual"] is None:
        return "label_missing_at_phase_end"
    if row["team"] is None:
        return "team_forecast_missing"
    if row["closing"] is None:
        return "closing_quote_missing"
    return None


def _check_rows(rows, phase, seen):
    start, end = _stamp(phase.start), _stamp(phase.end)
    for row in rows:
        if not start <= _stamp(row["kickoff_utc"]) < end:
            raise ValueError("比赛开球时间不在该段的事前窗口")
        if row["match_id"] in seen:
            raise ValueError("回溯输入出现重复比赛编号")
        seen.add(row["match_id"])
        if row["actual"] is not None and row["actual"] not in tuple(OUTCOMES):
            raise ValueError("回溯结果须为单一胜平负类别")


def _loss(probabilities, outcome):
    return -log(max(probabilities[outcome], 1e-15))


def _select(rows):
    common = [row for row in rows if _exclusion(row) is None]
    if not common:
        raise ValueError("训练段没有共同样本")
    candidates = []
    for w in WEIGHTS:
        loss = fsum(
            _loss(blend_probabilities(row["team"], row["closing"], w), row["actual"])
            for row in common
        ) / len(common)
        candidates.append((loss, w))
    return min(candidates)[1], [{"weight": w, "log_loss": loss} for loss, w in candidates]


def _metrics(rows, key):
    if not rows:
        return dict(n=0, log_loss=None, brier=None, first_choice_hit=None)
    losses, errors, hits = [], [], []
    for row in rows:
        q, actual = row[key], row["actual"]
        losses.append(_loss(q, actual))
        errors.append(fsum((q[k] - (k == actual)) ** 2 for k in OUTCOMES))
        best = [k for k in OUTCOMES if q[k] == max(q.values())]
        hits.append((actual in best) / len(best))
    n = len(rows)
    return dict(
        n=n,
        log_loss=fsum(losses) / n,
        brier=fsum(errors) / n,
        first_choice_hit=fsum(hits) / n,
    )


def _phase_report(phase, rows, folds, weight, output):
    path = output / f"{phase.name}.jsonl"
    paired, excluded = [], Counter()
    with path.open("x", encoding="utf-8") as stream:
        for source in rows:
            reason = _exclusion(source)
            row = dict(source, exclusion=reason, blend=None)
            if reason is None:
                row["blend"] = blend_probabilities(row["team"], row["closing"], weight)
                paired.append(row)
            else:
                excluded[reason] += 1
            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
    return dict(
        start_utc=phase.start,
        end_utc=phase.end,
        total=len(rows),
        paired=len(paired),
        excluded=dict(excluded),
        metrics={key: _metrics(paired, key) for key in ("team", "closing", "blend")},
        rows_path=path.name,
        rows_sha256=_sha(path),
        folds=folds,
    )


def evaluate_phases(fetch, output):
    """A caller supplies one time slice at a time. Only the first can choose w."""
    fixed = output / "weight-selection.json"
    if fixed.exists():
        raise FileExistsError(fixed)
    seen, results = set(), {}
    for phase in PHASES:
        rows, folds = fetch(phase)
        _check_rows(rows, phase, seen)
        if phase.name == "training":
            weight, training_grid = _select(rows)
            _write(fixed, dict(weight=weight, training_grid=training_grid))
        results[phase.name] = _phase_report(phase, rows, folds, weight, output)
    return dict(weight=weight, weight_selection_sha256=_sha(fixed), phases=results)


def _prior_parameters(entry, league):
    from zucai.predict.base import utc
    from zucai.predict.seasons import season_bounds

    windows = [
        row
        for row in entry.get("evaluation_windows", [])
        if utc(row["selection_as_of_utc"]) <= season_bounds(row["validation_season"], league)[0]
    ]
    return (
        {row["validation_season"]: row["half_life_days"] for row in windows},
        {row["validation_season"]: row.get("draw_calibration", False) for row in windows},
    )


def _league_rows(database, phase, league, entry):
    import pandas as pd
    from sqlalchemy.orm import Session

    from zucai.db import get_engine
    from zucai.predict.backtest import evaluate, read_closing, read_history
    from zucai.predict.pooled import evaluation_history

    start, end = pd.Timestamp(phase.start), pd.Timestamp(phase.end)
    half_lives, calibration = _prior_parameters(entry, league)
    engine = get_engine(database)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA query_only=ON")
            with Session(bind=connection) as session:
                games = read_history(session, league, "fd,jc")
                predictions = evaluate(
                    games,
                    {},
                    since=start,
                    as_of=end,
                    half_life_days=90,
                    parameters_by_season=half_lives,
                    calibration_by_season=calibration,
                )
                labels = (
                    evaluation_history(games, end)
                    if games.attrs.get("linked_source_selection")
                    else games
                )
                labels = labels.loc[(labels.kickoff_utc >= start) & (labels.kickoff_utc < end)]
                # Attach the existing B365 / PS / Avg close by exact source match_id only.
                closing = read_closing(session, labels)
    finally:
        engine.dispose()
    evaluated = {row["match_id"]: row for row in predictions["predictions"]}
    cutoffs = defaultdict(list)
    for fold in predictions["folds"]:
        cutoffs[fold["season"]].append(_stamp(fold["cutoff_utc"]))
    rows = []
    for row in labels.itertuples():
        forecast = evaluated.get(row.match_id)
        clock = row.kickoff_utc.isoformat()
        available = row.available_utc
        actual = None
        if row.kickoff_utc < available < end:
            difference = row.home_goals - row.away_goals
            actual = "h" if difference > 0 else "a" if difference < 0 else "d"
        cutoff = None
        if forecast:
            stamps = sorted(cutoffs[forecast["season"]])
            offset = bisect_left(stamps, _stamp(clock))
            if offset == 0 or actual != OUTCOMES[forecast["actual"]]:
                raise ValueError("逐场回测的时点或赛果不一致")
            cutoff = stamps[offset - 1].isoformat()
        odds = closing.get(row.match_id)
        rows.append(
            dict(
                match_id=row.match_id,
                league=league,
                kickoff_utc=clock,
                team_cutoff_utc=cutoff,
                result_available_utc=available.isoformat() if not pd.isna(available) else None,
                result_available_source=row.available_source,
                actual=actual,
                team=(
                    dict(zip(OUTCOMES, forecast["probabilities"], strict=True))
                    if forecast
                    else None
                ),
                closing=dict(zip(OUTCOMES, odds, strict=True)) if odds is not None else None,
            )
        )
    return rows, predictions["folds"]


def run(archive, output, review_url):
    from zucai.cloud_feeds import restore_feeds
    from zucai.predict.parameters import read_parameters

    review = urlsplit(review_url)
    if review.scheme != "https" or not review.netloc or not review.path.strip("/"):
        raise ValueError("请提供本协议的 Claude Code 审查记录链接")
    archive, output = archive.resolve(), output.resolve()
    params = ROOT / "data/model/params.json"
    manifest = archive / "feed_manifest.json"
    if output.is_relative_to(archive):
        raise ValueError("报告目录须在原归档外")
    if _sha(manifest) != ARCHIVE_MANIFEST_SHA256 or _sha(params) != PARAMETERS_SHA256:
        raise ValueError("归档或参数与协议固定版本不符")
    configs, _ = read_parameters(params)
    output.mkdir(parents=True, exist_ok=False)
    with TemporaryDirectory(dir=output.parent) as temp:
        database = Path(temp) / "zucai.db"
        restore_feeds(archive, database)
        database_sha = _sha(database)

        def fetch(phase):
            rows, folds = [], {}
            for league in LEAGUES:
                observations, boundaries = _league_rows(
                    database, phase, league, configs["leagues"][league]
                )
                rows.extend(observations)
                folds[league] = boundaries
            return rows, folds

        report = evaluate_phases(fetch, output)
        if _sha(database) != database_sha:
            raise ValueError("回溯过程中副本发生变化")
    if _sha(manifest) != ARCHIVE_MANIFEST_SHA256 or _sha(params) != PARAMETERS_SHA256:
        raise ValueError("回溯过程中原件或参数发生变化")
    report.update(
        type="retrospective_publisher_closing",
        reviewed_protocol_url=review_url,
        protocol_sha256=_sha(PROTOCOL),
        archive_commit=ARCHIVE_COMMIT,
        archive_manifest_sha256=ARCHIVE_MANIFEST_SHA256,
        database_sha256=database_sha,
        base_commit=BASE_COMMIT,
        parameters_sha256=PARAMETERS_SHA256,
        sources=["fd", "jc"],
        leagues=list(LEAGUES),
    )
    _write(output / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True, help="协议指定的归档副本")
    parser.add_argument("--out", type=Path, required=True, help="新建报告目录")
    parser.add_argument("--review-url", required=True, help="Claude Code 的协议审查记录")
    args = parser.parse_args()
    report = run(args.archive, args.out, args.review_url)
    for name, phase in report["phases"].items():
        LOG.warning("%s，共同样本 %d / %d", name, phase["paired"], phase["total"])


if __name__ == "__main__":
    main()
