"""Read-only SFC section for the daily digest."""

import json

from zucai.forecast_archive import _path
from zucai.report.render import _beijing, _md, _percent, _timestamp
from zucai.report.sfc import OPTIONS
from zucai.scoreboard.sfc import read_settled
from zucai.sfc_reports import read_reports


def section(archive, feed, summary, now, link):
    reports = read_reports(archive)
    settled, _ = read_settled(archive, summary)
    states = []
    for name in sorted(feed["files"]):
        if name.startswith("state/sfc/"):
            issue = json.loads(_path(archive, name).read_bytes())["issue"]
            if issue.get("observed_utc") and _timestamp(issue["observed_utc"]) <= now:
                states.append((name, issue))
    lines = ["## 14 场胜负彩", ""]
    collected = feed.get("collection", {}).get("sfc")
    if collected:
        lines += [
            f"采集状态：{_md(collected.get('status', '未记录'))}；"
            + "；".join(_md(e) for e in collected.get("errors", [])),
            "",
        ]
    if not states:
        return lines + ["尚无已归档期次。", ""]
    name, issue = max(states, key=lambda item: item[1]["issue_no"])
    number = issue["issue_no"]
    choices = [
        (path, r)
        for path, r in reports
        if r["issue_no"] == number and _timestamp(r["generated_utc"]) <= now
    ]
    path, report = (
        max(choices, key=lambda item: (_timestamp(item[1]["generated_utc"]), item[0]))
        if choices
        else (None, None)
    )
    end = report["source"]["sale_end_utc"] if report else issue["sale_end_utc"]
    state = "截止未知" if not end else "已截止" if _timestamp(end) <= now else "未截止"
    deadline = _beijing(end) if end else "未知"
    lines += [
        f"期号：{number}；第三方所报截止：{deadline}（北京时间，{state}）。",
        f"来源：{link(path + '/sfc.json') if report else link(name)}；仅作诊断，BET 禁用。",
        f"报告生成：{_beijing(report['generated_utc'])}（北京时间）。"
        if report
        else "尚无当日冻结报告。",
        "",
        "| 序号 | 对阵 | 北京开球 | 主胜 | 平 | 客胜 | 单选首项 | 复式项 | 状态 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    rows = report["rows"] if report else issue["snapshot"]
    for row in sorted(rows, key=lambda r: r["seq"]):
        p = row.get("probabilities") or {}
        selection = row.get("selection") or (list("hda") if not p else [])
        reason = row.get("reason") or ("模型诊断" if p else "尚无冻结概率")
        if not p and not reason.startswith("无模型"):
            reason = "无模型：" + reason
        values = [
            row["seq"],
            f"{row['home']} VS {row['away']}",
            _beijing(row["kickoff_utc"]),
            *[_percent(p.get(k)) for k in "hda"],
            OPTIONS.get(row.get("top_outcome"), "—"),
            "+".join(OPTIONS[k] for k in selection) or "—",
            reason,
        ]
        lines.append("| " + " | ".join(_md(value) for value in values) + " |")
    combo = report["combo"] if report else None
    lines += [""]
    if combo:
        lines += [
            f"复式 {combo['lines']} 注 / 上限 64；"
            f"覆盖概率 {_percent(combo['coverage_probability'])}"
            "（跨场独立近似；无模型场次保留空概率、按三选覆盖）。"
        ]
    elif report:
        lines += [
            f"复式未生成：至少 {report['minimum_lines']} 注，超过 64 注上限；覆盖概率未生成。"
        ]
    else:
        lines += ["复式注数与覆盖概率未生成。"]
    previous = [
        r for r in settled if r["issue_no"] < number and _timestamp(r["settled_utc"]) <= now
    ]
    if previous:
        last = max(previous, key=lambda r: r["issue_no"])
        combo_hit = (
            "全中"
            if last["combo_all_hit"] is True
            else "未全中"
            if last["combo_all_hit"] is False
            else "未生成"
        )
        lines += [
            f"上一已结算期 {last['issue_no']}：单选命中 {last['single_hits']} / 14"
            f"（有模型 {last['single_evaluated']} 场）；复式诊断：{combo_hit}。"
        ]
    else:
        lines += ["上一期命中数：尚无已结算记录。"]
    return lines + ["不估奖金、不设资金账户；无模型三选不计作单选命中。", ""]
