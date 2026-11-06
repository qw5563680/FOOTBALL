"""Read-only odds provenance; collection checks never substitute for quote timestamps."""

from zucai.digest_display import BEIJING, _instant, _text

HISTORICAL_WARNING = "本次未取得新的有效赔率，以下使用历史快照，不代表当前可购买价格。"


def _timestamp(value):
    try:
        return _instant(value) if value else None
    except (TypeError, ValueError, OverflowError):
        return None


def collection_lines(feed, now, *, jc_sync_run_id=None, jc_publish_status=None):
    jc = feed.get("collection", {}).get("jc", {})
    run = jc.get("run_id")
    same_run = (
        jc_sync_run_id is None
        or str(run) == str(jc_sync_run_id)
        or str(run).startswith(f"{jc_sync_run_id}-")
    )
    stamp = _timestamp(jc.get("schedule_updated_utc"))
    status = jc.get("schedule_status")
    complete = status == "complete" and "schedule" not in (jc.get("failed_stages") or [])
    usable = same_run and jc_publish_status != "failure" and stamp is not None and stamp <= now
    label = "本次" if jc_sync_run_id else "归档最近一次"
    if not same_run or jc_publish_status == "failure":
        collection = "本次结果未确认发布，归档记录不能代替本次结果"
    elif not usable:
        collection = "未知（缺少有效阶段或捕获时间记录）"
    elif complete:
        collection = "成功（仅指赛程/赔率接口检查完成，不代表赔率有更新）"
    elif status == "failed" or "schedule" in (jc.get("failed_stages") or []):
        collection = "失败（保留上次成功检查时间）"
    else:
        collection = "未知（未记录完整成功的接口采集）"
    lines = [f"{label}竞彩接口采集：{collection}；归档记录 run {_text(run)}。"]
    count = jc.get("odds_snapshots")
    if not (usable and complete and type(count) is int and count >= 0):
        lines.append(f"{label}新增入库有效赔率：未知；缺少可核实的新报价记录，不推断为零更新。")
        return lines
    lines.append(
        f"{label}新增入库有效赔率：{count} 条（各玩法选项记录数，不是比赛场数）。"
        "成功检查或有新增记录均不代表所有比赛均已更新。"
    )
    if count == 0:
        lines.append(HISTORICAL_WARNING)
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
