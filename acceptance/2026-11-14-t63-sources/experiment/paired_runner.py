"""Run only the approved fixed-snapshot T63 comparison; never alter production data."""

import argparse
import json
import logging
import math
import socket
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.dont_write_bytecode = True
from prepare_census import (
    BASELINE,
    END,
    FDORG,
    FIXED,
    LEAGUES,
    START,
    git,
    no_network,
    save,
    sha,
    snapshot_hashes,
    stamp,
)

LOG = logging.getLogger(__name__)
PROTOCOL = "b19dc7c58bcc1c5e4d7f8ad3531fced0ee8dd0b4"
APPROVED_UTC = "2026-11-14T02:30:39+00:00"
BEIJING = ZoneInfo("Asia/Shanghai")


def score_version(row):
    return [
        row[key]
        for key in (
            "match_id",
            "home_goals",
            "away_goals",
            "ht_home_goals",
            "ht_away_goals",
        )
    ]


def git_bound(proof):
    # Git timestamps have one-second precision. The next second is the first
    # defensible subsecond bound; a batch in that same Git second cannot qualify.
    return stamp(proof["conservative_available_utc"]) + timedelta(seconds=1)


def select_rows(rows, cutoff, arm, proofs, events, identity_presence):
    """Strict availability/identity census, with all exclusions retained, without fitting."""
    instant = stamp(cutoff)
    selected, excluded = [], []
    for original in rows:
        row = dict(original)
        key, source = row["match_id"], row["source"]
        if source not in {"fd", "jc", "apif", "fdorg"}:
            continue
        if arm == "old" and source not in {"fd", "jc"}:
            continue
        if source == "fdorg" and row["league"] not in FDORG:
            continue
        if stamp(row["kickoff_utc"]) >= instant:
            continue
        reasons, proof = [], proofs.get(key)
        added = source in {"apif", "fdorg"}
        if added:
            # The fixed e5848e5 snapshot contains neither dedicated proof table.
            # Current aliases/counterparts and raw capture times are not approval.
            reasons.append("unproven_historical_identity")
            if row.get("counterpart_id"):
                reasons.append("unproven_historical_link")
        presence = identity_presence.get(key)
        if not added and (presence is None or git_bound(presence) >= instant):
            reasons.append("legacy_normalized_identity_not_present_before_cutoff")
        if proof is None:
            reasons.append("unproven_actual_result_availability")
        elif git_bound(proof) >= instant:
            reasons.append("current_result_version_not_archived_before_cutoff")
        prior_events = [
            event for event in events.get(key, []) if git_bound(event) < instant
        ]
        if not prior_events or prior_events[-1]["version"] != score_version(row):
            reasons.append("current_result_version_not_active_at_cutoff")
        available = stamp(row["available_utc"]) if row.get("available_utc") else None
        row["stored_available_utc"] = row.get("available_utc")
        row["stored_available_source"] = row.get("available_source")
        if available is None:
            reasons.append("missing_stored_availability")
        elif source == "fd" and stamp(row["kickoff_utc"]) >= stamp(START) and proof:
            available = max(available, git_bound(proof))
        if (
            available is not None
            and not stamp(row["kickoff_utc"]) < available < instant
        ):
            reasons.append("result_not_strictly_known_before_cutoff")
        if reasons:
            excluded.append(
                {"match_id": key, "source": source, "reasons": sorted(set(reasons))}
            )
            continue
        row["available_utc"] = available.isoformat()
        row["archive_commit_time_utc"] = proof["conservative_available_utc"]
        row["actual_archive_bound_utc"] = git_bound(proof).isoformat()
        selected.append(row)
    # No fuzzy/name/score deduplication. Existing model overlap guards remain active.
    return sorted(
        selected, key=lambda row: (row["kickoff_utc"], row["match_id"])
    ), excluded


def fit_selected(rows, cutoff, parameters, sources):
    import pandas as pd
    from zucai.predict.dixon_coles import DixonColes

    frame = pd.DataFrame(rows)
    if frame.empty:
        raise ValueError("no_eligible_training_rows")
    for key in ("kickoff_utc", "available_utc"):
        frame[key] = pd.to_datetime(frame[key], utc=True, format="ISO8601")
    frame.attrs["sources"] = sources
    model = DixonColes(
        parameters["half_life_days"],
        unknown_team_prior=True,
        draw_calibration=parameters["draw_calibration"],
    ).fit(frame, as_of=cutoff)
    if model.training_rows != len(rows):
        raise ValueError("Fitting changed the predeclared selected training set")
    return model


def had(matrix):
    import numpy as np

    probabilities = [
        float(np.tril(matrix, -1).sum()),
        float(np.trace(matrix)),
        float(np.triu(matrix, 1).sum()),
    ]
    if (
        not all(math.isfinite(p) and 0 <= p <= 1 for p in probabilities)
        or abs(sum(probabilities) - 1) > 1e-9
    ):
        raise ValueError("invalid_probability_vector")
    return probabilities


def outcome(result):
    home, away = result["home_goals"], result["away_goals"]
    return 0 if home > away else 1 if home == away else 2


def point_metrics(probabilities, label):
    if (
        len(probabilities) != 3
        or any(not math.isfinite(p) or p < 0 or p > 1 for p in probabilities)
        or abs(sum(probabilities) - 1) > 1e-9
    ):
        raise ValueError("Illegal probabilities")
    highest = max(probabilities)
    maxima = [i for i, value in enumerate(probabilities) if value == highest]
    return {
        "log_loss": -math.log(max(probabilities[label], 1e-15)),
        "brier": sum(
            (p - int(index == label)) ** 2 for index, p in enumerate(probabilities)
        ),
        "hit_rate": 1 / len(maxima) if label in maxima else 0.0,
    }


def paired_metrics(rows):
    import numpy as np

    paired = [row for row in rows if row.get("paired_scoring_eligible")]
    if not paired:
        return {"paired_n": 0, "status": "no_common_successful_predictions_and_labels"}
    values = {
        arm: np.asarray(
            [
                [
                    point_metrics(row[arm]["probabilities"], outcome(row["result"]))[
                        name
                    ]
                    for name in ("log_loss", "brier", "hit_rate")
                ]
                for row in paired
            ]
        )
        for arm in ("old", "new")
    }
    delta = values["new"] - values["old"]
    draws = np.random.default_rng(6301).integers(0, len(paired), (2000, len(paired)))
    intervals = np.percentile(delta[draws].mean(axis=1), (2.5, 97.5), axis=0)
    return {
        "paired_n": len(paired),
        "metrics": {
            name: {
                "old": float(values["old"][:, index].mean()),
                "new": float(values["new"][:, index].mean()),
                "new_minus_old": float(delta[:, index].mean()),
                "paired_descriptive_95_percentile_interval": [
                    float(value) for value in intervals[:, index]
                ],
            }
            for index, name in enumerate(("log_loss", "brier", "hit_rate"))
        },
        "bootstrap": {
            "resamples": 2000,
            "seed": 6301,
            "unit": "paired_target_id",
            "interval_is_descriptive_not_independent_significance": True,
        },
    }


def previous_coverage(target, old_ids, new_ids):
    result = {}
    for side, previous in target["previous"].items():
        if previous["status"] != "identified_legacy_previous":
            result[side] = {
                "status": "unknown",
                "reason": previous["status"],
                "old_in_training": None,
                "new_in_training": None,
            }
            continue
        row = previous["candidates"][0]
        result[side] = {
            "status": "identified",
            "match_id": row["match_id"],
            "source": row["source"],
            "old_in_training": None if old_ids is None else row["match_id"] in old_ids,
            "new_in_training": None if new_ids is None else row["match_id"] in new_ids,
            "stored_available_source": row["result"]["available_source"],
            "actual_conservative_archive_bound_before_cutoff": row[
                "archive_current_score_version_before_cutoff"
            ],
            "archive_bound_hours_after_kickoff": (
                stamp(row["current_score_version_proof"]["conservative_available_utc"])
                - stamp(row["kickoff_utc"])
            ).total_seconds()
            / 3600,
        }
    return result


def selection_difference(old, new):
    left = {row["match_id"]: row for row in old}
    right = {row["match_id"]: row for row in new}
    fields = (
        "home_goals",
        "away_goals",
        "home_team_id",
        "away_team_id",
        "kickoff_utc",
        "available_utc",
    )
    changed = [
        {
            "match_id": key,
            "fields": {
                field: {"old": left[key].get(field), "new": right[key].get(field)}
                for field in fields
                if left[key].get(field) != right[key].get(field)
            },
        }
        for key in sorted(left.keys() & right.keys())
        if any(left[key].get(field) != right[key].get(field) for field in fields)
    ]
    return {
        "old_only": sorted(left.keys() - right.keys()),
        "new_only": sorted(right.keys() - left.keys()),
        "changed_common_rows": changed,
    }


def summarize_previous(predictions):
    summaries = {}
    for arm in ("old", "new"):
        counts = Counter()
        for target in predictions:
            sides = list(target["previous_coverage"].values())
            for side in sides:
                if side["status"] == "unknown":
                    counts["unknown_team_occurrences"] += 1
                else:
                    counts["identified_team_occurrences"] += 1
                    if side[arm + "_in_training"] is None:
                        counts["fit_unavailable_team_occurrences"] += 1
                        continue
                    counts["in_training_team_occurrences"] += int(
                        side[arm + "_in_training"]
                    )
                    counts["actual_archive_bound_before_cutoff_team_occurrences"] += (
                        int(side["actual_conservative_archive_bound_before_cutoff"])
                    )
                    counts["stored_assumed_but_in_training_team_occurrences"] += int(
                        side["stored_available_source"] == "assumed"
                        and side[arm + "_in_training"]
                    )
            contained = sum(side[arm + "_in_training"] is True for side in sides)
            counts["matches_at_least_one_previous_in_training"] += int(contained >= 1)
            if any(side["status"] == "unknown" for side in sides):
                counts["matches_with_unknown_previous"] += 1
                continue
            if any(side[arm + "_in_training"] is None for side in sides):
                counts["matches_without_fitted_model"] += 1
                continue
            counts["matches_both_previous_in_training"] += int(contained == 2)
            counts["matches_neither_previous_in_training"] += int(contained == 0)
        summaries[arm] = dict(counts)
    changes = Counter()
    for target in predictions:
        known = [
            side
            for side in target["previous_coverage"].values()
            if side["status"] == "identified"
            and side["old_in_training"] is not None
            and side["new_in_training"] is not None
        ]
        for side in known:
            delta = int(side["new_in_training"]) - int(side["old_in_training"])
            changes[
                {
                    1: "increased_team_occurrences",
                    -1: "decreased_team_occurrences",
                    0: "unchanged_identified_team_occurrences",
                }[delta]
            ] += 1
        if len(known) == 2:
            delta = sum(
                int(side["new_in_training"]) - int(side["old_in_training"])
                for side in known
            )
            changes[
                "increased_matches"
                if delta > 0
                else "decreased_matches"
                if delta < 0
                else "unchanged_identified_matches"
            ] += 1
    return {
        "target_team_occurrences": len(predictions) * 2,
        "arms": summaries,
        "changes": dict(changes),
        "counting_note": "At least one in training includes a known contained side even when the other side is unknown; this count overlaps matches_with_unknown_previous. Neither requires both sides identified and both training statuses known.",
    }


def run(archive, code, preparation, output, expected_code, review):
    from zucai.cloud_feeds import restore_feeds
    from zucai.predict.parameters import resolve_parameters

    if output.exists():
        raise ValueError("Experiment output must be a new directory")
    if git(code, "rev-parse", "HEAD").decode().strip() != expected_code:
        raise ValueError("Execution code does not match the approved fixed SHA")
    if git(archive, "rev-parse", "HEAD").decode().strip() != FIXED:
        raise ValueError("Archive is not the fixed snapshot")
    if not review.is_file():
        raise ValueError("Protocol review evidence is required")
    census = json.loads((preparation / "census.json").read_bytes())
    protocol_raw = git(
        code, "show", f"{PROTOCOL}:docs/experiments/T63-source-latency-protocol.md"
    )
    if census["protocol_sha256"] != sha(protocol_raw):
        raise ValueError("Census was prepared under a different protocol")
    # Dedicated proof records are absent in this exact preregistered input. Refuse
    # unexpected files instead of silently using today's identity re-verification.
    if any(
        (archive / name).exists()
        for name in (
            "state/result_source_identities.jsonl",
            "state/result_source_links.jsonl",
        )
    ):
        raise ValueError("Unexpected proof inventory for the fixed snapshot")
    before = snapshot_hashes(archive)
    output.mkdir(parents=True)
    restored = output / "restored/zucai.db"
    restore_feeds(archive, restored)
    db_sha_before = sha(restored.read_bytes())
    with sqlite3.connect(f"file:{restored}?mode=ro&immutable=1", uri=True) as db:
        db.row_factory = sqlite3.Row
        logical = {}
        for table in ("teams", "matches", "results"):
            table_rows = [
                tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1")
            ]
            logical[table] = {
                "rows": len(table_rows),
                "sha256": sha(
                    json.dumps(
                        table_rows, ensure_ascii=False, separators=(",", ":")
                    ).encode()
                ),
            }
        rows = [
            dict(row)
            for row in db.execute(
                "SELECT m.*,r.home_goals,r.away_goals,r.ht_home_goals,r.ht_away_goals,r.available_utc,r.available_source,h.name AS home,a.name AS away FROM matches m JOIN results r USING(match_id) JOIN teams h ON h.id=m.home_team_id JOIN teams a ON a.id=m.away_team_id WHERE m.status='finished' ORDER BY m.kickoff_utc,m.match_id"
            )
        ]
    by_league = {
        league: [row for row in rows if row["league"] == league] for league in LEAGUES
    }
    targets = json.loads((preparation / "targets.json").read_bytes())
    proofs = json.loads(
        (preparation / "current-score-version-first-commits.json").read_bytes()
    )
    events = json.loads((preparation / "score-version-events.json").read_bytes())
    identities = json.loads(
        (preparation / "normalized-match-first-presence.json").read_bytes()
    )
    params_path = preparation / "baseline-params.json"
    if sha(params_path.read_bytes()) != sha(
        git(code, "show", f"{BASELINE}:data/model/params.json")
    ):
        raise ValueError("Parameters differ from fixed baseline")
    predictions, cache, exclusions_by_source, excluded_ids = (
        [],
        {},
        defaultdict(Counter),
        defaultdict(set),
    )
    training_dir = output / "training"
    training_dir.mkdir()
    historical_invariance = {}
    for league in LEAGUES:
        old, _ = select_rows(by_league[league], END, "old", proofs, events, identities)
        new, _ = select_rows(by_league[league], END, "new", proofs, events, identities)
        historical_invariance[league] = dict(
            selection_difference(old, new),
            old_selected_rows=len(old),
            new_selected_rows=len(new),
        )
    save(output / "historical-invariance-at-20261114.json", historical_invariance)
    for target in targets:
        row = {
            key: target[key]
            for key in (
                "match_id",
                "league",
                "kickoff_utc",
                "home",
                "away",
                "result",
                "label_status",
            )
        }
        cutoff = target["cutoff_utc"]
        row.update(
            validation_status="实验未验证",
            bet_allowed=False,
            cutoff_utc=cutoff,
            cutoff_beijing=stamp(cutoff).astimezone(BEIJING).isoformat()
            if cutoff
            else None,
            source_cutoff_evidence=target.get("publication_proof"),
        )
        training_ids = {}
        if cutoff:
            parameters = resolve_parameters(
                target["league"], as_of=cutoff, params_file=params_path
            )
            row["shared_parameters"] = parameters
        for arm in ("old", "new"):
            arm_record = {
                "validation_status": "实验未验证",
                "model_name": "dixon-coles",
                "model_version": None,
                "as_of_utc": cutoff,
                "as_of_beijing": row["cutoff_beijing"],
            }
            training_ids[arm] = set()
            if cutoff is None:
                arm_record.update(
                    status="not_generated", reason="no_verified_pre_kickoff_cutoff"
                )
                row[arm] = arm_record
                continue
            sources = (
                ["fd", "jc"]
                if arm == "old"
                else sorted(
                    {"fd", "jc", "apif"}
                    | ({"fdorg"} if target["league"] in FDORG else set())
                )
            )
            cache_key = (target["league"], cutoff, arm)
            if cache_key not in cache:
                selected, excluded = select_rows(
                    by_league[target["league"]], cutoff, arm, proofs, events, identities
                )
                for excluded_row in excluded:
                    for reason in excluded_row["reasons"]:
                        exclusions_by_source[excluded_row["source"]][reason] += 1
                        excluded_ids[excluded_row["source"], reason].add(
                            excluded_row["match_id"]
                        )
                key = sha(json.dumps(cache_key, ensure_ascii=False).encode())[:20]
                selected_path = training_dir / f"{key}-selected.json"
                save(selected_path, selected)
                save(training_dir / f"{key}-excluded.json", excluded)
                entry = {
                    "selected": selected,
                    "exclusions": excluded,
                    "training_path": str(selected_path.relative_to(output)),
                    "training_sha256": sha(selected_path.read_bytes()),
                }
                try:
                    model = fit_selected(selected, cutoff, parameters, sources)
                    model_path = output / "models" / f"{key}.pkl"
                    model.save(model_path)
                    entry.update(
                        model=model,
                        model_version=sha(model_path.read_bytes()),
                        model_path=str(model_path.relative_to(output)),
                    )
                except (ValueError, RuntimeError) as exc:
                    entry.update(error=type(exc).__name__, reason=str(exc))
                cache[cache_key] = entry
                LOG.info(
                    "Fitted %s %s %s: %s rows; %s",
                    target["league"],
                    arm,
                    cutoff,
                    len(selected),
                    "failed" if "error" in entry else "success",
                )
            fitted = cache[cache_key]
            training_ids[arm] = (
                None
                if "error" in fitted
                else {item["match_id"] for item in fitted["selected"]}
            )
            arm_record.update(
                training_path=fitted["training_path"],
                training_sha256=fitted["training_sha256"],
                training_rows=len(fitted["selected"]),
                training_source_counts=dict(
                    Counter(item["source"] for item in fitted["selected"])
                ),
            )
            if "error" in fitted:
                arm_record.update(
                    status="not_generated",
                    reason=fitted["reason"],
                    error=fitted["error"],
                )
            else:
                try:
                    matrix, metadata = fitted["model"].predict_with_metadata(
                        dict(target, source="jc")
                    )
                    arm_record.update(
                        status="predicted",
                        probabilities=had(matrix),
                        prediction_metadata=metadata,
                        model_version=fitted["model_version"],
                        model_path=fitted["model_path"],
                    )
                except (ValueError, RuntimeError) as exc:
                    arm_record.update(
                        status="not_generated",
                        reason=str(exc),
                        error=type(exc).__name__,
                    )
            row[arm] = arm_record
        row["previous_coverage"] = previous_coverage(
            target, training_ids["old"], training_ids["new"]
        )
        if cutoff:
            row["training_input_difference"] = selection_difference(
                cache[target["league"], cutoff, "old"]["selected"],
                cache[target["league"], cutoff, "new"]["selected"],
            )
        row["paired_scoring_eligible"] = row[
            "label_status"
        ] == "fixed_snapshot_result_before_label_cutoff" and all(
            row[arm]["status"] == "predicted" for arm in ("old", "new")
        )
        iso_year, iso_week, _ = stamp(row["kickoff_utc"]).isocalendar()
        row["iso_week"] = f"{iso_year}-W{iso_week:02d}"
        row["previous_coverage_group"] = (
            "unknown"
            if any(
                item["status"] == "unknown"
                for item in row["previous_coverage"].values()
            )
            else "increased"
            if any(
                item["new_in_training"] and not item["old_in_training"]
                for item in row["previous_coverage"].values()
            )
            else "unchanged"
        )
        predictions.append(row)
    save(output / "predictions.json", predictions)
    summary = {
        "validation_status": "实验未验证",
        "bet_allowed": False,
        "protocol_commit": PROTOCOL,
        "protocol_sha256": sha(protocol_raw),
        "protocol_approved_utc": APPROVED_UTC,
        "protocol_review_evidence_sha256": sha(review.read_bytes()),
        "execution_code_commit": expected_code,
        "fixed_archive_commit": FIXED,
        "baseline_commit": BASELINE,
        "baseline_parameters_sha256": sha(params_path.read_bytes()),
        "runner_sha256": sha(Path(__file__).read_bytes()),
        "generated_utc": datetime.now(UTC).isoformat(),
        "target_count": len(predictions),
        "label_count": sum(
            row["label_status"] == "fixed_snapshot_result_before_label_cutoff"
            for row in predictions
        ),
        "cutoff_count": sum(row["cutoff_utc"] is not None for row in predictions),
        "arm_success_counts": {
            arm: sum(row[arm]["status"] == "predicted" for row in predictions)
            for arm in ("old", "new")
        },
        "overall": paired_metrics(predictions),
        "by_league": {
            league: paired_metrics(
                [row for row in predictions if row["league"] == league]
            )
            for league in LEAGUES
        },
        "by_iso_week": {
            week: paired_metrics(
                [row for row in predictions if row["iso_week"] == week]
            )
            for week in sorted({row["iso_week"] for row in predictions})
        },
        "by_previous_coverage": {
            group: paired_metrics(
                [row for row in predictions if row["previous_coverage_group"] == group]
            )
            for group in sorted({row["previous_coverage_group"] for row in predictions})
        },
        "previous_game_coverage": summarize_previous(predictions),
        "historical_invariance_cutoff_utc": END,
        "historical_invariance": historical_invariance,
        "prediction_exclusions": {
            arm: dict(
                Counter(
                    row[arm].get("reason")
                    for row in predictions
                    if row[arm]["status"] != "predicted"
                )
            )
            for arm in ("old", "new")
        },
        "training_exclusions_unique_match_ids_by_source": {
            source: {
                reason: len(ids)
                for (provider, reason), ids in excluded_ids.items()
                if provider == source
            }
            for source in ("fd", "jc", "fdorg", "apif")
        },
        "fit_count": len(cache),
        "added_source_selected_rows_by_provider": {
            source: len(
                {
                    item["match_id"]
                    for (league, cutoff, arm), fit in cache.items()
                    if arm == "new"
                    for item in fit["selected"]
                    if item["source"] == source
                }
            )
            for source in ("apif", "fdorg")
        },
        "interpretation": "历史身份/关系证明不足，新增来源的质量和时效贡献未验收；两臂数字如相同，不能解读为来源无贡献。",
        "quality_improvement_confirmed": False,
        "limitations": [
            "共同样本低于预注册的500场声明门槛。",
            "固定快照没有新增来源专用带时标身份和关系快照，未以当前别名或counterpart补造。",
            "FD在异常停更期的覆盖差异不能外推常态。",
            "Git首次包含同比分版本仅给保守可知界，不是真实首次入库时间。",
            "未运行P2重认证；BET禁用。",
        ],
        "sports_network_requests": 0,
        "formal_key_uses": 0,
        "git_timestamp_precision_rule": "Keep original commit timestamp; use next whole second as conservative effective availability to avoid inventing subsecond order.",
        "restored_logical_tables": logical,
    }
    summary["source_archive_files_unchanged"] = before == snapshot_hashes(archive)
    summary["restored_database_sha256"] = db_sha_before
    summary["restored_database_unchanged_after_model_work"] = db_sha_before == sha(
        restored.read_bytes()
    )
    if (
        not summary["source_archive_files_unchanged"]
        or not summary["restored_database_unchanged_after_model_work"]
    ):
        raise RuntimeError("Source or restored DB changed")
    save(output / "summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("archive", "code", "preparation", "output", "review"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--code-sha", required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.code / "src"))
    socket.create_connection = no_network
    import requests

    requests.sessions.Session.request = no_network
    return run(
        args.archive,
        args.code,
        args.preparation,
        args.output,
        args.code_sha,
        args.review,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
