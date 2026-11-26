"""Display the frozen forecast coverage list outside all selection tables."""

from zucai.digest_display import BEIJING, _instant, _link, _match, _number, _text
from zucai.forecast_coverage import coverage_summary


def _ordered_probabilities(values):
    return " / ".join(_number(values[key], 2, percent=True) for key in "hda") if values else "—"


def sections(coverage, repository):
    if coverage is None:
        return [], []

    header = [f"{_text(coverage_summary(coverage))}。", ""]
    lines = ["## 预测覆盖明细", ""]
    if coverage_cutoff := coverage.get("coverage_as_of_utc"):
        cutoff = _instant(coverage_cutoff).astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S")
        lines.extend([f"清单数据截点为北京时间 {cutoff}。", ""])
    if path := coverage.get("coverage_path"):
        lines.extend([f"逐场证据与待办：{_link(path, repository)}。", ""])
    if not any(rows for scope in ("sfc", "jc") for rows in coverage[scope].values()):
        return header, [*lines, "本次清单无场次。", ""]

    lines.extend(
        [
            "任九、复式、纸面串关和虚拟账户只读取各自流程的球队模型冻结记录。",
            "",
            "| 清单 | 编号 | 北京开球 / 对阵 | 状态 | 正式主胜 / 平 / 客胜 "
            "| 基准主胜 / 平 / 客胜 | 正式缺口 / 待办 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for scope in ("sfc", "jc"):
        for group, rows in sorted(coverage[scope].items()):
            label = _text(f"14 场 {group} 期" if scope == "sfc" else f"竞彩 {group}")
            for row in rows:
                number = f"第 {row['seq']} 场" if scope == "sfc" else _text(row.get("jc_num"))
                kickoff = row.get("kickoff_utc")
                time = (
                    _instant(kickoff).astimezone(BEIJING).strftime("%Y-%m-%d %H:%M")
                    if kickoff
                    else "开球时间待核"
                )
                status, formal, probabilities, gap = "正式预测", "—", "—", "—"
                if row["formal"]:
                    formal = _ordered_probabilities(row.get("formal_probabilities"))
                else:
                    baseline = row.get("baseline")
                    status = "基准（无球队信息）" if baseline else "未生成"
                    if baseline:
                        probabilities = _ordered_probabilities(baseline["probabilities"])
                    gap = (
                        "；".join(
                            _text(row[key])
                            for key in ("reason", "baseline_unavailable", "action")
                            if row.get(key)
                        )
                        or "未记录"
                    )
                lines.append(
                    f"| {label} | {number} | {time} / {_match(row)} | {status} "
                    f"| {formal} | {probabilities} | {gap} |"
                )
    return header, [*lines, ""]
