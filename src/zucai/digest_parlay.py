"""Display frozen paper groups and F receipts; never select groups in the daily report."""

import copy
import hashlib
import json
from datetime import timedelta

from zucai.digest_display import _link, _match, _number, _text
from zucai.forecast_archive import _path
from zucai.parlay.archive import load_documents
from zucai.parlay.paper import publication_view, read_ledger, render, summarize
from zucai.prediction_disclosure import prior_disclosure, prior_label, uses_unknown_team_prior
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
    "single_top_tied": "无法确定唯一最高项",
    "research_invalidated": "有截点前具体证据使原研究前提失效，等待重新评估",
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
    "unknown_team_prior": "使用默认实力，仅保留研究概率，不进入主候选",
}


def _reason(reason):
    if ":injuries_" in reason:
        side = "主队" if reason.startswith("home:") else "客队"
        return side + "伤停资料未知；仅此缺口不排除纸面比较"
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
            f"| {role} {_match(leg)} / {LABEL.get(leg['original_direction'], '最高项并列')} | "
            f"{_probabilities(leg['probabilities'])} | "
            f"{_probabilities(leg['market']['probabilities'])} | {choices} | {omitted} | "
            f"{prices} / {_text(leg['market']['snapshot_utc'])} | "
            f"{_text(leg['model_version'])} / {_text(leg['p2_label'])} |"
        )
    lines += [
        "",
        f"规则：{_text(group['rule'])}；仅纸面比较。",
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
        *_injury_lines(a, b),
        *_descriptive_lines(a, b),
        "价格审核：" + a["single"]["price_status"] + "；关键阵容反证尚未由正式首发排除。",
        "反证与可能改向条件：" + "；".join(map(_text, a["direction_change_conditions"])) + "。",
        "共同击穿条件：" + "；".join(map(_text, group["shared_risks"])) + "。",
        "缺口：" + "；".join(map(_text, group["gaps"])) + "；动作：" + group["action"] + "。",
        "",
    ]
    return lines


def _injury_lines(a, b):
    cells = []
    for role, leg in (("单选", a), ("双选", b)):
        for side, label in (("home", "主队"), ("away", "客队")):
            cell = (leg.get("injuries") or {}).get(side) or {}
            value = cell.get("confirmed_injured_out")
            detail = (
                "未知（名单缺失、覆盖不足或无法核实）"
                if value is None
                else f"供应商名单报缺席 {value} 人"
            )
            cells.append(f"{role}{label}：{detail}，捕获 UTC {_text(cell.get('captured_utc'))}")
    return ["伤停证据：" + "；".join(cells) + "。人数不代表球员影响；资料缺失不等于关键反证成立。"]


def _descriptive_lines(a, b):
    single, double = a["single"], b["double"]
    if "top_probability" not in single:
        return []  # Old freezes retain their original interpretation and fields.
    text = (
        f"单选第一 {_percent(single['top_probability'])}，"
        f"第二 {_percent(single['second_probability'])}，"
        f"相差 {single['direction_gap'] * 100:.2f} 个百分点（仅描述排序间距）。"
    )
    if single["top_below_half"]:
        text += "该项排序第一，但另外两项合计更可能。"
    other = (
        f"双选覆盖 {_percent(double['coverage_probability'])}，"
        f"放掉项 {_percent(double['omitted_probability'])}，"
        f"入选中较小项与放掉项相差 {double['selected_min_minus_omitted'] * 100:.2f} 个百分点。"
    )
    if double["numerical_tie"]:
        other += "数值并列；按胜+平、胜+负、平+负的固定顺序处理，不表示更可靠。"
    return [text, other]


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


def _prior_group(group):
    return any(
        leg.get("probability_source") != "MARKET" and uses_unknown_team_prior(leg)
        for leg in (group["single"], group["double"])
    )


def _disclosed_document(archive, manifest, document):
    """Enrich a display copy from its exact original evidence, never ledger input."""
    doc = copy.deepcopy(document)
    originals = {}
    for source in (doc.get("sources") or {}).values():
        if not source:
            continue
        path, expected = source["path"], source["sha256"]
        raw = _path(archive, path).read_bytes()
        if (manifest or {}).get("files", {}).get(path) != expected or (
            hashlib.sha256(raw).hexdigest() != expected
        ):
            raise ValueError("二串一默认实力披露的原预测 SHA256 不符")
        batch = json.loads(raw)
        if batch.get("batch_id") != doc["batch_id"]:
            raise ValueError("二串一默认实力披露的原预测批次不符")
        for row in batch.get("predictions", []):
            key = tuple(row.get(k) for k in ("match_id", "model_name", "model_version"))
            originals[key] = row
    notes = {}

    def visit(value):
        if isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, dict):
            if value.get("probability_source") in {"TEAM", "EXPERIMENTAL"}:
                key = tuple(value.get(k) for k in ("match_id", "model_name", "model_version"))
                original = originals.get(key)
                if original:
                    if any(
                        value.get(k) != original.get(k)
                        for k in ("source", "league", "home", "away", "kickoff_utc")
                    ):
                        raise ValueError("二串一默认实力披露的原预测身份不符")
                    detail = prior_disclosure(original)
                    if detail["used"] is True or "prior_disclosure" not in value:
                        value["prior_disclosure"] = detail
                if uses_unknown_team_prior(value):
                    notes[key] = dict(
                        match_id=value["match_id"],
                        reasons=["unknown_team_prior"],
                        prior_disclosure=prior_disclosure(value),
                        prior_label=prior_label(value),
                        model_probabilities=value["model_probabilities"],
                    )
            for child in value.values():
                if isinstance(child, (list, dict)):
                    visit(child)

    visit(doc)
    disclosed = {row["match_id"] for row in doc["diagnostics"] if row.get("prior_label")}
    doc["diagnostics"].extend(row for row in notes.values() if row["match_id"] not in disclosed)
    return doc


def section(archive, manifest, summary, now, repository, *, matches, previous=None):
    documents = load_documents(archive, manifest, now)
    latest = {doc["day"]: doc for doc in documents}
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
        doc = _disclosed_document(archive, manifest, doc)
        versions[doc["batch_id"]] = doc["sha256"]
        lines += [
            f"冻结来源：{_link(doc['path'], repository)}；批次 {_text(doc['batch_id'])}；"
            f"规则 {_text(doc['rule'])}；"
            f"本批生效 UTC {_text(doc.get('rule_effective_utc', doc['generated_utc']))}。",
            "",
        ]
        for group in doc["groups"]:
            if _prior_group(group):
                lines.append("含默认实力的旧冻结组移出当前主候选，原票与复盘保留。")
                continue
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
                f"{_text(name)}冻结时统计：枚举 {scope['enumerated']} 种，"
                f"资格过滤后 {scope['eligible']} 种，参考 {scope['reference_only']} 种。"
                "按覆盖排序贪心筛选，不保证全局最优。"
            )
            if any(uses_unknown_team_prior(row) for row in board["matches"]):
                lines.append("默认实力场次仅留研究诊断；本版展示过滤，不重排原冻结组。")
            for key, label in (
                ("highest_coverage", "覆盖最高"),
                ("highest_profit", "盈利概率最高"),
            ):
                best = board[key]
                if best and not _prior_group(best) and _current(best, matches, now):
                    lines += [f"本等级整组{label}（另列比较，不是额外一组）：", _reference(best)]
            for group in board["reference_groups"]:
                if not _prior_group(group) and _current(group, matches, now):
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
                    if not _prior_group(g) and _current(g, matches, now)
                ]
                name = LEVEL.get(board["name"], board["name"])
                lines.append(f"{_text(name)}：{len(groups)} 组参考，未进入 F 账本。")
                lines.extend(_reference(group) for group in groups)
        if doc["diagnostics"]:
            lines.append(
                "诊断："
                + "；".join(
                    f"{_text(r['match_id'])}：{_text('、'.join(map(_reason, r['reasons'])))}"
                    + (
                        f"；{_text(r['prior_label'])}；研究胜/平/负 "
                        f"{_probabilities(r['model_probabilities'])}"
                        if r.get("prior_label") and r.get("model_probabilities")
                        else ""
                    )
                    for r in doc["diagnostics"]
                )
            )
        impact = doc.get("legacy_filter_diagnostic")
        if impact:
            lines += [
                "旧筛选影响（独立诊断，不参与候选或账户 F）："
                f"{impact['parameters']['single_direction_gap']} / "
                f"{impact['parameters']['double_fragile_gap']} "
                "是未经验证的实验筛选参数，默认筛选关闭。",
                f"旧规则影响 {len(impact['matches'])} 场；"
                f"恢复参加排序的组合 {len(impact['restored_groups'])} 种（不是新增注单数）。"
                "不代表准确度或收益改善。",
            ]
            for league, counts in impact["leagues"].items():
                lines.append(
                    f"- {_text(league)}：影响 {counts['affected_matches']} 场，"
                    f"恢复 {counts['restored_groups']} 种；"
                    f"原因计数 {_text(str(counts['reasons']))}。"
                )
        if doc.get("previous_version_sha256"):
            lines.append(
                f"替换前版 {_text(doc['previous_version_sha256'][:12])}；"
                f"被替换组 {_text(','.join(doc['replaced_groups']) or '无')}，旧版仍保留。"
            )
    lines += ["", f"本版合格主候选 {displayed} 组；不足不凑数。", ""]
    from zucai.digest_context import observed_results

    ledger, publication_audit = publication_view(
        documents, observed_results(matches), read_ledger(archive, summary), now
    )
    account_summary = (summary or {}).get("parlay_sd")
    if publication_audit["filtered_in_memory"]:
        account_summary = summarize(ledger, observed_results(matches), now)
    lines += render(account_summary).splitlines()
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
