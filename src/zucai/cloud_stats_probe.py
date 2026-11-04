"""Bounded Actions-only endpoint observations; technical data never enters the database."""

import html
import json
import os
import re
import sqlite3
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

from zucai.cloud_apis import failure_reason
from zucai.cloud_probe import probe_scopes, require_probe_actions
from zucai.sources.api_football import API, APIF_IDENTITY_LEAGUE_IDS, APIF_LEAGUE_IDS, ApiFootball
from zucai.sources.apif_quota import QuotaExceeded
from zucai.sources.base import RawStore
from zucai.stats_probe import (
    DETAIL_ENDPOINTS,
    FIELDS,
    append_audit,
    parse_response,
    sample_fixtures,
)

ALLOWED = APIF_IDENTITY_LEAGUE_IDS | APIF_LEAGUE_IDS | {2}
DEFAULT_SEASONS = "2022,2024,2026"
DEFAULT_PER_SCOPE = 1
MAX_REQUESTS = 500


def stats_plan(codes, seasons, per_scope, now):
    selected = ",".join(map(str, sorted(ALLOWED))) if codes is None else codes
    if not re.fullmatch(r"[0-9]+(?:,[0-9]+)*", selected):
        raise ValueError("stats leagues 须为逗号分隔的已配置联赛 ID")
    leagues = [int(v) for v in selected.split(",")]
    if len(set(leagues)) != len(leagues) or not set(leagues) <= ALLOWED:
        raise ValueError("stats 仅允许七桥接、十一采集联赛和欧冠元数据，不得重复")
    years = [y for _, y in probe_scopes("3", DEFAULT_SEASONS if seasons is None else seasons, now)]
    if type(per_scope) is not int or per_scope < 1:
        raise ValueError("per_scope 须为正整数")
    scopes = [(code, year) for code in leagues if code != 2 for year in years]
    # One metadata/league, one fixtures + injuries/scope, four endpoints/sample,
    # and at most one half=true hypothesis probe in the whole run. No /status/retries.
    budget = len(leagues) + len(scopes) * (2 + 4 * per_scope) + bool(scopes)
    if budget > MAX_REQUESTS:
        raise ValueError(
            f"stats 请求预算 {budget} 超过 {MAX_REQUESTS}，请缩小 leagues/seasons/per_scope"
        )
    return dict(leagues=leagues, scopes=scopes, per_scope=per_scope, request_budget=budget)


def known_fixture_ids(database):
    """Map provider IDs to existing matches, preserving jc identity for sampling priority."""
    database = Path(database)
    if not database.exists():
        return {}
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = connection.execute("SELECT match_id,counterpart_id FROM matches").fetchall()
    finally:
        connection.close()
    known = {}
    for mid, counterpart in sorted(rows, key=lambda row: (not row[0].startswith("jc:"), row[0])):
        for identity in (mid, counterpart):
            if identity and re.fullmatch(r"apif:[1-9][0-9]*", identity):
                known.setdefault(identity.split(":")[1], mid)
    return known


def _observe(client, audit, report, context, endpoint, parameters):
    row = dict.fromkeys(FIELDS, "")
    row.update(
        context,
        endpoint=endpoint,
        parameters=json.dumps(parameters, sort_keys=True),
        observed_utc=datetime.now(UTC).isoformat(),
        outcome="error",
    )
    before, started = client.request_count, time.monotonic()
    client.pages = []
    raw, error, stop = None, None, False
    try:
        client._request(
            API + "/" + endpoint + "?" + urlencode(parameters),
            name="stats-" + endpoint.replace("/", "-"),
        )
    except Exception as exc:
        error, stop = failure_reason(exc), isinstance(exc, QuotaExceeded)
    if client.pages:
        try:
            path = client.pages[-1]
            meta = client.store.load_metadata(path)
            raw = path.read_bytes()
            row.update(parse_response(raw, endpoint, parameters))
            row.update(
                http=meta["status_code"],
                raw_sha256=meta["sha256"],
                observed_utc=meta["fetched_at_utc"],
            )
        except Exception as exc:
            error, raw = "原件或 sidecar 读取失败：" + failure_reason(exc), None
    if error:
        row.update(outcome="error", reason=error)
    row.update(
        elapsed_ms=round((time.monotonic() - started) * 1000, 3),
        requests=client.request_count - before,
    )
    try:
        quota = client.quota.describe()
        report.update(quota)
        row["remaining_quota"] = quota["remaining_quota"]
    except ValueError as exc:
        row.update(outcome="error", reason=failure_reason(exc))
        stop = True
    append_audit(audit, row)
    report["probes"].append(row)
    report["requests"] = client.request_count
    if row["outcome"] != "ok":
        report["status"] = "partial"
        report["errors"].append(f"{row['league']}/{row['season']}/{endpoint}: {row['reason']}")
    if stop:
        report["stopped_reason"] = row["reason"]
    return json.loads(raw) if raw is not None and row["outcome"] == "ok" else None


def collect_stats_probe(
    database, run_id, *, codes=None, seasons=None, per_scope=DEFAULT_PER_SCOPE, now=None
):
    require_probe_actions()
    plan = stats_plan(codes, seasons, per_scope, now or datetime.now(UTC))
    database = Path(database)
    known = known_fixture_ids(database)
    state = database.parent / "state"
    client = ApiFootball(
        store=RawStore(database.parent / "raw"),
        quota_path=state / "api_football_quota.json",
        reserve=10,
    )
    report = dict(
        status="complete",
        requests=0,
        status_requests=0,
        probes=[],
        errors=[],
        request_budget=plan["request_budget"],
        stopped_reason="",
    )
    audit = state / "audit/apif_stats_probes.csv"

    def observe(code, season, endpoint, parameters, match=None, *, half=False):
        fixture = match["fixture"] if match else {}
        fid, status = fixture.get("id", ""), fixture.get("status", {}).get("short", "")
        context = dict(
            run_id=run_id,
            league=code,
            season=season,
            fixture_id=fid,
            match_id=known.get(str(fid), ""),
            fixture_status=status,
            period_basis="unknown"
            if half
            else ("FT" if status == "FT" else "AET" if status in {"AET", "PEN"} else "unknown"),
        )
        return _observe(client, audit, report, context, endpoint, parameters)

    def skip_details(code, season, reason):
        for endpoint in DETAIL_ENDPOINTS:
            row = dict.fromkeys(FIELDS, "")
            row.update(
                run_id=run_id,
                observed_utc=datetime.now(UTC).isoformat(),
                league=code,
                season=season,
                endpoint=endpoint,
                parameters="{}",
                requests=0,
                remaining_quota=report.get("remaining_quota", ""),
                outcome="skipped",
                reason=reason,
            )
            append_audit(audit, row)
            report["probes"].append(row)
            report["errors"].append(f"{code}/{season}/{endpoint}: {reason}")
        report["status"] = "partial"

    try:
        for code in plan["leagues"]:
            observe(code, "", "leagues", {"id": str(code)})
            if report["stopped_reason"]:
                return report
        half_attempted = False
        for code, season in plan["scopes"]:
            scope = {"league": str(code), "season": str(season)}
            doc = observe(code, season, "fixtures", scope)
            if report["stopped_reason"]:
                return report
            sampled = sample_fixtures(doc["response"], per_scope, known) if doc else []
            if not sampled:
                skip_details(
                    code,
                    season,
                    "首页无可抽样的唯一 FT/AET/PEN/PST/CANC 比赛"
                    if doc
                    else "赛程首页为空或失败，未发出逐场端点请求",
                )
            for match in sampled:
                parameters = {"fixture": str(match["fixture"]["id"])}
                for endpoint in DETAIL_ENDPOINTS:
                    result = observe(code, season, endpoint, parameters, match)
                    if report["stopped_reason"]:
                        return report
                    if (
                        endpoint == "fixtures/statistics"
                        and result
                        and not half_attempted
                        and match["fixture"]["status"]["short"] == "FT"
                    ):
                        # Candidate parameter, not a claim of support or 90-minute segmentation.
                        half_attempted = True
                        observe(
                            code, season, endpoint, {**parameters, "half": "true"}, match, half=True
                        )
                        if report["stopped_reason"]:
                            return report
            observe(code, season, "injuries", scope)
            if report["stopped_reason"]:
                return report
    finally:
        client.session.close()
    return report


def write_stats_probe_summary(report):
    from zucai.cloud_feeds import write_apif_quota

    if not os.environ.get("GITHUB_STEP_SUMMARY"):
        return
    grouped = defaultdict(list)
    for row in report["probes"]:
        endpoint = row["endpoint"]
        if '"half"' in row["parameters"]:
            endpoint += " (half=true，口径待核)"
        grouped[row["league"], row["season"], endpoint].append(row)

    def cell(value):
        return html.escape(str(value)).replace("|", "&#124;").replace("\n", " ").replace("\r", " ")

    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as out:
        out.write(f"### API-Football 技术数据 probe：{report['status']}\n\n")
        out.write(
            f"预算最多 {report['request_budget']} 次；实际 {report['requests']} 次；"
            "无入库、无训练。\n\n"
        )
        out.write(
            "抽样有效率不是全季覆盖率；metadata coverage 只是线索。"
            "FT/AET 标记比赛状态，不能证明统计时段；half=true 仍需审查原件确认。\n\n"
        )
        write_apif_quota(out, report)
        out.write(
            "\n| 赛事 ID | 赛季 | 端点 | 抽样数 | 有效数 | 有效率 | 空响应数 | 空字段数 |"
            " 请求数 | 剩余配额 |\n"
            "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |\n"
        )
        for (code, season, endpoint), rows in grouped.items():
            samples = {r["fixture_id"] for r in rows if r["fixture_id"] and r["requests"]}
            effective = sum(r["outcome"] == "ok" for r in rows)
            requests = sum(r["requests"] for r in rows)
            values = (
                code,
                season or "metadata",
                endpoint,
                len(samples),
                effective,
                f"{effective / requests:.1%}" if requests else "—",
                sum(r["outcome"] == "empty" for r in rows),
                sum(int(r["null_fields"] or 0) for r in rows),
                requests,
                rows[-1]["remaining_quota"],
            )
            out.write("| " + " | ".join(map(cell, values)) + " |\n")
        for error in report["errors"]:
            out.write("\n- " + cell(error) + "\n")
        out.write(
            "\n有效率＝有效响应数 / 实际请求数（网络失败也计请求；未请求显示 —）。"
            "完整证据：state/audit/apif_stats_probes.csv；原件及配额 sidecar 在本次 artifact。"
            "欧冠 2 只读取 metadata。\n"
        )
