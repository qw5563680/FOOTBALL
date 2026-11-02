"""Score immutable prematch forecasts using only subsequently observed regular-time results."""

import hashlib
import json
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from zucai.cloud_feeds import digest
from zucai.forecast_archive import verify_forecasts
from zucai.report.render import load_batch
from zucai.sources.sporttery import BEIJING

OUTCOMES = "hda"


def instant(value):
    stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(stamp, datetime) or stamp.utcoffset() is None:
        raise ValueError("记分板时间必须包含时区")
    return stamp.astimezone(UTC)


def record_key(*values):
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def load_forecasts(archive: Path, as_of: datetime) -> list[dict]:
    manifest = verify_forecasts(archive)
    expected = {
        name for name in (manifest or {}).get("files", {}) if name.endswith("/forecast.json")
    }
    found = {p.relative_to(archive).as_posix() for p in archive.glob("forecasts/*/*/forecast.json")}
    if found != expected:
        raise ValueError("冻结预测目录与清单不一致")
    rows = []
    for name in sorted(expected):
        batch = load_batch(archive / name)
        if instant(batch["generated_utc"]) > instant(as_of):
            continue
        for prediction in batch["predictions"]:
            rows.append(
                {
                    **prediction,
                    **{k: batch[k] for k in ("batch_id", "generated_utc", "as_of_utc", "mode")},
                    "forecast_path": name,
                    "forecast_sha256": digest(archive / name),
                }
            )
    return rows


def read_results(database: Path) -> dict:
    with sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT m.match_id,m.source,m.league,m.kickoff_utc,m.status,"
            "h.name AS home,a.name AS away,r.home_goals,r.away_goals,"
            "r.available_utc,r.available_source FROM matches m "
            "JOIN teams h ON h.id=m.home_team_id JOIN teams a ON a.id=m.away_team_id "
            "LEFT JOIN results r ON r.match_id=m.match_id"
        )
        return {row["match_id"]: dict(row) for row in rows}


def prematch_rows(forecasts, as_of):
    seen = {}
    for row in forecasts:
        if row["status"] != "predicted" or row["mode"] != "prematch":
            continue
        generated, kickoff = instant(row["generated_utc"]), instant(row["kickoff_utc"])
        if not instant(row["as_of_utc"]) <= generated < kickoff or generated > instant(as_of):
            continue
        key = record_key(row["match_id"], row["batch_id"])
        if key in seen and seen[key] != row:
            raise ValueError("同一比赛/批次包含冲突预测")
        seen[key] = row
    return sorted(
        seen.values(), key=lambda r: (instant(r["generated_utc"]), r["batch_id"], r["match_id"])
    )


def frozen_market(row):
    market = row.get("market")
    if not market:
        return None
    prices = market.get("prices", {})
    if set(prices) != set(OUTCOMES) or any(
        type(p) not in (int, float) or not math.isfinite(p) or p <= 1 for p in prices.values()
    ):
        return None
    stamp = instant(market["snapshot_utc"])
    if not stamp <= instant(row["as_of_utc"]) <= instant(row["generated_utc"]) or stamp >= instant(
        row["kickoff_utc"]
    ):
        return None
    total = sum(1 / p for p in prices.values())
    return {
        "prices": dict(prices),
        "snapshot_utc": market["snapshot_utc"],
        "probabilities": {k: 1 / prices[k] / total for k in OUTCOMES},
    }


def result_reason(row, result, as_of):
    if result is None:
        return "result_missing"
    if any(row[k] != result[k] for k in ("source", "league", "home", "away")) or instant(
        row["kickoff_utc"]
    ) != instant(result["kickoff_utc"]):
        return "identity_changed"
    if result["status"] != "finished" or any(
        type(result[k]) is not int or result[k] < 0 for k in ("home_goals", "away_goals")
    ):
        return "result_incomplete"
    if not result.get("available_utc") or result.get("available_source") not in {
        "captured",
        "published",
    }:
        return "result_not_observed"
    available = instant(result["available_utc"])
    if available <= instant(row["generated_utc"]):
        return "result_not_after_forecast"
    if available > instant(as_of) or instant(row["kickoff_utc"]) >= instant(as_of):
        return "result_not_yet_available"
    return None


def outcome(result):
    h, a = result["home_goals"], result["away_goals"]
    return "h" if h > a else "a" if h < a else "d"


def _loss(probabilities, actual):
    return {
        "probabilities": dict(probabilities),
        "log_loss": -math.log(max(probabilities[actual], 1e-15)),
        "brier": sum((probabilities[k] - int(k == actual)) ** 2 for k in OUTCOMES),
    }


def score_forecasts(forecasts, results, existing, as_of):
    latest = {row["match_id"]: row for row in prematch_rows(forecasts, as_of)}
    scored = list(existing)
    known = {row["record_id"]: row for row in existing}
    if len(known) != len(existing):
        raise ValueError("已存评分含重复比赛/批次")
    excluded, corrections = Counter(), set()
    for row in latest.values():
        key = record_key(row["match_id"], row["batch_id"])
        result = results.get(row["match_id"])
        reason = result_reason(row, result, as_of)
        if reason:
            excluded[reason] += 1
            continue
        if key in known:
            if any(known[key][k] != result[k] for k in ("home_goals", "away_goals")):
                corrections.add(row["match_id"])
            continue
        actual = outcome(result)
        market = frozen_market(row)
        scored.append(
            {
                **{
                    k: row[k]
                    for k in (
                        "match_id",
                        "batch_id",
                        "generated_utc",
                        "kickoff_utc",
                        "league",
                        "model_name",
                        "model_version",
                        "model_probabilities",
                    )
                },
                "record_id": key,
                "forecast_path": row.get("forecast_path"),
                "forecast_sha256": row.get("forecast_sha256"),
                "p2_label": row.get("p2_label", "P2 实验"),
                "p2_ready": row.get("p2_ready", False),
                "available_utc": result["available_utc"],
                "available_source": result["available_source"],
                "home_goals": result["home_goals"],
                "away_goals": result["away_goals"],
                "actual": actual,
                "model": _loss(row["model_probabilities"], actual),
                "uniform": _loss(dict.fromkeys(OUTCOMES, 1 / 3), actual),
                "market": {**market, **_loss(market["probabilities"], actual)} if market else None,
            }
        )
    return scored, {
        "excluded": dict(excluded),
        "active_keys": [record_key(r["match_id"], r["batch_id"]) for r in latest.values()],
        "result_corrections": sorted(corrections),
        "selected_matches": len(latest),
        "newly_scored": len(scored) - len(existing),
    }


def _statistics(rows, column):
    paired = [r for r in rows if r[column] is not None]
    bins = [[] for _ in range(10)]
    for row in paired:
        for key, probability in row[column]["probabilities"].items():
            bins[min(int(probability * 10), 9)].append((probability, int(row["actual"] == key)))
    n = len(paired)

    def mean(values):
        return sum(values) / len(values) if values else None

    draw_mean = mean([r[column]["probabilities"]["d"] for r in paired])
    draw_rate = mean([int(r["actual"] == "d") for r in paired])
    return {
        "n": n,
        "log_loss": mean([r[column]["log_loss"] for r in paired]),
        "brier": mean([r[column]["brier"] for r in paired]),
        "draw_mean": draw_mean,
        "draw_rate": draw_rate,
        "draw_bias": draw_mean - draw_rate if n else None,
        "calibration": [
            {
                "lower": index / 10,
                "n": len(values),
                "mean_p": mean([v[0] for v in values]),
                "observed": mean([v[1] for v in values]),
            }
            for index, values in enumerate(bins)
        ],
    }


def _aggregate(rows):
    return {
        "n": len(rows),
        **{key: _statistics(rows, key) for key in ("model", "uniform", "market")},
        "market_paired_model": _statistics([r for r in rows if r["market"]], "model"),
    }


def _drift(rows, as_of):
    today = instant(as_of).astimezone(BEIJING).date()
    monday = today - timedelta(days=today.weekday())
    weeks = []
    for offset in range(4, 0, -1):
        start = monday - timedelta(weeks=offset)
        week = [
            r
            for r in rows
            if start
            <= instant(r["kickoff_utc"]).astimezone(BEIJING).date()
            < start + timedelta(days=7)
        ]
        stats = _statistics(week, "model")
        weeks.append({"week": start.isoformat(), "n": len(week), "draw_bias": stats["draw_bias"]})
    last = rows[-100:]
    draw = all(w["n"] and abs(w["draw_bias"]) > 0.05 for w in weeks)
    loss = len(last) == 100 and sum(r["model"]["log_loss"] for r in last) > sum(
        r["uniform"]["log_loss"] for r in last
    )
    return {
        "four_weeks_draw_bias": draw,
        "last_100_log_loss": loss,
        "warning": draw or loss,
        "weeks": weeks,
    }


def summarize_scores(scored, as_of, *, active_keys=None):
    selected = set(active_keys) if active_keys is not None else None
    latest = {}
    for row in sorted(scored, key=lambda r: (instant(r["generated_utc"]), r["batch_id"])):
        if instant(row["available_utc"]) <= instant(as_of) and (
            selected is None or row["record_id"] in selected
        ):
            latest[row["match_id"]] = row
    rows = sorted(latest.values(), key=lambda r: (instant(r["kickoff_utc"]), r["match_id"]))
    leagues, versions, months = defaultdict(list), defaultdict(list), defaultdict(list)
    for row in rows:
        leagues[row["league"]].append(row)
        versions[row["league"], row["model_version"]].append(row)
        months[instant(row["kickoff_utc"]).astimezone(BEIJING).strftime("%Y-%m")].append(row)
    result = {
        "total": _aggregate(rows),
        "leagues": {},
        "league_versions": [],
        "months": {k: _aggregate(v) for k, v in sorted(months.items())},
    }
    for league, values in sorted(leagues.items()):
        item = _aggregate(values)
        item.update(
            p2_labels=sorted({r["p2_label"] for r in values}),
            prospective_verified=len(values) >= 200
            and item["model"]["log_loss"] <= item["uniform"]["log_loss"],
            drift=_drift(values, as_of),
        )
        result["leagues"][league] = item
    for (league, version), values in sorted(versions.items()):
        result["league_versions"].append(
            {"league": league, "model_version": version, **_aggregate(values)}
        )
    return result
