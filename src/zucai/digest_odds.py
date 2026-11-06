"""Read-only odds provenance; collection checks never substitute for quote timestamps."""

from zucai.digest_display import BEIJING, _instant, _text

HISTORICAL_WARNING = "本次未取得新的有效赔率，以下使用历史快照，不代表当前可购买价格。"


def _timestamp(value):
    try:
        return _instant(value) if value else None
    except (TypeError, ValueError, OverflowError):
        return None


def same_collection_attempt(archived_run, run_id, jc_sync_run_id):
    """Actions passes a bare workflow ID and a separate ID-attempt digest run."""
    if not isinstance(run_id, str) or jc_sync_run_id is None:
        return False
    workflow, _, attempt = run_id.rpartition("-")
    return (
        workflow == str(jc_sync_run_id)
        and attempt.isascii()
        and attempt.isdecimal()
        and int(attempt) > 0
        and archived_run == run_id
    )


def collection_lines(
    feed, now, *, run_id=None, jc_sync_status=None, jc_sync_run_id=None, jc_publish_status=None
):
    jc = feed.get("collection", {}).get("jc", {})
    if jc_sync_status == "skipped":
        lines = ["本次未执行采集；以下单列归档最近一次采集，不算作本次新增。"]
    elif all(value is None for value in (jc_sync_status, jc_sync_run_id, jc_publish_status)):
        lines = []  # Local read-only rendering has no evidence of a current collection.
    elif (
        same_collection_attempt(jc.get("run_id"), run_id, jc_sync_run_id)
        and jc_publish_status == "success"
    ):
        return _record_lines(jc, now, label="本次")
    else:
        lines = [
            f"本次竞彩接口采集：结果未知（run {_text(run_id)}）；本次结果未确认发布，"
            "归档记录不能代替本次结果。",
            "本次新增入库有效赔率：未知；运行编号、重试次数及发布成功记录须同时核实。",
        ]
    return lines + _record_lines(jc, now, label="归档最近一次")


def _record_lines(jc, now, *, label):
    stamp = _timestamp(jc.get("schedule_updated_utc"))
    status = jc.get("schedule_status")
    complete = status == "complete" and "schedule" not in (jc.get("failed_stages") or [])
    usable = stamp is not None and stamp <= now
    if not usable:
        collection = "未知（缺少有效阶段或捕获时间记录）"
    elif complete:
        collection = "成功（仅指赛程/赔率接口检查完成，不代表赔率有更新）"
    elif status == "failed" or "schedule" in (jc.get("failed_stages") or []):
        collection = "失败（保留上次成功检查时间）"
    else:
        collection = "未知（未记录完整成功的接口采集）"
    lines = [f"{label}竞彩接口采集：{collection}；归档记录 run {_text(jc.get('run_id'))}。"]
    if label == "归档最近一次":
        checked = stamp.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S") if usable else "未知"
        lines.append(f"归档最近一次接口成功检查：{checked}（北京时间）。")
    count = jc.get("odds_snapshots")
    if not (usable and complete and type(count) is int and count >= 0):
        lines.append(f"{label}新增入库有效赔率：未知；缺少可核实的新报价记录，不推断为零更新。")
        return lines
    lines.append(
        f"{label}新增入库有效赔率：{count} 条（各玩法选项记录数，不是比赛场数）。"
        "成功检查或有新增记录均不代表所有比赛均已更新。"
    )
    if count == 0:
        warning = (
            HISTORICAL_WARNING
            if label == "本次"
            else HISTORICAL_WARNING.replace("本次", "归档最近一次采集", 1)
        )
        lines.append(warning)
        lines.append(
            "零新增也可能包含重复报价被去重，不能仅凭此数判断停售；销售状态以原件及跳过审计为准。"
        )
    return lines


def odds_time(row, *, checked_at=None):
    market = row.get("market")
    if not isinstance(market, dict) or not market:
        return "未冻结赔率"
    stamp = _timestamp(market.get("snapshot_utc"))
    if stamp is None:
        return "未知（冻结记录缺少有效赔率时间）"
    value = stamp.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S")
    checked = _timestamp(checked_at)
    if checked is not None and stamp < checked:
        value += "（历史快照）"
    return value


def batch_odds_lines(batch, source_run):
    rows = [row for row in batch["predictions"] if row.get("status") == "predicted"]
    if not rows:
        return []
    quoted = [row for row in rows if isinstance(row.get("market"), dict) and row["market"]]
    known = sum(_timestamp(row["market"].get("snapshot_utc")) is not None for row in quoted)
    lines = [
        f"本批冻结赔率：{known} 场有采集时间；{len(quoted) - known} 场时间未知；"
        f"{len(rows) - len(quoted)} 场未冻结赔率。"
        "逐场时间见表，不用批次生成或归档时间代替，不代表当前可购买价格。",
    ]
    checked_at = (source_run.get("feed_snapshot") or {}).get("schedule_updated_utc")
    if any("历史快照" in odds_time(row, checked_at=checked_at) for row in quoted):
        lines.append("本批有场次沿用接口检查前的历史赔率；采集成功不表示这些场次的赔率已更新。")
    return lines + [""]
