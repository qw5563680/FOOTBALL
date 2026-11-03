"""Actions-only metadata confirmation for the seven candidate identity leagues."""

import csv
import json
import re
from datetime import UTC, datetime

from zucai.cloud_apis import failure_reason
from zucai.cloud_probe import append_csv, require_probe_actions
from zucai.sources.api_football import (
    API,
    APIF_IDENTITY_LEAGUE_IDS,
    LEAGUES,
    ApiFootball,
    check_api_errors,
)
from zucai.sources.base import RawStore
from zucai.sources.football_data import read_json

FIELDS = (
    "run_id",
    "observed_utc",
    "league",
    "api_name",
    "country",
    "seasons",
    "http",
    "raw_sha256",
    "outcome",
    "reason",
)


def identity_codes(codes=None):
    configured = read_json(LEAGUES)
    selected = sorted(map(str, APIF_IDENTITY_LEAGUE_IDS)) if codes is None else codes.split(",")
    if (
        not selected
        or len(selected) != len(set(selected))
        or any(
            code not in {str(k) for k in APIF_IDENTITY_LEAGUE_IDS}
            or configured.get(code, {}).get("identity_only") is not True
            for code in selected
        )
    ):
        raise ValueError("identity 仅支持七个 identity-only 桥接联赛，不得重复")
    return {code: configured[code] for code in selected}


def _metadata(raw, code, config):
    doc = json.loads(raw)
    check_api_errors(doc, raw)
    if (
        doc.get("get") != "leagues"
        or doc.get("parameters") != {"id": code}
        or doc.get("results") != 1
        or doc.get("paging") != {"current": 1, "total": 1}
        or not isinstance(doc.get("response"), list)
        or len(doc["response"]) != 1
    ):
        raise ValueError("联赛元数据范围、分页或数量不符")
    record = doc["response"][0]
    league, country = record["league"], record["country"]
    if (
        type(league.get("id")) is not int
        or league["id"] != int(code)
        or league.get("name") != config["api_name"]
        or league.get("type") != "League"
        or country.get("name") != config["country"]
    ):
        raise ValueError("候选联赛 ID、名称、国家或类型不符，须人工复核")
    seasons = record["seasons"]
    if not isinstance(seasons, list) or any(type(s.get("year")) is not int for s in seasons):
        raise ValueError("联赛元数据年份无效")
    return dict(
        api_name=league["name"],
        country=country["name"],
        seasons=json.dumps(sorted({s["year"] for s in seasons})),
    )


def collect_league_probe(database, run_id, *, codes=None):
    require_probe_actions()
    selected = identity_codes(codes)
    state = database.parent / "state"
    client = ApiFootball(
        store=RawStore(database.parent / "raw"), quota_path=state / "api_football_quota.json"
    )
    report = dict(status="complete", requests=0, errors=[], probes=[], status_requests=0)
    try:
        for code, config in selected.items():
            client.pages = []
            row = dict.fromkeys(FIELDS, "")
            row.update(
                run_id=run_id,
                league=code,
                observed_utc=datetime.now(UTC).isoformat(),
                outcome="error",
            )
            try:
                path = client._request(API + "/leagues?id=" + code, name="identity-league")
                row.update(_metadata(path.read_bytes(), code, config), outcome="ok")
            except Exception as exc:
                row["reason"] = failure_reason(exc)
                report["status"] = "partial"
                report["errors"].append(f"{code}: {row['reason']}")
            finally:
                if client.pages:
                    meta = client.store.load_metadata(client.pages[-1])
                    row.update(
                        http=meta["status_code"],
                        raw_sha256=meta["sha256"],
                        observed_utc=meta["fetched_at_utc"],
                    )
                append_csv(state / "audit/apif_league_probes.csv", FIELDS, [row])
                report["probes"].append(row)
        report.update(client.quota.describe())
    finally:
        report["requests"] = client.request_count
        client.session.close()
    return report


def require_confirmed_leagues(database, codes):
    selected = identity_codes(",".join(codes))
    path = database.parent / "state/audit/apif_league_probes.csv"
    latest = {}
    if path.exists():
        with path.open(encoding="utf-8", newline="") as stream:
            for row in csv.DictReader(stream):
                latest[row["league"]] = row
    for code, config in selected.items():
        row = latest.get(code, {})
        if (
            row.get("outcome") != "ok"
            or row.get("http") != "200"
            or row.get("api_name") != config["api_name"]
            or row.get("country") != config["country"]
            or not re.fullmatch(r"[0-9a-f]{64}", row.get("raw_sha256", ""))
        ):
            raise ValueError(f"{code} 需先由审查者在 Actions probe 确认联赛元数据")
    return {code: latest[code] for code in selected}


def write_league_probe_summary(report):
    import html
    import os
    from pathlib import Path

    from zucai.cloud_feeds import write_apif_quota

    if not os.environ.get("GITHUB_STEP_SUMMARY"):
        return
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
        stream.write(f"### identity-only 联赛元数据 probe：{report['status']}\n\n")
        write_apif_quota(stream, report)
        stream.write(
            f"\n请求 {report['requests']} 次；只确认 ID/名称/国家，不证明赛季读取权限。\n\n"
        )
        stream.write(
            "| ID | API 名称 | 国家 | 状态 | 原件 SHA256 |\n| --- | --- | --- | --- | --- |\n"
        )
        for row in report["probes"]:
            stream.write(
                "| "
                + " | ".join(
                    html.escape(str(row[k])).replace("|", "&#124;").replace("\n", " ")
                    for k in ("league", "api_name", "country", "outcome", "raw_sha256")
                )
                + " |\n"
            )
        for error in report["errors"]:
            stream.write("\n- " + html.escape(error) + "\n")
