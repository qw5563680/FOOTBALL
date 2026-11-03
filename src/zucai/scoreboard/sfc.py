"""SFC14 frozen-report diagnostics, without money, prizes or model changes."""

import json
from collections import Counter
from statistics import fmean

from zucai.cloud_feeds import digest
from zucai.forecast_archive import _path
from zucai.scoreboard.score import instant, outcome, result_reason
from zucai.sfc_reports import read_reports


def read_settled(archive, summary):
    path = _path(archive, "sfc/settled.jsonl")
    sha = (summary or {}).get("sfc", {}).get("settled_sha256")
    if not path.exists():
        if sha:
            raise ValueError("SFC 结算记录缺失")
        return [], b""
    if not sha or digest(path) != sha:
        raise ValueError("SFC 结算 SHA256 不符")
    raw = path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        raise ValueError("SFC 结算 JSONL 缺少行终止符")
    rows = [json.loads(line) for line in raw.splitlines()]
    if len({r["issue_no"] for r in rows}) != len(rows):
        raise ValueError("SFC 结算期号重复")
    return rows, raw


def settle(archive, results, existing, at):
    known = {row["issue_no"] for row in existing}
    latest = {}
    for path, report in read_reports(archive):
        end = report["source"]["sale_end_utc"]
        generated = instant(report["generated_utc"])
        if (
            not end
            or report["mode"] != "prematch"
            or generated > at
            or generated >= instant(end)
            or instant(end) > at
            or any(generated >= instant(r["kickoff_utc"]) for r in report["rows"])
        ):
            continue
        key = report["issue_no"]
        if key not in latest or (generated, path) > latest[key][:2]:
            latest[key] = (generated, path, report)
    added = []
    for number, (generated, path, report) in sorted(latest.items()):
        if number in known:
            continue
        rows = []
        for row in report["rows"]:
            identity = row.get("matched_identity")
            result = results.get(row["match_id"])
            if not identity or result_reason({**identity, "generated_utc": generated}, result, at):
                break
            actual = outcome(result)
            rows.append(
                {
                    "seq": row["seq"],
                    "match_id": row["match_id"],
                    "home": identity["home"],
                    "away": identity["away"],
                    "probabilities": row["probabilities"],
                    "top_outcome": row["top_outcome"],
                    "selection": row["selection"],
                    "actual": actual,
                    "home_goals": result["home_goals"],
                    "away_goals": result["away_goals"],
                    "available_utc": result["available_utc"],
                    "single_hit": row["top_outcome"] == actual if row["top_outcome"] else None,
                }
            )
        if len(rows) != 14:
            continue
        added.append(
            {
                "issue_no": number,
                "report_path": path + "/sfc.json",
                "report_sha256": digest(archive / path / "sfc.json"),
                "report_generated_utc": report["generated_utc"],
                "settled_utc": max((r["available_utc"] for r in rows), key=instant),
                "single_hits": sum(r["single_hit"] is True for r in rows),
                "single_evaluated": sum(r["top_outcome"] is not None for r in rows),
                "combo_all_hit": all(r["actual"] in r["selection"] for r in rows)
                if report["combo"]
                else None,
                "rows": rows,
                "diagnostic": True,
                "bet_allowed": False,
            }
        )
    records = [*existing, *added]
    visible = [r for r in records if instant(r["settled_utc"]) <= at]
    summary = {
        "issues": len(visible),
        "average_single_hits": fmean(r["single_hits"] for r in visible) if visible else None,
        "hit_distribution": dict(sorted(Counter(str(r["single_hits"]) for r in visible).items())),
        "latest": max(visible, key=lambda r: r["issue_no"]) if visible else None,
        "diagnostic": True,
        "bet_allowed": False,
    }
    return added, summary


def render_diagnostics(summary):
    section = summary.get("sfc")
    if section is None:
        return ""
    avg = section["average_single_hits"]
    average = avg if avg is not None else "未提供"
    distribution = json.dumps(section["hit_distribution"], ensure_ascii=False)
    return (
        "\n## 14 场胜负彩诊断\n\n"
        f"已结 {section['issues']} 期；平均单选命中：{average} / 14。\n\n"
        f"命中数分布（命中场数:期数）：{distribution}。\n\n"
        "缺模型场次没有单选首项，逐期同时记录 single_evaluated；复式不可行时全中诊断为空。"
        "仅作诊断，不设资金账户、不估奖金，BET 禁用。\n"
    )
