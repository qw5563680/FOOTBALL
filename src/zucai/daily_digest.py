"""Read frozen archives and publish a digest, without inference or scoring."""

import argparse
import hashlib
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import fmean

from zucai.cloud_feeds import archive_info
from zucai.digest_context import archived_matches, audit_rows, frozen_rows, pending_tickets
from zucai.digest_display import (
    _day,
    _instant,
    _link,
    _match,
    _number,
    _probabilities,
    _text,
)
from zucai.digest_history import source_hashes, version_context
from zucai.digest_views import (
    forecast_sections as _forecast_sections,
)
from zucai.digest_views import (
    header_lines,
    pending_sections,
    review_sections,
)
from zucai.forecast_archive import _path, verify_forecasts
from zucai.report.render import load_batch
from zucai.scoreboard.command import read_state
from zucai.sfc_digest import section as sfc_section
from zucai.sources.base import _atomic_write

LOG = logging.getLogger(__name__)
DISCLAIMER = "非购买建议 · BET 禁用"
ACCOUNTS = {
    "A": "单场固定额",
    "B": "单场四分之一凯利",
    "C": "热门对照组",
    "D": "二串一",
    "E": "三串一",
}


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
        "资金为虚拟资金；投入仅指已结注单，盈亏为记分板原返还减原已结投入；回报率照录原值。"
        "昨日注单指昨日生成或昨日结算，注额与 SP 均照录冻结值。",
        "",
        "| 账户 | 已结 / 投入 / 盈亏 / 回报率 | 当前资金 / 可用现金 | 昨日注单与结果 |",
        "| --- | --- | --- | --- |",
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
        invested, returns = values.get("settled_stakes"), values.get("returns")
        profit = None if invested is None or returns is None else returns - invested
        lines.append(
            f"| {key} {_text(values.get('name', name))} | "
            f"已结 {values.get('settled', '未提供')} 注，投入 {_number(invested, 2)}，"
            f"盈亏 {format(profit, '+.2f') if profit is not None else '未提供'}，"
            f"回报率 {_number(values.get('roi'), 2, percent=True)} | "
            f"{_number(values.get('current_funds'), 2)} / "
            f"{_number(values.get('available_cash'), 2)} | {detail} |"
        )
    return lines + [""]


def _anomaly_sections(feed, summary, quota, repository, coverage=None):
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
    # Legacy/manual archives can lack quota metadata; preserve the fallback's provenance.
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
    if coverage is not None:
        lines.extend(
            [
                "",
                "### 技术数据覆盖率",
                "",
                f"来源：{_link('state/stats_coverage.json', repository)}；"
                f"观测 UTC：{_text(coverage.get('observed_utc'))}。数字照录归档，不重算覆盖率。",
                "",
                "| 联赛 / 赛季 / 端点 / 阶段 | 应取 | 已取 | 有效 | 空值 | 失败 | 未支持 "
                "| captured 行数 |",
                "| --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for row in coverage["rows"]:
            scope = " / ".join(
                _text(row.get(key)) for key in ("league", "season", "endpoint", "phase")
            )
            counts = " | ".join(
                _text(row.get(key))
                for key in ("expected", "fetched", "valid", "null", "failed", "unsupported")
            )
            lines.append(f"| {scope} | {counts} | {_text(row.get('captured_rows'))} |")
        lines.append(
            "captured 按联赛季端点的存储行版本计数，同范围不同阶段不相加；旧快照未记录则显示缺失。"
        )
        injuries = coverage.get("injuries")
        if injuries is not None:
            lines.extend(
                [
                    "",
                    "伤停入库（存储版本数；不进入正式预测）：",
                    "",
                    "| captured | backfilled | 已关联 / 总行数 | 关联率 |",
                    "| --- | --- | --- | --- |",
                    f"| {_text(injuries.get('captured_rows'))} | "
                    f"{_text(injuries.get('backfilled_rows'))} | "
                    f"{_text(injuries.get('linked_rows'))} / {_text(injuries.get('total_rows'))} | "
                    f"{_text(injuries.get('linkage_rate'))} |",
                    "空接口或没有名单表示缺失，不能解读为没有伤停。",
                ]
            )
    lines.extend(["", "仅转述冻结数据；缺失不填零，门槛标记不授予 BET 权限。", ""])
    return lines


def generate(
    archive,
    out,
    *,
    now=None,
    run_id="local",
    repository=None,
    jc_sync_status=None,
    jc_sync_run_id=None,
    jc_publish_status=None,
    require_notified_history=False,
):
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
    manifest = verify_forecasts(archive, allow_unknown_odds_time=True)
    scored, ledger, summary, _ = read_state(archive)
    # Experimental scores share the append-only score file but never the original
    # yesterday totals, paper ledger or production evidence gates.
    scored = [r for r in scored if r.get("validation_status") != "experimental_unadopted"]
    from zucai.scoreboard.experimental import render_experiments

    batches = []
    for run in (manifest or {}).get("runs", {}).values():
        for day, entry in run["forecasts"].items():
            if _instant(entry["generated_utc"]) <= at:
                path = entry["path"] + "/forecast.json"
                batches.append(
                    (day, path, load_batch(archive / path, allow_unknown_odds_time=True))
                )
    matches = archived_matches(archive, feed)
    # No rows known at the cutoff means no evidence, not a checked empty scope.
    # Only scoped_catalog below may produce [] after usable evidence was found.
    catalog = audit_rows(archive, feed, "jc_catalog", at, all_shards=True) or None
    skipped = audit_rows(archive, feed, "jc_skipped", at, all_shards=True)
    if catalog is not None:
        from zucai.digest_context import scoped_catalog

        catalog = scoped_catalog(catalog, at)
        # Enrichment by exact archived ID only; never name matching.
        for row in catalog:
            if row["match_id"] in matches:
                matches[row["match_id"]].setdefault("business_date", row["business_date"])
    latest, observations = frozen_rows(batches, matches, at)
    from zucai.digest_context import prediction_window

    window = prediction_window(batches, at)
    inputs = {
        **source_hashes(archive),
        "jc_sync_status": jc_sync_status,
        "jc_sync_run_id": jc_sync_run_id,
        "jc_publish_status": jc_publish_status,
        "repository": repository,
        "require_notified_history": require_notified_history,
    }
    previous, update, version_id = version_context(
        archive, out, today, at, run_id, inputs, require_notified=require_notified_history
    )
    pending = pending_tickets(ledger, at)
    from zucai.digest_parlay import section as parlay_section

    parlay_lines, parlay_pending, parlay_versions = parlay_section(
        archive, manifest, summary, at, repository, matches=matches, previous=previous
    )
    quota_path = "state/api_football_quota.json"
    quota = (
        json.loads(_path(archive, quota_path).read_bytes()) if quota_path in feed["files"] else {}
    )
    coverage_path = "state/stats_coverage.json"
    coverage = (
        json.loads(_path(archive, coverage_path).read_bytes())
        if coverage_path in feed["files"]
        else None
    )
    if coverage is not None and (
        not isinstance(coverage, dict)
        or coverage.get("version") != 1
        or not isinstance(coverage.get("rows"), list)
        or any(not isinstance(row, dict) for row in coverage["rows"])
    ):
        raise ValueError("技术覆盖率快照格式无效")
    states = " / ".join(
        f"{source}={_text(feed.get('collection', {}).get(source, {}).get('status'))}"
        for source in ("jc", "fd", "fdorg", "apif")
    )
    displayed_ids = set()
    lines = [
        DISCLAIMER,
        f"<!-- zucai-daily:{today} -->",
        "",
        f"# 每日摘要 {today}（北京时间）",
        "",
        f"本日第 {update} 次更新；本次 run ID：{_text(run_id)}；摘要生成 UTC：{at.isoformat()}。",
        *header_lines(
            feed,
            latest,
            matches,
            at,
            jc_sync_status=jc_sync_status,
            jc_sync_run_id=jc_sync_run_id,
            jc_publish_status=jc_publish_status,
        ),
        f"采集状态：{states}。",
        f"采集归档 UTC：{_text(feed.get('created_utc'))}；"
        f"记分板截点 UTC：{_text((summary or {}).get('as_of_utc'))}。",
        "缺失状态不推断为成功；以下均为已归档快照，不代表实时信息。",
        "",
        *_forecast_sections(
            batches,
            today,
            repository,
            now=at,
            matches=matches,
            displayed_ids=displayed_ids,
            runs=(manifest or {}).get("runs", {}),
        ),
        *sfc_section(archive, feed, summary, at, lambda path: _link(path, repository)),
        *parlay_lines,
        *review_sections(
            latest,
            observations,
            matches,
            scored,
            summary,
            catalog,
            skipped,
            at,
            displayed_ids=displayed_ids,
            window=window,
        ),
        *_settlement_sections(scored, summary, batches, today - timedelta(days=1), repository),
        *_account_sections(ledger, summary, today - timedelta(days=1), repository),
        *pending_sections(pending, ledger, previous, at),
        *render_experiments((summary or {}).get("experiments")).splitlines(),
        *_anomaly_sections(feed, summary, quota, repository, coverage),
    ]
    text = "\n".join(lines)
    if "建议购买" in text or "推荐" in text:
        raise ValueError("来源文本含禁止的购买措辞，请检查原归档；未发布摘要")
    out.mkdir(parents=True, exist_ok=True)
    dated = _path(out, today.isoformat() + ".md")
    version_path = _path(out, f"{today}/{version_id}")
    version_markdown = version_path.with_suffix(version_path.suffix + ".md")
    version_json = version_path.with_suffix(version_path.suffix + ".json")
    receipt = {
        "schema_version": 1,
        "date": today.isoformat(),
        "run_id": run_id,
        "version_id": version_id,
        "generated_utc": at.isoformat(),
        "update_number": update,
        "inputs": inputs,
        "pending": pending,
        "parlay_pending": parlay_pending,
        "parlay_versions": parlay_versions,
        "previous_version": previous["version_id"] if previous else None,
        "markdown_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
    content = {
        dated: text.encode("utf-8"),
        _path(out, "latest.md"): text.encode("utf-8"),
        version_markdown: text.encode("utf-8"),
        version_json: (
            json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8"),
    }
    for path in (version_markdown, version_json):
        if path.exists() and path.read_bytes() != content[path]:
            raise ValueError("不可覆盖已有日报版本")
    paths = list(content)
    backups = {path: path.read_bytes() if path.exists() else None for path in paths}
    directory_existed = version_path.parent.exists()
    try:
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(path, content[path])
    except BaseException:
        for path, raw in backups.items():
            if raw is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write(path, raw)
        if not directory_existed and version_path.parent.exists():
            version_path.parent.rmdir()
        raise
    LOG.info("摘要已生成：%s", paths[0])
    return paths[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path, help="daily 输出目录")
    parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", "local"))
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--jc-sync-status", choices=("success", "failure", "cancelled", "skipped"))
    parser.add_argument("--jc-sync-run-id")
    parser.add_argument(
        "--jc-publish-status", default=os.environ.get("SPORTTERY_PUBLISH_STATUS") or None
    )
    parser.add_argument("--require-notified-history", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        generate(
            args.archive,
            args.out,
            run_id=args.run_id,
            repository=args.repository,
            jc_sync_status=args.jc_sync_status,
            jc_sync_run_id=args.jc_sync_run_id,
            jc_publish_status=args.jc_publish_status,
            require_notified_history=args.require_notified_history,
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        LOG.error("摘要未生成：%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
