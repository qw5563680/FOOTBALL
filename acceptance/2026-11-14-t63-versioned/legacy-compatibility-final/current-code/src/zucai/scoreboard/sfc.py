"""SFC14 frozen-report diagnostics, without money, prizes or model changes."""

import json
from collections import Counter
from statistics import fmean

from zucai.cloud_feeds import digest
from zucai.forecast_archive import _path
from zucai.prediction_disclosure import prior_disclosure
from zucai.report.sfc import disclosed_rows
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
    return [_disclosed_settlement(archive, row) for row in rows], raw


def _prior_groups(rows):
    groups = {
        key: dict(n=0, evaluated=0, hits=0)
        for key in ("default_strength", "known_teams", "unrecorded")
    }
    for row in rows:
        used = prior_disclosure(row)["used"]
        key = (
            "default_strength" if used is True else "known_teams" if used is False else "unrecorded"
        )
        groups[key]["n"] += 1
        groups[key]["evaluated"] += row.get("single_hit") is not None
        groups[key]["hits"] += row.get("single_hit") is True
    return groups


def _disclosed_settlement(archive, record):
    """Recover exact old-report evidence in memory; never rewrite settled JSONL rows."""
    original = {}
    name = record.get("report_path")
    if name and record.get("report_sha256"):
        path = _path(archive, name)
        if path.exists():
            if digest(path) != record["report_sha256"]:
                raise ValueError("SFC 结算引用报告 SHA256 不符")
            report = json.loads(path.read_bytes())
            if report.get("issue_no") == record["issue_no"]:
                original = {(row["seq"], row["match_id"]): row for row in disclosed_rows(report)}
    rows = []
    for row in record["rows"]:
        source = original.get((row["seq"], row["match_id"]))
        evidence = prior_disclosure(row)
        if source is not None and all(
            source.get(key) == row.get(key) for key in ("probabilities", "top_outcome")
        ):
            source_evidence = prior_disclosure(source)
            if evidence["used"] is not True or source_evidence["used"] is True:
                evidence = source_evidence
        rows.append({**row, "prior_disclosure": evidence})
    return {**record, "rows": rows, "prior_groups": _prior_groups(rows)}


def prior_group_text(groups):
    if groups is None:
        return "默认实力分层：旧结算未记录分层依据。"
    labels = {
        "default_strength": "使用默认实力",
        "known_teams": "双方已知",
        "unrecorded": "默认实力使用情况未记录",
    }
    return (
        "默认实力分层："
        + "；".join(
            f"{label} {groups[key]['n']} 场（已计分 {groups[key]['evaluated']}、"
            f"命中 {groups[key]['hits']}）"
            for key, label in labels.items()
        )
        + "。"
    )


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
        for row in disclosed_rows(report):
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
                    "prior_disclosure": prior_disclosure(row),
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
    visible = [
        _disclosed_settlement(archive, r) for r in records if instant(r["settled_utc"]) <= at
    ]
    summary = {
        "issues": len(visible),
        "average_single_hits": fmean(r["single_hits"] for r in visible) if visible else None,
        "hit_distribution": dict(sorted(Counter(str(r["single_hits"]) for r in visible).items())),
        "latest": max(visible, key=lambda r: r["issue_no"]) if visible else None,
        "prior_groups": _prior_groups([row for record in visible for row in record["rows"]]),
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
        + prior_group_text(section.get("prior_groups"))
        + "\n\n"
        + "缺模型场次没有单选首项，逐期同时记录 single_evaluated；复式不可行时全中诊断为空。"
        "仅作诊断，不设资金账户、不估奖金，BET 禁用。\n"
    )
