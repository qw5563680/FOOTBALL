"""Display frozen paper groups and F receipts; never select groups in the daily report."""

from datetime import timedelta

from zucai.digest_display import _link, _match, _number, _text
from zucai.parlay.archive import load_documents
from zucai.parlay.paper import read_ledger, render
from zucai.scoreboard.score import instant
from zucai.sources.sporttery import BEIJING

LABEL = {"h": "胜", "d": "平", "a": "负"}
LEVEL = {
    "prospective_verified": "前瞻已验证",
    "historical_gate_passed": "历史门槛通过",
    "p2_experimental": "P2 实验",
    "market_reference": "市场参考",
}
REASONS = {
    "single_direction_fragile": "单选方向差距不足",
    "double_direction_fragile": "双选放掉项接近入选项，只作参考",
    "independent_direction_disagrees": "独立模型方向不同",
    "no_frozen_had_sp": "缺冻结普通胜平负 SP",
    "single_sp_below_1_20": "单选 SP 低于 1.20",
    "model_probabilities_unavailable": "球队模型未生成可靠完整概率，留空",
    "sale_qualification_unknown": "过关资格待核",
    "not_selling": "已停售或销售状态不符",
    "had_parlay_not_supported": "未确认普通胜平负过关资格",
    "identity_conflict": "比赛身份冲突",
    "already_started": "已开球或开球时间缺失",
    "match_not_scheduled": "比赛已结束、取消或状态变化",
    "not_jc": "不是竞彩比赛",
    "not_jc_or_missing_kickoff": "不是竞彩比赛或缺开球时间",
}


def _reason(reason):
    if ":injuries_" in reason:
        side = "主队" if reason.startswith("home:") else "客队"
        return side + "伤停截点前证据不足，关键反证未解决"
    return REASONS.get(reason, reason)


def _probabilities(values):
    return " / ".join(_number(values[k], 2, percent=True) for k in "hda")


def _percent(value):
    return _number(value, 2, percent=True)


def _group_lines(group):
    a, b = group["single"], group["double"]
    alpha, pair = group["single_selection"], group["double_selections"]
    lines = [
        f"### {_match(a)} × {_match(b)}",
        "",
        "| 腿 / 原判断 | 冻结胜 / 平 / 负 | 市场参考胜 / 平 / 负 | 票面 | 放掉项 |"
        " SP / 快照 UTC | 版本与证据 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for role, leg, selected in (("单选", a, [alpha]), ("双选", b, pair)):
        omitted = (
            "—"
            if role == "单选"
            else (f"{LABEL[group['omitted']]} {_percent(group['omitted_probability'])}")
        )
        prices = " / ".join(str(leg["market"]["prices"][k]) for k in selected)
        choices = "＋".join(LABEL[k] for k in selected)
        lines.append(
            f"| {role} {_match(leg)} / {LABEL[leg['original_direction']]} | "
            f"{_probabilities(leg['probabilities'])} | "
            f"{_probabilities(leg['market']['probabilities'])} | {choices} | {omitted} | "
            f"{prices} / {_text(leg['market']['snapshot_utc'])} | "
            f"{_text(leg['model_version'])} / {_text(leg['p2_label'])} |"
        )
    lines += [
        "",
        f"覆盖 {_percent(group['coverage_probability'])}；"
        f"整组盈利概率 {_percent(group['profit_probability'])}；"
        f"税前 EV {_number(group['expected_profit'], 2)} 元；"
        f"ROI {_percent(group['roi'])}；回本概率 {_percent(group['breakeven_probability'])}。",
        "两注各 2 元，共 4 元，仅作纸面比较。跨场独立近似；"
        "取位、税费与可执行性均未核实；BET 禁用。",
        "",
        "| 分支 / 两注展开 | 概率 | 整组本金 | 奖金 | 整组净收益 | 盈亏 |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for i, branch in enumerate(group["branches"]):
        name = f"第{i + 1}注中：{LABEL[alpha]} × {LABEL[pair[i]]}" if i < 2 else "都不中"
        status = {"profit": "盈利", "loss": "亏损", "breakeven": "回本"}[branch["profit_status"]]
        lines.append(
            f"| {name} | {_percent(branch['probability'])} | 4.00 | "
            f"{branch['return']:.2f} | {branch['profit']:+.2f} | {status} |"
        )
    lines += [
        "",
        "支持：" + "；".join(map(_text, a["support"])) + "。",
        "伤停证据："
        + "；".join(
            f"{label}供应商报缺席 {a['injuries'][side]['confirmed_injured_out']} 人，"
            f"捕获 UTC {_text(a['injuries'][side]['captured_utc'])}"
            for side, label in (("home", "主队"), ("away", "客队"))
        )
        + "；不等于主力门将已确认可出场。",
        "价格审核：" + a["single"]["price_status"] + "；关键阵容反证尚未由正式首发排除。",
        "反证与可能改向条件：" + "；".join(map(_text, a["direction_change_conditions"])) + "。",
        "共同击穿条件：" + "；".join(map(_text, group["shared_risks"])) + "。",
        "缺口：" + "；".join(map(_text, group["gaps"])) + "；动作：" + group["action"] + "。",
        "",
    ]
    return lines


def _current(group, matches, now):
    return all(
        instant(leg["kickoff_utc"]) > instant(now)
        and matches.get(leg["match_id"], {}).get("status", "scheduled") == "scheduled"
        for leg in (group["single"], group["double"])
    )


def _reference(group):
    choices = "＋".join(LABEL[k] for k in group["double_selections"])
    return (
        f"- {_match(group['single'])} {LABEL[group['single_selection']]} × "
        f"{_match(group['double'])} {choices}；覆盖 {_percent(group['coverage_probability'])}，"
        f"盈利概率 {_percent(group['profit_probability'])}；可执行性未核实。"
    )


def section(archive, manifest, summary, now, repository, *, matches, previous=None):
    latest = {doc["day"]: doc for doc in load_documents(archive, manifest, now)}
    latest_batches = {}
    for run in (manifest or {}).get("runs", {}).values():
        for day, entry in run["forecasts"].items():
            key = (instant(entry["generated_utc"]), entry["batch_id"])
            if key[0] <= instant(now) and key > latest_batches.get(day, (key[0], "")):
                latest_batches[day] = key
    latest = {
        day: doc
        for day, doc in latest.items()
        if latest_batches.get(day, (None, None))[1] == doc["batch_id"]
    }
    today = instant(now).astimezone(BEIJING).date()
    wanted = {today.isoformat(), (today + timedelta(days=1)).isoformat()}
    display_limit = max(
        (doc["config"]["max_groups"] for day, doc in latest.items() if day in wanted), default=3
    )
    lines = [
        "## 二串一候选（单选＋双选，纸面）",
        "",
        "非购买建议 · BET 禁用。可执行性一律未核实；各榜只作对照，跨榜重合不算新增分散机会。",
        "",
    ]
    displayed, versions = 0, {}
    used_matches, used_teams = set(), set()
    for day, doc in sorted(latest.items()):
        if day not in wanted:
            continue
        versions[doc["batch_id"]] = doc["sha256"]
        lines += [
            f"冻结来源：{_link(doc['path'], repository)}；批次 {_text(doc['batch_id'])}。",
            "",
        ]
        for group in doc["groups"]:
            if not _current(group, matches, now):
                lines.append("已开球或状态变化的组移出当前候选，原票与复盘保留。")
                continue
            if (
                displayed >= display_limit
                or used_matches.intersection(group["match_ids"])
                or used_teams.intersection(group["team_ids"])
            ):
                lines.append(
                    "跨今明批次重复比赛/球队或达到展示上限，不列入本版主组；原批次记录保留。"
                )
                continue
            displayed += 1
            used_matches.update(group["match_ids"])
            used_teams.update(group["team_ids"])
            lines += _group_lines(group)
        for board in doc["boards"]:
            scope = board["scope"]
            name = LEVEL.get(board["name"], board["name"])
            lines.append(
                f"{_text(name)}：枚举 {scope['enumerated']} 种，"
                f"资格过滤后 {scope['eligible']} 种，参考 {scope['reference_only']} 种。"
                "按覆盖排序贪心筛选，不保证全局最优。"
            )
            for key, label in (
                ("highest_coverage", "覆盖最高"),
                ("highest_profit", "盈利概率最高"),
            ):
                best = board[key]
                if best and _current(best, matches, now):
                    lines += [f"本等级整组{label}（另列比较，不是额外一组）：", _reference(best)]
            for group in board["reference_groups"]:
                if _current(group, matches, now):
                    lines += [
                        "仅供参考：" + "、".join(map(_reason, group["reference_reasons"])),
                        _reference(group),
                    ]
        for label, boards in (
            ("实验模型参考榜", doc["experimental_boards"]),
            ("市场参考榜", [doc["market_reference"]]),
        ):
            lines += ["", f"#### {label}", ""]
            for board in boards:
                groups = [
                    g
                    for g in board["groups"] + board["reference_groups"]
                    if _current(g, matches, now)
                ]
                name = LEVEL.get(board["name"], board["name"])
                lines.append(f"{_text(name)}：{len(groups)} 组参考，未进入 F 账本。")
                lines.extend(_reference(group) for group in groups)
        if doc["diagnostics"]:
            lines.append(
                "诊断："
                + "；".join(
                    f"{_text(r['match_id'])}：{_text('、'.join(map(_reason, r['reasons'])))}"
                    for r in doc["diagnostics"]
                )
            )
        if doc.get("previous_version_sha256"):
            lines.append(
                f"替换前版 {_text(doc['previous_version_sha256'][:12])}；"
                f"被替换组 {_text(','.join(doc['replaced_groups']) or '无')}，旧版仍保留。"
            )
    lines += ["", f"本版合格主候选 {displayed} 组；不足不凑数。", ""]
    ledger = read_ledger(archive, summary)
    lines += render((summary or {}).get("parlay_sd")).splitlines()
    records = {r["record_id"]: r for r in ledger if r["record_type"] == "group"}
    pending = []
    for row in records.values():
        if row["settlement"]:
            continue
        first = min(instant(row["frozen"][side]["kickoff_utc"]) for side in ("single", "double"))
        pending.append(
            dict(
                record_id=row["record_id"], phase="upcoming" if first > instant(now) else "started"
            )
        )
    lines.append(
        f"F 待结：发报时未开球 {sum(p['phase'] == 'upcoming' for p in pending)} 组；"
        f"已开球、赛果未核对 {sum(p['phase'] == 'started' for p in pending)} 组。"
        "取消或结果证据不足保持待结。"
    )
    if previous is not None:
        previous_pending = previous.get("parlay_pending", [])
        settled = [
            records[p["record_id"]]["settlement"]
            for p in previous_pending
            if p["record_id"] in records and records[p["record_id"]]["settlement"]
        ]
        lines.append(
            f"F 较上一版：原待结 {len(previous_pending)} 组，本版补结 {len(settled)} 组，"
            f"其中覆盖 {sum(s['covered'] for s in settled)} 组、"
            f"盈利 {sum(s['status'] == 'profit' for s in settled)} 组、"
            f"亏损 {sum(s['status'] == 'loss' for s in settled)} 组；两者分别记账。"
        )
    return lines + [""], pending, versions
