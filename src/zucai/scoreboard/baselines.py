"""Score the historical baseline independently of any official model or paper account."""

import json
from collections import Counter, defaultdict
from datetime import timedelta
from statistics import fmean

from zucai.cloud_feeds import digest
from zucai.forecast_archive import _path
from zucai.forecast_coverage import read_coverage
from zucai.scoreboard.score import (
    _loss,
    instant,
    outcome,
    publication_reason,
    record_key,
    result_reason,
)

SCORES = "scoreboard/baseline.jsonl"
IDENTITY_FIELDS = ("source", "league", "home", "away", "kickoff_utc")


def read_scored(archive, summary):
    path = _path(archive, SCORES)
    sha = (summary or {}).get("coverage_baseline", {}).get("scored_sha256")
    if not path.exists() and sha is None:
        return [], b""
    if not path.is_file() or not sha or digest(path) != sha:
        raise ValueError("基准评分 SHA256 不符")
    raw = path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        raise ValueError("基准评分 JSONL 缺少行终止符")
    rows = [json.loads(line) for line in raw.splitlines()]
    if len({row["record_id"] for row in rows}) != len(rows):
        raise ValueError("已存基准评分含重复比赛/批次")
    return rows, raw


def _candidates(archive, manifest, publications, as_of, replay_days):
    for run in (manifest or {}).get("runs", {}).values():
        path = run.get("coverage_path")
        if not path:
            continue
        generated = instant(run["generated_utc"])
        if generated > as_of or (
            replay_days is not None and generated < as_of - timedelta(days=replay_days)
        ):
            continue
        document = read_coverage(archive, run)
        published = publications.get(path)
        for scope in ("jc", "sfc"):
            for group, rows in document[scope].items():
                for row in rows:
                    if row["formal"] or row["baseline"] is None or row.get("baseline_source"):
                        continue
                    yield {
                        **row["baseline"],
                        "match_id": row["match_id"],
                        "identity": row.get("identity"),
                        "kickoff_utc": row["kickoff_utc"],
                        "run_id": run["run_id"],
                        "generated_utc": document["generated_utc"],
                        "as_of_utc": document["as_of_utc"],
                        "published_utc": published["published_utc"] if published else None,
                        "forecast_path": path,
                        "forecast_sha256": manifest["files"][path],
                        "scope": scope,
                        "group": group,
                        "number": row["jc_num"] if scope == "jc" else row["seq"],
                    }


def _eligible(candidate, result, as_of):
    identity = candidate["identity"]
    if (
        not candidate["match_id"]
        or not identity
        or any(not identity.get(field) for field in IDENTITY_FIELDS)
    ):
        return "identity_unresolved"
    try:
        kickoff = instant(candidate["kickoff_utc"])
        generated = instant(candidate["generated_utc"])
        if not instant(candidate["as_of_utc"]) <= generated < kickoff:
            return "invalid_forecast_clock"
        if instant(identity["kickoff_utc"]) != kickoff:
            return "identity_changed"
        reason = publication_reason(candidate, as_of)
        if reason:
            return reason
        reason = result_reason({**candidate, **identity}, result, as_of)
        if reason:
            return reason
        if instant(result["available_utc"]) <= kickoff:
            return "result_not_after_kickoff"
    except (TypeError, ValueError, OverflowError):
        return "invalid_clock"
    return None


def score(archive, manifest, publications, results, existing, as_of, *, replay_days):
    """Select the last trustworthy baseline for a fixture and model version."""
    latest, excluded = {}, Counter()
    for candidate in _candidates(archive, manifest, publications, as_of, replay_days):
        result = results.get(candidate["match_id"])
        reason = _eligible(candidate, result, as_of)
        if reason:
            excluded[reason] += 1
            continue
        identity = candidate["identity"]
        fixture = (
            identity["league"],
            identity["home"],
            identity["away"],
            instant(identity["kickoff_utc"]).isoformat(),
            candidate["model_name"],
            candidate["model_version"],
        )
        order = (
            instant(candidate["generated_utc"]),
            instant(candidate["published_utc"]),
            candidate["run_id"],
            candidate["scope"] == "jc",
        )
        if fixture not in latest or latest[fixture][0] < order:
            latest[fixture] = (order, candidate, result)

    known = {row["record_id"]: row for row in existing}
    added, active, corrections = [], [], set()
    groups = defaultdict(list)
    for fixture, (_, candidate, result) in sorted(latest.items()):
        key = record_key(*fixture, candidate["run_id"], candidate["match_id"])
        active.append(key)
        goals = {field: result[field] for field in ("home_goals", "away_goals")}
        if key in known:
            record = known[key]
            if any(record[field] != value for field, value in goals.items()):
                corrections.add(candidate["match_id"])
        else:
            actual = outcome(result)
            record = {
                "record_id": key,
                **candidate,
                **goals,
                **_loss(candidate["probabilities"], actual),
                "actual": actual,
                "available_utc": result["available_utc"],
                "available_source": result["available_source"],
            }
            added.append(record)
        groups[fixture[-2:]].append(record)
    return added, {
        "active_keys": active,
        "models": [
            {
                "model_name": name,
                "model_version": version,
                "n": len(records),
                "log_loss": fmean(row["log_loss"] for row in records),
                "brier": fmean(row["brier"] for row in records),
            }
            for (name, version), records in sorted(groups.items())
        ],
        "excluded": dict(excluded),
        "result_corrections": sorted(corrections),
    }


def render(summary):
    if summary is None:
        return ""
    lines = [
        "\n## 基准（无球队信息）独立计分",
        "",
        "| 模型 | 版本 | 已计分 | Log Loss | Brier |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for model in summary["models"]:
        lines.append(
            f"| {model['model_name']} | {model['model_version']} | {model['n']} "
            f"| {model['log_loss']:.4f} | {model['brier']:.4f} |"
        )
    if not summary["models"]:
        lines.append("| 基准 | — | 0 | — | — |")
    return "\n".join(lines) + "\n"
