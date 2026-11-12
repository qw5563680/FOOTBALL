"""Plain-text probability and virtual-account evidence, never execution guidance."""

from zucai.report.render import _md
from zucai.scoreboard.score import instant
from zucai.sources.sporttery import BEIJING

DISCLAIMER = "非购买建议 · BET 禁用 · 虚拟资金模拟 · 竞彩含抽水"
SINGLE_BIAS = "单关偏差：本模拟假设均可单关，竞彩部分场次实际不开单关；未模拟奖金封顶等其他规则。"


def number(value, percent=False):
    if value is None:
        return "—"
    return f"{value:.2%}" if percent else f"{value:.4f}"


def _quality_row(label, value):
    metrics = [
        number(value[column][metric])
        for column in ("model", "uniform", "market")
        for metric in ("log_loss", "brier")
    ]
    return (
        "| " + " | ".join([_md(label), str(value["n"]), str(value["market"]["n"]), *metrics]) + " |"
    )


def _quality_table():
    return [
        "| 范围 | 前瞻 n | 市场 n | 模型 LL | 模型 Brier |"
        " 等概率 LL | 等概率 Brier | 市场 LL | 市场 Brier |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]


def _accounts(accounts):
    lines = [
        "| 账户 | 注数 / 待结 | 命中 | 投入 / 已结投入 | 回报 | 回报率 |"
        " 当前资金 / 可用 | 最大回撤 | 最长连亏 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for key, a in accounts.items():
        lines.append(
            f"| {key} {_md(a['name'])} | {a['bets']} / {a['pending']} | {a['hits']} | "
            f"{a['invested']:.2f} / {a['settled_stakes']:.2f} | {a['returns']:.2f} | "
            f"{number(a['roi'], True)} | {a['current_funds']:.2f} / {a['available_cash']:.2f} | "
            f"{number(a['max_drawdown'], True)} | {a['longest_losing_streak']} |"
        )
    return lines


def render_scoreboard(summary):
    quality, accounts = summary["probability"], summary["accounts"]
    passed = [name for name, value in quality["leagues"].items() if value["prospective_verified"]]
    lines = [
        DISCLAIMER,
        "",
        SINGLE_BIAS,
        "竞彩 SP 含抽水；五种虚拟规则不表示现实可执行或具有投资价值。",
        "默认门槛 1.05：严格要求 概率 × 冻结 SP > 1.05，即预期收益超过 5%；另两档独立统计。",
        "回报率只计已结注单；未结注额占用可用资金，当前资金按已结盈亏计算。",
        "历史批次的虚拟注单标记为事后重放，冻结预测本身的真实生成时间保持原样。",
        f"截至北京时间：{instant(summary['as_of_utc']).astimezone(BEIJING).isoformat()}。",
        f"前瞻已验证联赛：{_md('、'.join(passed)) if passed else '无'}；模拟盈利证据："
        + (
            "达到模拟门槛，待所有者决策"
            if summary["gates"]["simulated_profit_evidence"]
            else "未达成"
        ),
        "",
        "## 概率质量",
        "",
        *_quality_table(),
    ]
    lines.append(_quality_row("总计", quality["total"]))
    for league, value in quality["leagues"].items():
        label = (
            league
            + " · "
            + "/".join(value["p2_labels"])
            + " · "
            + ("前瞻已验证" if value["prospective_verified"] else "仅历史证据")
        )
        lines.append(_quality_row(label, value))
    if "prior_strata" in quality:
        lines += [
            "",
            "### 默认实力分层",
            "",
            "沿用相同有效预测和原评分；未记录不能视为双方均已知，不改变原总分或验证门槛。",
            "",
            *_quality_table(),
        ]
        for key, label in (
            ("default_strength", "使用默认实力"),
            ("known_teams", "双方均已知"),
            ("unrecorded", "是否使用默认实力未记录"),
        ):
            lines.append(_quality_row(label, quality["prior_strata"][key]))
    lines += ["", "## 虚拟账户 · 默认 1.05", "", *_accounts(accounts["1.05"]), ""]
    for league, value in quality["leagues"].items():
        if value["drift"]["warning"]:
            lines.append(
                f"🟡 {_md(league)} 漂移告警：连续四个完整周平局偏差超 5 个百分点，"
                "或近 100 场 LL 高于等概率；请复核并重跑 tune。本任务不会自动更改模型。"
            )
    lines += [
        "",
        "## 口径与未完成项",
        "",
        "只计开球前最后一批真实冻结预测；赛果须在预测生成之后被实际观察到。"
        "假设时间、事后生成、身份变化及未知赛果不计前瞻样本。"
        "市场指标只覆盖有完整冻结 SP 的场次；同场模型对照另见 summary.json。",
        "评分的原始追加记录不改写；后到的更新批次在汇总中按比赛去重。纸面账户每场/每天首次有效注单冻结，后续批次不替换，因此与最后一批概率评分的口径不同。",
        "",
    ]
    audit = summary["audit"]
    lines.append(
        f"本次新增评分 {audit['newly_scored']}；等待或排除 {sum(audit['excluded'].values())}；"
        f"赛果更正待复核 {len(audit['result_corrections'])}；"
        f"已结注单比分更正待复核 {len(audit['settlement_corrections'])}。"
    )
    reasons = {
        "insufficient_candidates": "串关候选不足",
        "insufficient_cash": "可用资金或凯利上下限不满足",
        "no_edge_above_threshold": "没有超过门槛的选项",
        "no_frozen_sp": "没有完整冻结 SP",
        "model_not_p2_ready": "模型为 P2 实验，不下注",
        "result_already_known": "生成时已有赛果",
        "unknown_team_prior": "使用默认实力，移出串关主候选",
    }
    for reason, count in audit["paper_skips"].items():
        lines.append(f"- {_md(reasons.get(reason, reason))}：{count} 条记录。")
    lines += [
        f"- 迟到批次未补开历史注单：{audit['late_batches']} 批；旧注单注额与赔率保持冻结。",
        "",
        "## 稳健性",
        "",
        "仅用已结注单；去掉按总回报排序最高的 ceil(5%×注数) 注；"
        "固定种子 20261103，bootstrap 1000 次。空样本及删后空集保留未知。"
        "串关之间可能相关，本区间是逐注抽样描述，不是盈利保证。",
        "",
        "| 门槛 | 账户 | 去掉前 5% 后回报率 | 回报率 95% 区间 |",
        "| --- | --- | ---: | --- |",
    ]
    for threshold in ("1.05", "1.00", "1.10"):
        for key, value in accounts[threshold].items():
            low, high = value["roi_ci95"]
            lines.append(
                f"| {threshold} | {key} | {number(value['trimmed_roi'], True)} | "
                f"{number(low, True)} ～ {number(high, True)} |"
            )
    for threshold in ("1.00", "1.10"):
        lines += ["", f"## 附表 · 门槛 {threshold}", "", *_accounts(accounts[threshold])]
    lines += [
        "",
        "## 每周资金",
        "",
        "周一为北京周界；每周最后一个可用时刻，未结本金保留在资金中并从可用资金扣除。",
        "",
        "| 门槛 | 账户 | 北京周起始 | 当前资金 | 可用资金 |",
        "| --- | --- | --- | ---: | ---: |",
    ]
    for threshold in ("1.05", "1.00", "1.10"):
        for key, value in accounts[threshold].items():
            for point in value["weekly_curve"]:
                lines.append(
                    f"| {threshold} | {key} | {point['week']} | "
                    f"{point['equity']:.2f} | {point['cash']:.2f} |"
                )
    lines += ["", "## 按联赛与模型版本", "", *_quality_table()]
    for value in quality["league_versions"]:
        lines.append(_quality_row(value["league"] + " / " + value["model_version"], value))
    lines += ["", "## 按月", "", *_quality_table()]
    for month, value in quality["months"].items():
        lines.append(_quality_row(month, value))
    lines += [
        "",
        "## 10 桶校准与平局偏差",
        "",
        "每场三个选项分别入桶；空桶为未知，桶内 n 合计为三倍场数。"
        "平局偏差 = 平均预测平局概率 − 实际平局率。",
    ]
    for league, value in quality["leagues"].items():
        lines += [
            "",
            f"### {_md(league)}",
            "",
            "| 概率列 | 桶下界 | 选项 n | 平均概率 | 实际频率 |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
        for column, label in (("model", "模型"), ("uniform", "等概率"), ("market", "冻结市场")):
            for bucket in value[column]["calibration"]:
                lines.append(
                    f"| {label} | {bucket['lower']:.1f} | {bucket['n']} | "
                    f"{number(bucket['mean_p'], True)} | {number(bucket['observed'], True)} |"
                )
            lines.append(f"\n{label}平局偏差：{number(value[column]['draw_bias'], True)}。\n")
    lines += [
        "",
        "门槛：联赛前瞻 n ≥ 200 且模型 LL ≤ 等概率 LL；模拟门槛仅看 A / 1.05，"
        "已结注数 ≥ 500、回报率和去掉前 5% 后回报率均 > 0、bootstrap 95% 下限 > −2%。"
        "达到模拟门槛仍不改变 BET 或任何默认值。",
    ]
    return "\n".join(lines) + "\n"
