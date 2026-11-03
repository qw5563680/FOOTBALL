"""Read frozen archives and publish a digest, without inference or scoring."""

import argparse
import html
import json
import logging
import os
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from statistics import fmean
from urllib.parse import quote

from zucai.cloud_feeds import archive_info
from zucai.forecast_archive import _path, verify_forecasts
from zucai.report.render import load_batch
from zucai.scoreboard.command import read_state
from zucai.sources.base import _atomic_write

LOG = logging.getLogger(__name__)
BEIJING = timezone(timedelta(hours=8))
DISCLAIMER = "非购买建议 · BET 禁用"
ACCOUNTS = {
    "A": "单场固定额",
    "B": "单场四分之一凯利",
    "C": "热门对照组",
    "D": "二串一",
    "E": "三串一",
}


def _instant(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("摘要来源时间必须含时区")
    return result.astimezone(UTC)


def _day(value):
    return _instant(value).astimezone(BEIJING).date()


def _text(value):
    if value is None:
        return "未记录"
    text = html.escape(str(value)).replace("\n", " ").replace("\r", " ")
    for symbol in ("\\", "|", "[", "]", "`", "*", "_"):
        text = text.replace(symbol, "\\" + symbol)
    return text.replace("@", "&#64;")


def _number(value, places=4, *, percent=False):
    if value is None:
        return "未提供"
    return f"{value * 100 if percent else value:.{places}f}" + ("%" if percent else "")


def _probabilities(row):
    values = row.get("model_probabilities") or {}
    return " / ".join(_number(values.get(key), 2, percent=True) for key in "hda")


def _match(row):
    if row.get("home") and row.get("away"):
        return f"{_text(row['home'])} VS {_text(row['away'])}"
    return _text(row["match_id"])


def _link(path, repository):
    target = (
        f"https://github.com/{repository}/blob/data/football-feeds/{quote(path)}"
        if repository
        else "../" + quote(path)
    )
    return f"[{_text(path)}]({target})"


def _forecast_sections(batches, today, repository):
    lines = ["## 今日与明日预测", ""]
    for offset, label in ((0, "今日"), (1, "明日")):
        day = (today + timedelta(days=offset)).isoformat()
        choices = [entry for entry in batches if entry[0] == day]
        lines.extend([f"### {label} {day}", ""])
        if not choices:
            lines.extend(["尚无冻结预测。", ""])
            continue
        _, path, batch = max(
            choices, key=lambda entry: (_instant(entry[2]["generated_utc"]), entry[2]["batch_id"])
        )
        lines.extend(
            [
                f"来源：{_link(path, repository)}；批次 {_text(batch['batch_id'])}；"
                f"生成 UTC：{_text(batch['generated_utc'])}。",
                "",
            ]
        )
        rows = batch["predictions"]
        if not rows:
            lines.extend(["该批次无比赛。", ""])
        for league in sorted({row.get("league") or "未知联赛" for row in rows}):
            lines.extend(
                [
                    f"#### {_text(league)}",
                    "",
                    "| 北京开球 | 对阵 | 胜 / 平 / 负 | 模型版本 | P2 | 比分前五 |",
                    "| --- | --- | --- | --- | --- | --- |",
                ]
            )
            for row in rows:
                if (row.get("league") or "未知联赛") != league:
                    continue
                kickoff = row.get("kickoff_utc")
                kickoff = (
                    _instant(kickoff).astimezone(BEIJING).strftime("%m-%d %H:%M")
                    if kickoff
                    else "未记录"
                )
                if row["status"] != "predicted":
                    lines.append(
                        f"| {kickoff} | {_match(row)} | 无模型：{_text(row.get('reason'))} "
                        "| — | — | — |"
                    )
                    continue
                scores = (
                    "；".join(
                        f"{_text(score['score'])} {_number(score['probability'], 2, percent=True)}"
                        for score in row.get("top_scores", [])[:5]
                    )
                    or "未提供"
                )
                label = "ready" if row.get("p2_ready") is True else "experimental"
                lines.append(
                    f"| {kickoff} | {_match(row)} | {_probabilities(row)} "
                    f"| {_text(row['model_name'])} / {_text(row['model_version'])} "
                    f"| {label} | {scores} |"
                )
            lines.append("")
    return lines


def _settlement_sections(scored, summary, batches, yesterday, repository):
    lines = ["## 昨日结算", ""]
    names = {
        (batch["batch_id"], row["match_id"]): row
        for _, _, batch in batches
        for row in batch["predictions"]
    }
    lines.extend(
        [
            f"北京日 {yesterday}；按赛果可知时间 available_utc 归日，"
            "只列记分板 active_keys 中的原评分。",
            f"来源：{_link('scoreboard/scored.jsonl', repository)}；"
            f"汇总：{_link('scoreboard/summary.json', repository)}。",
            "",
            "| 比赛 / 记录 ID | 冻结胜 / 平 / 负 | 实际结果 | LogLoss | Brier |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    active = set((summary or {}).get("audit", {}).get("active_keys", []))
    rows = [
        row
        for row in scored
        if row["record_id"] in active and _day(row["available_utc"]) == yesterday
    ]
    for row in rows:
        match = names.get((row["batch_id"], row["match_id"]), row)
        actual = {"h": "主胜", "d": "平局", "a": "客胜"}[row["actual"]]
        lines.append(
            f"| {_match(match)} / {_text(row['record_id'])} | {_probabilities(row)} "
            f"| {row['home_goals']}:{row['away_goals']} {actual} "
            f"| {_text(row['model']['log_loss'])} | {_text(row['model']['brier'])} |"
        )
    if not rows:
        lines.extend(["| 昨日无已结算场次 | — | — | — | — |", ""])
    else:
        # Average the frozen losses before display rounding; never rescore probabilities.
        losses = {key: fmean(row["model"][key] for row in rows) for key in ("log_loss", "brier")}
        lines.extend(
            [
                "",
                f"当日 LogLoss：{_number(losses['log_loss'])}；"
                f"当日 Brier：{_number(losses['brier'])}"
                f"（由当日 {len(rows)} 场逐场评分平均，非记分板原字段）。",
                "逐场评分原值照录，日均仅展示舍入至四位小数。",
            ]
        )
    probability = (summary or {}).get("probability", {})
    total = probability.get("total", {})
    model = total.get("model", {})
    lines.extend(
        [
            f"累计样本：{total.get('n', '未提供')}；累计 LogLoss："
            f"{_number(model.get('log_loss'))}；累计 Brier：{_number(model.get('brier'))}。",
            "",
        ]
    )
    for league, values in sorted(probability.get("leagues", {}).items()):
        status = "已达成（原记分板标记）" if values.get("prospective_verified") else "证据不足"
        lines.append(f"- {_text(league)}：前瞻样本 {values['n']} / 200；{status}。")
    accounts = (summary or {}).get("accounts", {}).get("1.05", {})
    status = (
        "已达成（原记分板标记）"
        if (summary or {}).get("gates", {}).get("simulated_profit_evidence")
        else "证据不足"
    )
    lines.extend(
        [
            f"- 模拟盈利证据（A / 1.05）：已结 {accounts.get('A', {}).get('settled', '未提供')} "
            f"/ 500；{status}。样本进度不替代其余门槛，BET 保持禁用。",
            "",
        ]
    )
    return lines


def _account_sections(ledger, summary, yesterday, repository):
    lines = [
        "## 虚拟账户（1.05 档）",
        "",
        f"来源：{_link('scoreboard/summary.json', repository)} accounts.1.05；"
        f"{_link('scoreboard/ledger.jsonl', repository)}。",
        "资金为虚拟资金；昨日注单指昨日生成或昨日结算，注额与 SP 均照录冻结值。",
        "",
        "| 账户 | 当前资金 | 可用现金 | 累计 ROI | 昨日注单与结果 |",
        "| --- | --- | --- | --- | --- |",
    ]
    accounts = (summary or {}).get("accounts", {}).get("1.05", {})
    for key, name in ACCOUNTS.items():
        values = accounts.get(key, {})
        tickets = []
        for ticket in ledger:
            if (
                ticket.get("record_type") != "ticket"
                or ticket["account"] != key
                or ticket["threshold"] != "1.05"
            ):
                continue
            settlement = ticket.get("settlement") or {}
            settled = settlement.get("settled_utc")
            if _day(ticket["decision_utc"]) != yesterday and (
                not settled or _day(settled) != yesterday
            ):
                continue
            legs = " × ".join(
                f"{_match(leg)} { {'h': '主胜', 'd': '平局', 'a': '客胜'}[leg['selection']] }"
                f" SP={_text(leg['sp'])}"
                for leg in ticket["legs"]
            )
            status = {"won": "中", "lost": "未中"}.get(settlement.get("status"), "待结")
            replay = "（历史补记）" if ticket.get("replayed") else ""
            tickets.append(
                f"{_text(ticket['record_id'])}{replay}：{legs}；"
                f"注额 {_number(ticket['stake'], 2)}；{status}；"
                f"返还 {_number(settlement.get('return'), 2)}；"
                f"盈亏 {_number(settlement.get('profit'), 2)}"
            )
        detail = "；".join(tickets) or ("无已归档的昨日注单" if summary else "尚无记分板")
        lines.append(
            f"| {key} {_text(values.get('name', name))} | "
            f"{_number(values.get('current_funds'), 2)} | "
            f"{_number(values.get('available_cash'), 2)} "
            f"| {_number(values.get('roi'), 2, percent=True)} | {detail} |"
        )
    return lines + [""]


def _anomaly_sections(feed, summary, quota, repository):
    lines = [
        "## 异常与采集记录",
        "",
        f"来源：{_link('feed_manifest.json', repository)} collection。",
        "",
    ]
    collection = feed.get("collection", {})
    for source in ("jc", "fd", "fdorg", "apif"):
        record = collection.get(source, {})
        details = [record["reason"]] if record.get("reason") else []
        details.extend(record.get("errors", []))
        reason = "；".join(_text(detail) for detail in details) or "未记录原因"
        lines.append(
            f"- {source}：{_text(record.get('status'))}；疑似重复 "
            f"{_text(record.get('suspected_duplicates'))}；{reason}。"
        )
    apif = collection.get("apif", {})
    # A jc sync may replace collection metadata. Preserve explicit provenance of quota fallback.
    if quota:
        lines.append(
            f"- 配额快照来源：{_link('state/api_football_quota.json', repository)}；"
            f"UTC 日 {_text(quota.get('date_utc'))}（不是当前实时余额）。"
        )
    lines.append(
        f"- apif 配额剩余：{_text(apif.get('remaining_quota', quota.get('remaining')))}；"
        f"plan：{_text(apif.get('plan', quota.get('plan')))}；"
        f"limit_day：{_text(apif.get('limit_day', quota.get('limit_day')))}。"
    )
    if not summary:
        lines.append("- 尚无记分板；结算、账户与门槛数字未提供，证据不足。")
    elif summary.get("audit", {}).get("result_corrections") or summary.get("audit", {}).get(
        "settlement_corrections"
    ):
        lines.append("- 记分板记录了赛果或结算更正，请查看原 summary.json 的 audit。")
    lines.extend(["", "仅转述冻结数据；缺失不填零，门槛标记不授予 BET 权限。", ""])
    return lines


def generate(archive, out, *, now=None, run_id="local", repository=None):
    """Return dated Markdown path; the only permitted in-archive output is daily/."""
    archive, out = Path(archive).absolute(), Path(out).absolute()
    for path in (archive, out):
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise ValueError("摘要路径不允许符号链接")
    archive, out = archive.resolve(), out.resolve()
    if out.is_relative_to(archive) and out != archive / "daily":
        raise ValueError("归档内只允许写 daily/，不可覆盖输入")
    if repository:
        from zucai.digest_notify import validate_repository

        validate_repository(repository)
    at = _instant(now or datetime.now(UTC))
    today = _day(at)
    feed = archive_info(archive)
    manifest = verify_forecasts(archive)
    scored, ledger, summary, _ = read_state(archive)
    batches = []
    for run in (manifest or {}).get("runs", {}).values():
        for day, entry in run["forecasts"].items():
            if _instant(entry["generated_utc"]) <= at:
                path = entry["path"] + "/forecast.json"
                batches.append((day, path, load_batch(archive / path)))
    quota_path = "state/api_football_quota.json"
    quota = (
        json.loads(_path(archive, quota_path).read_bytes()) if quota_path in feed["files"] else {}
    )
    states = " / ".join(
        f"{source}={_text(feed.get('collection', {}).get(source, {}).get('status'))}"
        for source in ("jc", "fd", "fdorg", "apif")
    )
    lines = [
        DISCLAIMER,
        f"<!-- zucai-daily:{today} -->",
        "",
        f"# 每日摘要 {today}（北京时间）",
        "",
        f"本次 run ID：{_text(run_id)}；摘要生成 UTC：{at.isoformat()}。",
        f"竞彩同步至：{_text(feed.get('through_beijing'))}；采集状态：{states}。",
        f"采集归档 UTC：{_text(feed.get('created_utc'))}；"
        f"记分板截点 UTC：{_text((summary or {}).get('as_of_utc'))}。",
        "缺失状态不推断为成功；以下均为已归档快照，不代表实时信息。",
        "",
        *_forecast_sections(batches, today, repository),
        *_settlement_sections(scored, summary, batches, today - timedelta(days=1), repository),
        *_account_sections(ledger, summary, today - timedelta(days=1), repository),
        *_anomaly_sections(feed, summary, quota, repository),
    ]
    text = "\n".join(lines)
    if "建议购买" in text or "推荐" in text:
        raise ValueError("来源文本含禁止的购买措辞，请检查原归档；未发布摘要")
    out.mkdir(parents=True, exist_ok=True)
    paths = [_path(out, today.isoformat() + ".md"), _path(out, "latest.md")]
    backups = {path: path.read_bytes() if path.exists() else None for path in paths}
    try:
        for path in paths:
            _atomic_write(path, text.encode("utf-8"))
    except BaseException:
        for path, raw in backups.items():
            if raw is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write(path, raw)
        raise
    LOG.info("摘要已生成：%s", paths[0])
    return paths[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path, help="daily 输出目录")
    parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", "local"))
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        generate(args.archive, args.out, run_id=args.run_id, repository=args.repository)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        LOG.error("摘要未生成：%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
