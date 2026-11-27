"""Score-only T100 retrospective, isolated from every production entry point."""

import argparse
import hashlib
import json
import re
import subprocess
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from math import fsum, inf, isfinite, log
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from zucai.db import get_engine
from zucai.models import Match
from zucai.predict.backtest import evaluate, read_history
from zucai.predict.base import utc
from zucai.predict.dixon_coles import InsufficientHistory
from zucai.predict.experiments.global_rating import RatingResult, candidate_parameters, fit_ratings
from zucai.predict.experiments.metrics import losses
from zucai.predict.parameters import read_parameters, resolve_parameters
from zucai.predict.pooled import (
    SOURCES,
    configuration,
    default_scope,
    evaluation_history,
    select_training_history,
    source_eligible_history,
)
from zucai.predict.seasons import season_bounds, season_for

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = Path("docs/experiments/T100-cross-league-rating-protocol.md")
PROTOCOL_COMMIT = "76f90f66fa80358ebd31f6cd6317d11992893af8"
PROTOCOL_SHA256 = "ff5bb0dcc3beb23415949203b84379541b6503070842bbd00059bbdd2cf99e6c"
ARCHIVE_COMMIT = "f87ae9232653add8d5da2f23138a028ca6896697"
ARCHIVE_SHA256 = "50e8e92726edab749c2592fc334e6208bbfdab4dea99c6c99f3050ad5b6aeb7e"
PARAMETERS = ROOT / "data/model/params.json"
APIF_POLICY = ROOT / "data/model/apif_history_training.json"
PARAMETERS_SHA256 = "12cafa6c60adc19a6629da74f6cc40d360cb8c34ef3997da1f26a01099dfa888"
APIF_POLICY_SHA256 = "2ee1b02a6434884d164347af45b21469f861e7594688f40d785a9441c75c46fb"


@dataclass(frozen=True)
class Phase:
    name: str
    start: pd.Timestamp
    end: pd.Timestamp


TRAINING = Phase("tuning", utc("2024-07-01T00:00+08:00"), utc("2026-07-01T00:00+08:00"))
VALIDATION = Phase("validation", TRAINING.end, utc("2026-11-27T04:51:35.591105Z"))


@dataclass(frozen=True)
class ReplayRow:
    match_id: str
    league: str
    kickoff: pd.Timestamp
    cutoff: pd.Timestamp
    home_id: int
    away_id: int
    actual: int | None
    official: tuple[float, float, float] | None
    default_strength: bool = False
    count_only: bool = False


def validate_review_url(value):
    link = urlsplit(value)
    if (
        link.scheme != "https"
        or link.netloc != "github.com"
        or not re.fullmatch(r"/qw5563680/FOOTBALL/pull/\d+", link.path, re.I)
        or not re.fullmatch(r"(?:issuecomment|pullrequestreview)-\d+", link.fragment)
    ):
        raise ValueError("须提供本项目 PR 上确认此协议版本的独立审查评论链接")
    return value


def evaluation_cutoff(kickoff, model_league):
    monday = kickoff.normalize() - pd.Timedelta(days=kickoff.day_of_week)
    boundary = season_bounds(season_for(kickoff, model_league), model_league)[0]
    return max(monday, boundary) - pd.Timedelta(microseconds=1)


def _sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _write_json(path, document):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(document, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write("\n")


def _write_rows(path, rows):
    with path.open("x", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
    return dict(path=path.name, sha256=_sha(path))


def _log_loss(probabilities, actual):
    if (
        len(probabilities) != 3
        or any(not isfinite(p) or p < 0 for p in probabilities)
        or abs(fsum(probabilities) - 1) > 1e-8
    ):
        raise ValueError("回溯概率必须是有效的胜平负分布")
    p = probabilities[actual]
    return -log(p) if p else inf


def _exclusion(row, *, tuning):
    if not tuning and row.cutoff < TRAINING.end:
        return "cutoff_before_parameter_freeze"
    if row.actual is None:
        return "label_missing_at_phase_end"
    if not tuning and row.official is None:
        return "official_forecast_missing"
    return None


def _phase_rows(load, phase, parameters):
    matches, history_at, evidence = load(phase)
    tuning = phase is TRAINING
    ids, groups = set(), defaultdict(list)
    for row in matches:
        if row.match_id in ids or not phase.start <= row.kickoff < phase.end:
            raise ValueError("回溯目标重复或不在协议窗口")
        if row.actual not in (None, 0, 1, 2) or not row.cutoff < row.kickoff:
            raise ValueError("回溯目标的赛果类别或截点无效")
        ids.add(row.match_id)
        groups[row.cutoff].append(row)
    rows = []
    unconfigured = Counter(total=0, both_rated=0, cutoff_before_parameter_freeze=0)
    for cutoff, fold in sorted(groups.items()):
        snapshots = (
            fit_ratings(history_at(cutoff), as_of=cutoff, parameters=parameters)
            if any(
                (
                    cutoff >= TRAINING.end
                    if row.count_only
                    else _exclusion(row, tuning=tuning) is None
                )
                for row in fold
            )
            else ()
        )
        for row in sorted(fold, key=lambda target: target.match_id):
            if row.count_only:
                unconfigured["total"] += 1
                if cutoff < TRAINING.end:
                    unconfigured["cutoff_before_parameter_freeze"] += 1
                elif row.home_id in snapshots[0].ratings and row.away_id in snapshots[0].ratings:
                    unconfigured["both_rated"] += 1
                continue
            reason = _exclusion(row, tuning=tuning)
            probabilities, missing = [], []
            if reason is None:
                for snapshot in snapshots:
                    result = snapshot.predict(row.home_id, row.away_id, kickoff_utc=row.kickoff)
                    if result.probabilities is None:
                        missing = list(result.missing_team_ids)
                        reason = "rating_unavailable"
                        break
                    probabilities.append(result.probabilities)
            rows.append(
                dict(
                    match_id=row.match_id,
                    league=row.league,
                    kickoff_utc=row.kickoff.isoformat(),
                    cutoff_utc=cutoff.isoformat(),
                    actual=row.actual,
                    official=row.official,
                    default_strength=row.default_strength,
                    excluded=reason,
                    missing_team_ids=missing,
                    rating_candidates=probabilities if reason is None else [],
                )
            )
    if not tuning:
        evidence["unconfigured_jc"] = dict(unconfigured)
    return rows, evidence


def _metrics(rows, probabilities):
    scored = []
    bins = {category: defaultdict(list) for category in "hda"}
    for row in rows:
        actual = row["actual"]
        values = probabilities(row)
        scored.append(losses(dict(actual=actual, variants={"p": {"probabilities": values}}), "p"))
        for index, category in enumerate("hda"):
            p = values[index]
            bins[category][min(9, int(10 * p))].append((p, actual == index))
    calibration = {}
    for category, block in bins.items():
        cells = [
            dict(
                lower=key / 10,
                n=len(items),
                mean_probability=fsum(item[0] for item in items) / len(items),
                actual_frequency=fsum(item[1] for item in items) / len(items),
            )
            for key, items in sorted(block.items())
        ]
        calibration[category] = dict(
            bins=cells,
            ece=(
                fsum(
                    cell["n"] * abs(cell["mean_probability"] - cell["actual_frequency"])
                    for cell in cells
                )
                / len(rows)
                if rows
                else None
            ),
        )
    mean = fsum(row["log_loss"] for row in scored) / len(rows) if rows else None
    return dict(
        log_loss="Infinity" if mean == inf else mean,
        brier=fsum(row["brier"] for row in scored) / len(rows) if rows else None,
        first_choice_hit=fsum(row["top_accuracy"] for row in scored) / len(rows) if rows else None,
        calibration=calibration,
    )


def _comparison(rows):
    paired = [row for row in rows if row["excluded"] is None]
    official_count = sum(row["actual"] is not None and row["official"] is not None for row in rows)
    official = _metrics(paired, lambda row: row["official"])
    rating = _metrics(paired, lambda row: row["rating"])
    a, b = rating["log_loss"], official["log_loss"]
    diff = a - b if isinstance(a, float) and isinstance(b, float) else None
    return dict(
        total=len(rows),
        official_scored=official_count,
        common=len(paired),
        coverage=len(paired) / official_count if official_count else None,
        official=official,
        rating=rating,
        log_loss_difference=diff,
    )


def _validation(rows, evidence, output):
    for row in rows:
        candidates = row.pop("rating_candidates")
        row["rating"] = candidates[0] if candidates else None
    overall = _comparison(rows)
    diff = overall["log_loss_difference"]
    rating_loss, official_loss = overall["rating"]["log_loss"], overall["official"]["log_loss"]
    if rating_loss == "Infinity" and isinstance(official_loss, float):
        gate = "stop"
    elif diff is None:
        gate = "unresolved"
    else:
        gate = "stop" if diff > 0.015 else "within_retrospective_limit"
    leagues = sorted({row["league"] for row in rows})
    return dict(
        overall=overall,
        by_league={
            league: _comparison([row for row in rows if row["league"] == league])
            for league in leagues
        },
        default_strength=_comparison([row for row in rows if row["default_strength"]]),
        excluded=dict(Counter(row["excluded"] for row in rows if row["excluded"])),
        gate=gate,
        diagnostics=evidence,
        rows=_write_rows(output / "validation.jsonl", rows),
    )


def run_phases(load, output):
    """Read tuning and validation separately, after persisting the chosen grid point."""
    parameters = candidate_parameters()
    training, diagnostic = _phase_rows(load, TRAINING, parameters)
    paired = [row for row in training if row["excluded"] is None]
    if not paired:
        raise ValueError("选参段没有共同有效比赛")
    losses = [
        fsum(_log_loss(row["rating_candidates"][i], row["actual"]) for row in paired) / len(paired)
        for i in range(len(parameters))
    ]
    best = min(range(len(parameters)), key=losses.__getitem__)
    if not isfinite(losses[best]):
        raise ValueError("没有有限的网格 Log Loss")
    selection = output / "parameter-selection.json"
    _write_json(
        selection,
        dict(
            phase_start_utc=TRAINING.start.isoformat(),
            phase_end_utc=TRAINING.end.isoformat(),
            written_utc=datetime.now(UTC).isoformat(),
            grid=[
                dict(parameters=asdict(parameter), log_loss=loss if isfinite(loss) else "Infinity")
                for parameter, loss in zip(parameters, losses, strict=True)
            ],
            selected=asdict(parameters[best]),
            total=len(training),
            common=len(paired),
            excluded=dict(Counter(row["excluded"] for row in training if row["excluded"])),
            diagnostics=diagnostic,
            rows=_write_rows(output / "tuning.jsonl", training),
        ),
    )
    validation, diagnostic = _phase_rows(load, VALIDATION, (parameters[best],))
    return dict(
        selection_sha256=_sha(selection),
        validation=_validation(validation, diagnostic, output),
    )


@contextmanager
def score_session(database, end):
    """Even eager history validation sees only physically eligible labels for this phase."""
    upper = end.to_pydatetime()

    def could_know(available, kickoff):
        if available is None:
            return False
        return datetime.fromisoformat(kickoff) < datetime.fromisoformat(available) < upper

    engine = get_engine(database)
    try:
        with engine.connect() as connection:
            connection.connection.driver_connection.create_function(
                "t100_could_know", 2, could_know
            )
            connection.exec_driver_sql("""
                CREATE TEMP VIEW results AS
                SELECT result.* FROM main.results AS result
                JOIN main.matches AS match ON match.match_id = result.match_id
                WHERE t100_could_know(result.available_utc, match.kickoff_utc)
            """)
            connection.exec_driver_sql("PRAGMA query_only=ON")
            with Session(bind=connection) as session:
                yield session
    finally:
        engine.dispose()


def _prior_parameters(league, entry):
    halves, calibration = {}, {}
    for window in entry.get("evaluation_windows", ()):
        season = window["validation_season"]
        if league in {"日职", "JPN"} and season == "2026":
            season = "2026T"
        if utc(window["selection_as_of_utc"]) <= season_bounds(season, league)[0]:
            halves[season] = window["half_life_days"]
            calibration[season] = window.get("draw_calibration", False)
    return halves, calibration


def _team_targets(session, phase, entries, diagnostics):
    targets = []
    for league, entry in entries.items():
        pool = configuration(league, entry)
        leagues = pool["leagues"] if pool else [league]
        scope = None
        if pool:
            scope = pool["training_leagues"] or default_scope(
                session, [name for name in entries if name != league], as_of=phase.end
            )
        archived = session.scalars(
            select(Match).where(
                Match.league.in_(leagues),
                Match.source.in_(entry["sources"]),
                Match.kickoff_utc >= phase.start.to_pydatetime(),
                Match.kickoff_utc < phase.end.to_pydatetime(),
                Match.status != "cancelled",
            )
        ).all()
        try:
            games = read_history(
                session, league, ",".join(entry["sources"]), training_leagues=scope
            )
            labels = (
                evaluation_history(games, phase.end)
                if games.attrs.get("linked_source_selection")
                else games
            )
        except (InsufficientHistory, ValueError) as exc:
            diagnostics["official_scope_failures"][league] = dict(
                reason=str(exc),
                archive_match_ids=sorted(row.match_id for row in archived),
            )
            continue
        forecasts, fitted_folds = {}, set()
        if phase is VALIDATION:
            halves, calibration = _prior_parameters(league, entry)
            baseline = evaluate(
                games,
                {},
                since=phase.start,
                as_of=phase.end,
                fold="weekly",
                half_life_days=(
                    resolve_parameters(league, as_of=phase.end, params_file=PARAMETERS)[
                        "half_life_days"
                    ]
                    if league == "欧国联"
                    else 90
                ),
                unknown_team_prior=league != "欧国联",
                parameters_by_season=halves,
                calibration_by_season=calibration,
                target_leagues=pool["leagues"] if pool else None,
            )
            fitted_folds = {
                utc(fold["cutoff_utc"]) for fold in baseline["folds"] if fold["status"] == "fitted"
            }
            forecasts = {row["match_id"]: row for row in baseline["predictions"]}
            diagnostics["official_folds"][league] = [
                {key: fold[key] for key in ("cutoff_utc", "status", "eligible", "skipped")}
                for fold in baseline["folds"]
            ]
        eligible = labels.loc[
            labels.league.isin(leagues)
            & (labels.kickoff_utc >= phase.start)
            & (labels.kickoff_utc < phase.end)
        ]
        for row in eligible.itertuples():
            cutoff = evaluation_cutoff(row.kickoff_utc, league)
            actual = None
            if row.kickoff_utc < row.available_utc < phase.end:
                actual = (
                    0
                    if row.home_goals > row.away_goals
                    else int(row.home_goals != row.away_goals) + 1
                )
            forecast = forecasts.pop(row.match_id, None)
            if forecast and (actual != forecast["actual"] or cutoff not in fitted_folds):
                raise ValueError(f"正式回放的时点或赛果不一致：{row.match_id}")
            targets.append(
                ReplayRow(
                    row.match_id,
                    row.league,
                    row.kickoff_utc,
                    cutoff,
                    int(row.home_id),
                    int(row.away_id),
                    actual,
                    tuple(map(float, forecast["probabilities"])) if forecast else None,
                    bool(forecast and forecast["unknown_team_prior"]),
                )
            )
        if forecasts:
            raise ValueError(f"正式回放存在无法按编号配对的预测：{league}")
        # These are raw archive IDs, so they are not silently treated as unique
        # matches or included in the current model's paired denominator.
        represented = set(eligible.match_id)
        diagnostics["unpaired_archive_ids"][league] = {
            reason: sorted(
                row.match_id
                for row in archived
                if row.match_id not in represented and (row.status == "finished") == finished
            )
            for finished, reason in (
                (False, "not_finished_in_archive"),
                (True, "no_selected_official_label"),
            )
        }
    return targets


def _rating_history_at(database, cutoff, leagues, diagnostics):
    """Select and quarantine each physical league independently at this exact cutoff."""
    with score_session(database, cutoff) as session:
        for league in leagues:
            try:
                games = read_history(
                    session,
                    source=",".join(SOURCES),
                    training_leagues=[league],
                    source_selection="cutoff",
                )
                known = source_eligible_history(games)["available_utc"]
                training = select_training_history(games.loc[known.index[known < cutoff]], cutoff)
            except InsufficientHistory:
                continue
            except ValueError as exc:
                diagnostics["rating_scope_failures"].append(
                    dict(league=league, cutoff_utc=cutoff.isoformat(), reason=str(exc))
                )
                continue
            for row in training.to_dict("records"):
                yield RatingResult.from_row(row)


def _load_phase(database, phase, entries):
    diagnostics = dict(
        official_scope_failures={},
        official_folds={},
        unpaired_archive_ids={},
        rating_scope_failures=[],
    )
    with score_session(database, phase.end) as session:
        matches = _team_targets(session, phase, entries, diagnostics)
        if phase is VALIDATION:
            targets = {
                target
                for model, entry in entries.items()
                for target in (configuration(model, entry) or {"leagues": [model]})["leagues"]
            }
            for match in session.scalars(
                select(Match).where(
                    Match.source == "jc",
                    Match.league.not_in(targets),
                    Match.status != "cancelled",
                    Match.kickoff_utc >= phase.start.to_pydatetime(),
                    Match.kickoff_utc < phase.end.to_pydatetime(),
                )
            ):
                kickoff = utc(match.kickoff_utc)
                matches.append(
                    ReplayRow(
                        match.match_id,
                        match.league,
                        kickoff,
                        evaluation_cutoff(kickoff, match.league),
                        match.home_team_id,
                        match.away_team_id,
                        None,
                        None,
                        count_only=True,
                    )
                )
        leagues = tuple(
            session.scalars(
                select(Match.league)
                .where(
                    Match.source.in_(SOURCES),
                    Match.kickoff_utc < phase.end.to_pydatetime(),
                )
                .distinct()
                .order_by(Match.league)
            )
        )
    return (
        matches,
        lambda cutoff: _rating_history_at(database, cutoff, leagues, diagnostics),
        diagnostics,
    )


def _git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def _check_inputs(archive, review_url, started):
    validate_review_url(review_url)
    for path, pinned in (
        (ROOT / PROTOCOL, PROTOCOL_SHA256),
        (archive / "feed_manifest.json", ARCHIVE_SHA256),
        (PARAMETERS, PARAMETERS_SHA256),
        (APIF_POLICY, APIF_POLICY_SHA256),
    ):
        if _sha(path) != pinned:
            raise ValueError(f"与协议锁定的 SHA-256 不一致：{path.name}")
    approved = _git("show", f"{PROTOCOL_COMMIT}:{PROTOCOL.as_posix()}")
    recorded = _git("show", "-s", "--format=%cI", PROTOCOL_COMMIT).decode().strip()
    if hashlib.sha256(approved).hexdigest() != PROTOCOL_SHA256 or utc(recorded) >= started:
        raise ValueError("协议提交与登记版本或回溯开始时间不符")
    return recorded


def run(archive, output, review_url):
    from zucai.cloud_feeds import restore_feeds

    started = datetime.now(UTC)
    protocol_committed = _check_inputs(archive, review_url, started)
    if output.resolve().is_relative_to(archive.resolve()):
        raise ValueError("回溯输出须放在归档副本以外的新目录")
    output.mkdir(parents=True, exist_ok=False)
    entries = read_parameters(PARAMETERS)[0]["leagues"]
    provenance = dict(
        archive_commit=ARCHIVE_COMMIT,
        feed_manifest_sha256=ARCHIVE_SHA256,
        parameters_sha256=PARAMETERS_SHA256,
        apif_policy_sha256=APIF_POLICY_SHA256,
        protocol_commit=PROTOCOL_COMMIT,
        protocol_committed_utc=utc(protocol_committed).isoformat(),
        protocol_sha256=PROTOCOL_SHA256,
        reviewed_protocol_url=review_url,
        run_commit=_git("rev-parse", "HEAD").decode().strip(),
        started_utc=started.isoformat(),
    )
    with TemporaryDirectory(prefix="t100-score-replay-") as working:
        database = Path(working) / "scores.db"
        restore_feeds(archive, database)
        provenance["database_sha256"] = _sha(database)
        replay = run_phases(lambda phase: _load_phase(database, phase, entries), output)
        if _sha(database) != provenance["database_sha256"]:
            raise ValueError("回溯期间只读库发生变化")
    report = {**provenance, **replay, "finished_utc": datetime.now(UTC).isoformat()}
    _write_json(output / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--review-url", required=True)
    args = parser.parse_args()
    run(args.archive, args.out, args.review_url)


if __name__ == "__main__":
    main()
