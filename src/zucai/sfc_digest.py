"""Read-only SFC section for the daily digest."""

from zucai.digest_context import archived_sfc_issues
from zucai.report.render import _beijing, _md, _percent, _timestamp
from zucai.report.sfc import OPTIONS, pick_nine_lines
from zucai.scoreboard.sfc import read_settled
from zucai.sfc_publication import deadline_message, deadline_status, resolve_publications
from zucai.sfc_reports import read_reports


def section(archive, feed, summary, now, link):
    reports = resolve_publications(
        archive, read_reports(archive, include_unconfirmed=True), now=now
    )
    settled, _ = read_settled(archive, summary)
    states = archived_sfc_issues(archive, feed, now)
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
    open_states = [
        (name, issue)
        for name, issue in states
        if issue.get("sale_end_utc") and _timestamp(issue["sale_end_utc"]) > now
    ]
    visible = open_states or [max(states, key=lambda item: item[1]["issue_no"])]
    for name, issue in sorted(visible, key=lambda item: item[1]["issue_no"], reverse=True):
        lines.extend(_issue_lines(name, issue, reports, settled, now, link))
    return lines


def _fixture_list(rows):
    return sorted(
        (
            r["seq"],
            r.get("fixture_id"),
            r.get("league"),
            r["home"],
            r["away"],
            r.get("provider_home_id"),
            r.get("provider_away_id"),
            _timestamp(r.get("source_kickoff_utc") or r["kickoff_utc"]),
            bool(r.get("source_ended")),
        )
        for r in rows
    )


def _issue_lines(name, issue, reports, settled, now, link):
    number = issue["issue_no"]
    choices = [
        (path, r)
        for path, r in reports
        if r["issue_no"] == number and _timestamp(r["generated_utc"]) <= now
    ]
    if (
        issue.get("sale_end_utc")
        and _timestamp(issue["sale_end_utc"]) > now
        and any(r["league"] == "欧国联" for r in issue["snapshot"])
    ):
        current = _fixture_list(issue["snapshot"])
        choices = [
            (path, report) for path, report in choices if _fixture_list(report["rows"]) == current
        ]
    for _, candidate in choices:
        candidate["sale_end_utc"] = issue["sale_end_utc"]
        candidate["sale_state"] = deadline_status(issue["sale_end_utc"], candidate["published_utc"])
    timely = [
        (path, report)
        for path, report in choices
        if report["sale_state"] == "published_before_deadline"
    ]
    path, report = (
        max(timely or choices, key=lambda item: (_timestamp(item[1]["generated_utc"]), item[0]))
        if choices
        else (None, None)
    )
    end = issue["sale_end_utc"]
    state = "截止未知" if not end else "已截止" if _timestamp(end) <= now else "未截止"
    deadline = _beijing(end) if end else "未知"
    eligible = report and report["sale_state"] == "published_before_deadline"
    publication = report or dict(
        sale_state="deadline_unconfirmed", generated_utc=None, published_utc=None, sale_end_utc=end
    )
    published = _beijing(publication["published_utc"]) if publication["published_utc"] else "未知"
    lines = [
        f"期号：{number}；第三方所报截止：{deadline}（北京时间，{state}）。",
        f"来源：{link(path + '/sfc.json') if report else link(name)}；仅作诊断，BET 禁用。",
        f"报告生成：{_beijing(report['generated_utc'])}（北京时间）。"
        if report
        else "尚无当日冻结报告。",
        deadline_message(publication) + "。",
        f"存好时间：{published}（北京时间）。",
        "",
        *(
            [
                "| 序号 | 对阵 | 北京开球 | 主胜 | 平 | 客胜 | 单选首项 | 复式项 | 状态 |",
                "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
            if eligible
            else ["| 序号 | 对阵 | 北京开球 | 状态 |", "| --- | --- | --- | --- |"]
        ),
    ]
    rows = report["rows"] if report else issue["snapshot"]
    for row in sorted(rows, key=lambda r: r["seq"]):
        p = row.get("probabilities") or {}
        selection = row.get("selection") or (list("hda") if not p else [])
        reason = row.get("reason") or ("模型诊断" if p else "尚无冻结概率")
        if not p:
            if row.get("match_id") is None:
                if not reason.startswith("未映射："):
                    reason = "未映射：" + reason
            elif not reason.startswith("无模型"):
                reason = "无模型：" + reason
        kickoff = _beijing(row["kickoff_utc"])
        if row.get("kickoff_basis") == "placeholder":
            kickoff += "（占位开球时间，待核实）"
        values = [
            row["seq"],
            f"{row['home']} VS {row['away']}",
            kickoff,
            *(
                [
                    *[_percent(p.get(k)) for k in "hda"],
                    OPTIONS.get(row.get("top_outcome"), "—"),
                    "+".join(OPTIONS[k] for k in selection) or "—",
                    reason,
                ]
                if eligible
                else ["无冻结概率"]
            ),
        ]
        lines.append("| " + " | ".join(_md(value) for value in values) + " |")
    combo = report["combo"] if eligible else None
    lines += [""]
    if combo:
        lines += [
            f"复式 {combo['lines']} 注 / 上限 64；"
            f"覆盖概率 {_percent(combo['coverage_probability'])}"
            "（跨场独立近似；无模型场次保留空概率、按三选覆盖）。"
        ]
    elif eligible:
        lines += [
            f"复式未生成：至少 {report['minimum_lines']} 注，超过 64 注上限；覆盖概率未生成。"
        ]
    lines += ["", *pick_nine_lines(report)]
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
            f"（有模型 {last['single_evaluated']} 场）"
            + (f"；复式诊断：{combo_hit}。" if eligible else "。")
        ]
    else:
        lines += ["上一期命中数：尚无已结算记录。"]
    return lines + [
        "不估奖金、不设资金账户；3+1+0 全包＝无预测，无模型三选不计作单选命中。"
        if eligible
        else "不估奖金、不设资金账户。",
        "",
    ]
