"""Manual Actions-only API-Football first-page permission and status billing probes."""

import csv
import json
import os
import re
from datetime import UTC, datetime

from zucai.apif_budget import utc_stamp
from zucai.cloud_apis import failure_reason
from zucai.sources.api_football import ApiFootball
from zucai.sources.base import RawStore

LEAGUE_IDS = {3, 848, 98, 292, 188, 113, 103, 244, 119, 253, 169}
FIELDS = (
    "run_id",
    "observed_utc",
    "league",
    "season",
    "http",
    "errors",
    "results",
    "paging",
    "raw_sha256",
    "outcome",
    "reason",
)


def require_probe_actions():
    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
    ):
        raise ValueError("权限 probe 只能由审查者通过 Actions workflow_dispatch 触发")


def probe_scopes(codes, seasons, now):
    codes = "3,848" if codes is None else codes
    seasons = "2021-2026" if seasons is None else seasons
    if not re.fullmatch(r"[0-9]+(?:,[0-9]+)*", codes):
        raise ValueError("probe leagues 须为逗号分隔的已支持联赛 ID")
    ids = [int(v) for v in codes.split(",")]
    if len(set(ids)) != len(ids) or not set(ids) <= LEAGUE_IDS:
        raise ValueError("probe 只允许 3、848 和既有九个联赛，且不得重复")
    if re.fullmatch(r"[0-9]{4}-[0-9]{4}", seasons):
        first, last = map(int, seasons.split("-"))
        years = list(range(first, last + 1))
    elif re.fullmatch(r"[0-9]{4}(?:,[0-9]{4})*", seasons):
        years = [int(v) for v in seasons.split(",")]
    else:
        raise ValueError("probe seasons 须为 YYYY-YYYY 或逗号分隔的年份")
    if not years or len(set(years)) != len(years) or any(not 2021 <= y <= now.year for y in years):
        raise ValueError("probe 赛季须在 2021 至当前 UTC 年之间，且不得重复")
    return [(league, year) for league in ids for year in years]


def status_billing(first, second):
    """Two consecutive status observations; only consistent counters imply a result."""
    start, end = utc_stamp(first["observed_utc"]), utc_stamp(second["observed_utc"])
    if start.date() != end.date() or end <= start:
        return None
    if first["plan"] != second["plan"] or first["limit_day"] != second["limit_day"]:
        return None
    for item in (first, second):
        headers = item["headers"]
        if headers.get("x-ratelimit-requests-limit") != str(item["limit_day"]) or headers.get(
            "x-ratelimit-requests-remaining"
        ) != str(item["limit_day"] - item["current"]):
            return None
    change = second["current"] - first["current"]
    return {0: True, 1: False}.get(change)


def append_csv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def collect_probe(database, run_id, *, codes=None, seasons=None, now=None):
    require_probe_actions()
    now = now or datetime.now(UTC)
    scopes = probe_scopes(codes, seasons, now)
    state = database.parent / "state"
    audit = state / "audit"
    # Probing is deliberately independent of confirmed season permissions. No importer runs.
    client = ApiFootball(
        store=RawStore(database.parent / "raw"),
        quota_path=state / "api_football_quota.json",
        leagues={str(k): {} for k in LEAGUE_IDS},
    )
    report = dict(
        status="complete",
        requests=0,
        status_requests=0,
        status_billing="unknown",
        errors=[],
        probes=[],
    )
    observations = []
    try:
        for _ in range(2):
            before = client.request_count
            try:
                observations.append(client.status())
            except Exception as exc:
                report["status"] = "partial"
                report["errors"].append("status: " + failure_reason(exc))
                break
            finally:
                report["status_requests"] += client.request_count - before
        free = status_billing(*observations) if len(observations) == 2 else None
        if observations:
            client.quota.record_status_billing(free, [o["sha256"] for o in observations])
        report["status_billing"] = (
            "unbilled" if free is True else "billed" if free is False else "unknown"
        )
        for observation in observations:
            append_csv(
                audit / "apif_status_probes.csv",
                (
                    "run_id",
                    "observed_utc",
                    "plan",
                    "limit_day",
                    "current",
                    "headers",
                    "raw_sha256",
                    "billing",
                ),
                [
                    dict(
                        run_id=run_id,
                        observed_utc=observation["observed_utc"],
                        plan=observation["plan"],
                        limit_day=observation["limit_day"],
                        current=observation["current"],
                        headers=json.dumps(observation["headers"], sort_keys=True),
                        raw_sha256=observation["sha256"],
                        billing=report["status_billing"],
                    )
                ],
            )
        for league, season in scopes:
            row = dict.fromkeys(FIELDS, "")
            row.update(
                run_id=run_id,
                observed_utc=now.isoformat(),
                league=league,
                season=season,
                outcome="error",
            )
            client.pages = []
            before = client.request_count
            try:
                path = client._request(client.request_url(league_id=league, season=season))
                doc = client._document(path.read_bytes(), client._scope(league, season))
                if doc["paging"]["current"] != 1:
                    raise ValueError("probe 未返回第一页")
                row["outcome"] = "ok"
            except Exception as exc:
                row["reason"] = failure_reason(exc)
                report["status"] = "partial"
                report["errors"].append(f"{league}/{season}: {row['reason']}")
            finally:
                report["requests"] += client.request_count - before
                if client.pages:
                    path = client.pages[-1]
                    meta = client.store.load_metadata(path)
                    doc = json.loads(path.read_bytes())
                    row.update(
                        http=meta["status_code"],
                        raw_sha256=meta["sha256"],
                        observed_utc=meta["fetched_at_utc"],
                    )
                    if isinstance(doc, dict):
                        row.update(
                            errors=json.dumps(doc.get("errors"), ensure_ascii=False),
                            results=doc.get("results"),
                            paging=json.dumps(doc.get("paging")),
                        )
                append_csv(audit / "apif_season_probes.csv", FIELDS, [row])
                report["probes"].append(row)
        try:
            report.update(client.quota.describe())
        except ValueError as exc:
            report["status"] = "partial"
            report["errors"].append("quota: " + failure_reason(exc))
    finally:
        client.session.close()
    return report


def write_probe_summary(report):
    import html
    from pathlib import Path

    from zucai.cloud_feeds import write_apif_quota

    if not os.environ.get("GITHUB_STEP_SUMMARY"):
        return
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as out:
        out.write(
            f"### API-Football probe: {report['status']}\n\n"
            f"仅第一页，不入库；/status 计费观察: {report['status_billing']}。\n"
        )
        write_apif_quota(out, report)
        out.write(
            "\n| 联赛 | 赛季 | HTTP | results | paging | 状态 | 原件 SHA256 |\n"
            "| --- | --- | --- | --- | --- | --- | --- |\n"
        )
        for row in report["probes"]:
            cells = [
                html.escape(str(row[k])).replace("|", "&#124;").replace("\n", " ")
                for k in ("league", "season", "http", "results", "paging", "outcome", "raw_sha256")
            ]
            out.write("| " + " | ".join(cells) + " |\n")
        for error in report["errors"]:
            out.write("\n- " + html.escape(error) + "\n")
        out.write(
            "\n完整证据：state/audit/apif_season_probes.csv、apif_status_probes.csv；"
            "原件在本次 artifact。赛季权限配置尚未回填。\n"
        )
