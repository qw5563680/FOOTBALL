"""Read-only facts for the daily presentation; no predictions, scoring or ticket decisions."""

import csv
import json
from datetime import UTC, date, timedelta
from decimal import Decimal

from zucai.forecast_archive import _path
from zucai.scoreboard.score import instant, prematch_rows
from zucai.sources.sporttery import BEIJING, LEGACY_AUDIT_MONTH


def _table(archive, feed, name):
    path = f"state/{name}.jsonl"
    if path not in feed["files"]:
        return
    with _path(archive, path).open(encoding="utf-8") as stream:
        for line in stream:
            yield dict(zip(feed["columns"][name], json.loads(line), strict=True))


def archived_matches(archive, feed):
    teams = {row["id"]: row["name"] for row in _table(archive, feed, "teams")}
    matches = {
        row["match_id"]: {
            **row,
            "home": teams.get(row["home_team_id"]),
            "away": teams.get(row["away_team_id"]),
        }
        for row in _table(archive, feed, "matches")
        if row["source"] == "jc"
    }
    for row in _table(archive, feed, "results"):
        if row["match_id"] in matches:
            matches[row["match_id"]]["result"] = row
    return matches


def audit_rows(archive, feed, name, now):
    now = instant(now)
    start = week_start(now).astimezone(UTC).date()
    months = {
        (start + timedelta(days=offset)).strftime("%Y-%m")
        for offset in range((now.date() - start).days + 1)
    }
    paths = [f"state/audit/{name}/{month}.csv" for month in sorted(months)]
    if LEGACY_AUDIT_MONTH in months:
        paths.insert(0, f"state/audit/{name}.csv")
    paths = [path for path in paths if path in feed["files"]]
    if not paths:
        return None
    rows = []
    for path in paths:
        with _path(archive, path).open(encoding="utf-8", newline="") as stream:
            rows.extend(csv.DictReader(stream, strict=True))
    return [row for row in rows if row.get("captured_utc") and instant(row["captured_utc"]) <= now]


def frozen_rows(batches, matches, now):
    rows = []
    for _, path, batch in batches:
        for prediction in batch["predictions"]:
            row = {**matches.get(prediction["match_id"], {}), **prediction}
            row.update({k: batch[k] for k in ("batch_id", "generated_utc", "as_of_utc", "mode")})
            row["forecast_path"] = path
            rows.append(row)
    latest = {row["match_id"]: row for row in prematch_rows(rows, now)}
    observations = {
        row["match_id"]: row
        for row in sorted(rows, key=lambda r: (instant(r["generated_utc"]), r["batch_id"]))
    }
    return latest, observations


def prediction_window(batches, now):
    """Plan is today's + tomorrow's Beijing dates, independently of batch success."""
    today = instant(now).astimezone(BEIJING).date()
    planned = [today.isoformat(), (today + timedelta(days=1)).isoformat()]
    successful = set()
    for _, _, batch in batches:
        if batch.get("mode") != "prematch" or instant(batch["generated_utc"]) > instant(now):
            continue
        declared = batch.get("input", {}).get("kickoff_date_beijing")
        if declared in planned:
            successful.add(declared)
    return dict(
        planned_dates=planned,
        successful_dates=sorted(successful),
        missing_batch_dates=sorted(set(planned) - successful),
    )


def kickoff_day(row):
    try:
        return instant(row.get("kickoff_utc")).astimezone(BEIJING).date()
    except (TypeError, ValueError):
        return None


def catalog_gaps(catalog, skipped, predicted_ids, observations, *, window=None, known_ids=()):
    """Only a preserved catalog proves absence, never a hole in a numeric sequence."""
    if catalog is None:
        return None
    by_number = {}
    for row in sorted(catalog, key=lambda r: r["captured_utc"]):
        by_number[row["business_date"], row["jc_num"]] = row
    explanations = {}
    for row in sorted(skipped or [], key=lambda r: r["captured_utc"]):
        explanations[row["business_date"], row["jc_num"], row["match_id"]] = row
    gaps = []
    for key, row in sorted(by_number.items()):
        if row["match_id"] in predicted_ids:
            continue
        skipped_row = explanations.get((*key, row["match_id"]))
        observed = observations.get(row["match_id"])
        reason = (
            skipped_row["reason"]
            if skipped_row
            else "no_prematch"
            if observed or row["match_id"] in known_ids
            else "unknown"
        )
        day = kickoff_day(row)
        category = "unexplained" if reason == "unknown" else "missing"
        expected = None
        missing_batch = False
        if window is not None:
            if day is None:
                category = "unknown_window"
            elif day > date.fromisoformat(max(window["planned_dates"])):
                category = "outside_window"
                expected = (day - timedelta(days=1)).isoformat()
            else:
                missing_batch = day.isoformat() in window["missing_batch_dates"]
        gaps.append(
            {
                **row,
                "confirmed": category in {"missing", "unexplained"},
                "reason": reason,
                "skip_evidence": skipped_row,
                "category": category,
                "kickoff_date_beijing": day.isoformat() if day else None,
                "expected_window_date": expected,
                "batch_missing": missing_batch,
            }
        )
    return gaps


def pending_tickets(ledger, now):
    result = {account: [] for account in "ABCDE"}
    for ticket in ledger:
        if (
            ticket.get("record_type") != "ticket"
            or ticket["threshold"] != "1.05"
            or instant(ticket["decision_utc"]) > now
        ):
            continue
        settled = ticket.get("settlement") or {}
        if settled.get("settled_utc") and instant(settled["settled_utc"]) <= now:
            continue
        kickoffs = [leg.get("kickoff_utc") for leg in ticket["legs"]]
        first = min(map(instant, kickoffs)) if kickoffs and all(kickoffs) else None
        phase = "unknown" if first is None else "upcoming" if first > now else "started"
        result[ticket["account"]].append(
            {
                "record_id": ticket["record_id"],
                "phase": phase,
                "earliest_kickoff_utc": first.isoformat() if first else None,
            }
        )
    return result


def settled_since(previous, ledger, now):
    current = {
        row["record_id"]: row
        for row in ledger
        if row.get("record_type") == "ticket" and row["threshold"] == "1.05"
    }
    result = {}
    for account, pending in previous["pending"].items():
        outcomes = []
        for row in pending:
            ticket = current.get(row["record_id"], {})
            settlement = ticket.get("settlement") or {}
            if settlement.get("settled_utc") and instant(settlement["settled_utc"]) <= now:
                outcomes.append(settlement)
        result[account] = dict(
            previous=len(pending),
            upcoming=sum(r["phase"] == "upcoming" for r in pending),
            started=sum(r["phase"] == "started" for r in pending),
            unknown=sum(r["phase"] == "unknown" for r in pending),
            settled=len(outcomes),
            won=sum(r["status"] == "won" for r in outcomes),
            lost=sum(r["status"] == "lost" for r in outcomes),
            profit=float(sum((Decimal(str(r["profit"])) for r in outcomes), Decimal(0))),
        )
    return result


def week_start(now):
    local = now.astimezone(BEIJING)
    return (local - timedelta(days=local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def observed_result(row, now):
    result = row.get("result") or {}
    return (
        result
        if (
            result.get("available_utc")
            and instant(result["available_utc"]) <= now
            and result.get("home_goals") is not None
            and result.get("away_goals") is not None
        )
        else None
    )
