"""T71 只读命令：一场比赛的市场参考完整出处页。

只读：归档与恢复出的数据库都不做任何修改；真实样例由审查者在真实归档上执行后贴到 PR。
"""

import json
from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy import select

from zucai.models import Match, Odds, Team

BEIJING_TZ = "Asia/Shanghai"
OPERATOR = "竞彩官方"
SUPPLIER = "中国体育彩票（竞彩官网）"
SOURCE_QUOTE_TIME_MISSING = "来源未提供"
OPENING_MISSING = "来源未提供初盘"
BETFAIR_STATUSES = (
    ("成交价", "权限未核实"),
    ("成交量", "权限未核实"),
    ("盘口深度", "权限未核实"),
)


def _beijing(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    from zoneinfo import ZoneInfo

    return stamp.astimezone(ZoneInfo(BEIJING_TZ)).strftime("%Y-%m-%d %H:%M:%S")


def _forecast_records(archive, match_id):
    """Every frozen prediction record of the match, one per published batch.

    The production archive registers batches in the root ``forecast_manifest.json``:
    ``runs.<run_id>.forecasts.<date>`` entries carry ``path`` / ``batch_id`` /
    ``generated_utc``, and the prediction file lives at ``<path>/forecast.json``.
    A match may appear in several batches; each is listed separately. Batches
    published before this task carry no ``snapshot_id`` — their adopted
    snapshot is the market ``snapshot_utc``.
    """
    manifest_path = archive / "forecast_manifest.json"
    if not manifest_path.is_file():
        return []
    manifest = json.loads(manifest_path.read_bytes())
    records = []
    for run_id, run in (manifest.get("runs") or {}).items():
        for entry in (run.get("forecasts") or {}).values():
            path = archive / entry["path"] / "forecast.json"
            if not path.is_file():
                continue
            batch = json.loads(path.read_bytes())
            for row in batch.get("predictions", []):
                if row.get("match_id") == match_id:
                    records.append((row, path, run_id, entry))
    return records


def page(session, archive, match_id):
    """Read-only provenance page for one match's market reference."""
    match = session.get(Match, match_id)
    if match is None:
        raise ValueError(f"归档内找不到比赛 {match_id}")
    home = session.get(Team, match.home_team_id)
    away = session.get(Team, match.away_team_id)
    rows = session.scalars(
        select(Odds)
        .where(Odds.match_id == match_id, Odds.pool == "had", Odds.line == 0)
        .order_by(Odds.snapshot_utc)
    )
    snapshots = defaultdict(dict)
    for row in rows:
        if row.selection in "hda" and len(row.selection) == 1:
            snapshots[row.snapshot_utc][row.selection] = row.price
    records = _forecast_records(archive, match_id)
    first_capture = min(snapshots, default=None)
    if records:
        for record, _path, _run_id, _entry in records:
            market = (
                record.get("market")
                if record.get("status") == "predicted"
                else record.get("market_reference")
            )
            if market and market.get("first_capture_utc"):
                first_capture = min(
                    first_capture or market["first_capture_utc"], market["first_capture_utc"]
                )
    adopted_stamps = set()
    for record, _path, _run_id, _entry in records:
        market = (
            record.get("market")
            if record.get("status") == "predicted"
            else record.get("market_reference")
        )
        if market:
            adopted_stamps.add(
                datetime.fromisoformat(market.get("snapshot_id") or market.get("snapshot_utc"))
            )

    lines = [
        f"# 市场参考出处页：{match_id}",
        "",
        f"- 比赛：{home.name if home else '?'} VS {away.name if away else '?'}；"
        f"竞彩编号：{match.jc_num or '未提供'}；开球（北京）：{_beijing(match.kickoff_utc)}。",
        f"- 数据供应商：{SUPPLIER}；运营商：{OPERATOR}。",
    ]
    if not records:
        lines.append("- 球队模型：归档批次中未找到该场比赛的冻结预测记录。")
        lines.append("- 市场参考：未生成（无冻结预测记录可查）。")
    else:
        for record, path, run_id, entry in records:
            lines.append("")
            lines.append(f"## 冻结批次 {run_id}")
            lines.append("")
            lines.append(
                f"- 冻结预测记录：{path.relative_to(archive)}；"
                f"批次生成 UTC：{entry.get('generated_utc') or '未提供'}。"
            )
            market = (
                record.get("market")
                if record.get("status") == "predicted"
                else record.get("market_reference")
            )
            adopted = (market or {}).get("snapshot_id") or (market or {}).get("snapshot_utc")
            lines.append(f"- 该批采用的胜平负快照：{adopted or '无'}。")
            model = record.get("model_probabilities")
            if record.get("status") == "predicted" and model:
                lines.append(
                    "- 球队模型概率：主胜 {:.2f} / 平 {:.2f} / 客胜 {:.2f}。".format(
                        model["h"], model["d"], model["a"]
                    )
                )
            else:
                lines.append(
                    f"- 球队模型：球队模型未生成（{record.get('reason') or '未提供原因'}）。"
                )
            if market:
                probs = market.get("probabilities", {})
                lines.append(
                    "- 市场参考概率：主胜 {:.2f} / 平 {:.2f} / 客胜 {:.2f}；"
                    "返还率 {:.4f}；方法版本 {}。".format(
                        probs.get("h", float("nan")),
                        probs.get("d", float("nan")),
                        probs.get("a", float("nan")),
                        market.get("return_rate", float("nan")),
                        market.get("method_version", "未知"),
                    )
                )
            else:
                missing = record.get("market_missing_reason") or "未提供原因"
                lines.append(f"- 市场参考：市场参考未生成（{missing}）。")
    lines.extend(
        [
            "",
            f"- 首次采集：{first_capture or '无'}；初盘：{OPENING_MISSING}。",
            "",
            f"## 胜平负报价快照（{OPERATOR}）",
            "",
            "| 项目采集时间（北京） | 来源报价时间 | 主胜 | 平 | 客胜 | 标记 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for stamp in sorted(snapshots):
        prices = snapshots[stamp]
        instant = datetime.fromisoformat(stamp)
        marks = []
        if first_capture and instant == datetime.fromisoformat(first_capture):
            marks.append("首次采集")
        if instant in adopted_stamps:
            marks.append("预测采用")
        lines.append(
            "| {} | {} | {} | {} | {} | {} |".format(
                _beijing(stamp),
                SOURCE_QUOTE_TIME_MISSING,
                prices.get("h", "—"),
                prices.get("d", "—"),
                prices.get("a", "—"),
                "、".join(marks) if marks else "—",
            )
        )
    if not snapshots:
        lines.append("| 无已采集快照 | — | — | — | — | — |")
    lines.extend(["", "## 必发", "", "| 字段 | 状态 |", "| --- | --- |"])
    lines.extend(f"| {field} | {status} |" for field, status in BETFAIR_STATUSES)
    return "\n".join(lines) + "\n"
