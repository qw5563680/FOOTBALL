"""Exercise the fallback decision off-line using captured pages and scoped match evidence."""

from dataclasses import asdict
from datetime import datetime

from zucai.okooo_probe import ORIGIN, compare_sheets
from zucai.sfc_verification import compare_issue, notice_for_issue
from zucai.sources.c500 import C500Sfc

SEQUENCES = list(range(1, 15))
GAME_PATHS = {"sfc": "/zucai/", "ren9": "/zucai/ren9/"}
GAME_NAMES = {"sfc": "胜负彩", "ren9": "任九"}


def _record_official_check(candidate, notice, at, reasons):
    # Check the raw labels through project rules. Report evidence cannot approve an alias.
    verification = compare_issue(
        candidate["issue_no"], candidate["sale_end_utc"], candidate["fixtures"], notice, at
    )
    candidate["official_status"] = verification["status"]
    candidate["official_pending_fields"] = verification["differences"]
    if verification["status"] != "verified":
        reasons.append(f"项目公告核对仍待核，{verification['reason']}")


def _primary(issue, primary, notice, at, result):
    try:
        (snapshot,) = C500Sfc().parse(primary)
    except ValueError as error:
        result["pending_reasons"].append(f"500 页面待核：{error}")
        return result
    if snapshot.issue_no != issue:
        result["pending_reasons"].append(f"500 返回别期 {snapshot.issue_no}")
        return result
    if snapshot.sale_end_utc != datetime.fromisoformat(notice["sale_end_utc"]):
        result["pending_reasons"].append("500 停售时间与公告不符")
        return result
    fixtures = [
        {
            **asdict(row),
            "kickoff_utc": row.kickoff_utc.isoformat(),
            "provider_home_id": row.home_id,
            "provider_away_id": row.away_id,
        }
        for row in snapshot.fixtures
    ]
    candidate = {
        "provider": "500.com",
        "issue_no": issue,
        "sale_end_utc": snapshot.sale_end_utc.isoformat(),
        "fixtures": fixtures,
    }
    _record_official_check(candidate, notice, at, result["pending_reasons"])
    if candidate["official_status"] != "conflict":
        result["candidate"] = candidate
    return result


def _verified_fixtures(page, notice, schedule, reasons):
    if [entry["seq"] for entry in schedule] != SEQUENCES or any(
        not entry.get("project_match_id") or not entry.get("evidence_urls") for entry in schedule
    ):
        reasons.append("赛程证据缺失，需逐场提供 1 至 14 场的库内比赛和来源")
        return []
    if len({entry["project_match_id"] for entry in schedule}) != len(SEQUENCES):
        reasons.append("赛程证据的库内比赛编号重复")
        return []
    fixtures = []
    for row, official, proof in zip(page["rows"], notice["fixtures"], schedule, strict=True):
        seq = row["seq"]
        if (
            row["seq"] != official["seq"]
            or (proof["official_home"], proof["official_away"])
            != (official["home"], official["away"])
            or (row["home"], row["away"]) != (proof["okooo_home"], proof["okooo_away"])
        ):
            reasons.append(f"第 {seq} 场主客或队名与公告及按场证据不符")
        elif row["okooo_fixture_id"] != proof["okooo_fixture_id"]:
            reasons.append(f"第 {seq} 场澳客比赛编号与按场证据不符")
        elif row["league"] != proof["league"] or row["kickoff_beijing"] != proof["kickoff_beijing"]:
            reasons.append(f"第 {seq} 场赛事或开球时间与赛程证据不符")
        else:
            fixtures.append(
                {
                    **row,
                    "project_match_id": proof["project_match_id"],
                    "official_home": official["home"],
                    "official_away": official["away"],
                    "evidence_urls": proof["evidence_urls"],
                }
            )
    return fixtures


def replay(issue, at, primary, observations, notices, evidence):
    """Return a candidate and pending reasons without updating any project state."""
    result = {"candidate": None, "pending_reasons": []}
    reasons = result["pending_reasons"]
    notice = notice_for_issue(issue, notices, at)
    if (
        [row.get("seq") for row in notice.get("fixtures", [])] != SEQUENCES
        or not notice.get("sale_end_utc")
        or notice.get("reason")
    ):
        reasons.append("本期官方公告待核")
        return result
    clock = datetime.fromisoformat(at)
    if clock >= datetime.fromisoformat(notice["sale_end_utc"]):
        reasons.append("已到停售时间")
        return result
    if notice.get("sale_start_utc") and clock < datetime.fromisoformat(notice["sale_start_utc"]):
        reasons.append("尚未到公告开售时间")
        return result

    if primary is not None:
        return _primary(issue, primary, notice, at, result)
    reasons.append(f"500 缺少 {issue} 按期页")
    complete = []
    for page in observations:
        game = page["game"]
        path = GAME_PATHS[game]
        label = GAME_NAMES[game]
        if page["expected"] != issue or page["url"] != f"{ORIGIN}{path}{issue}/":
            reasons.append(f"澳客{label}来源不是本期按期页")
        elif page["http_status"] != 200 or page["read_status"] != "complete":
            reasons.extend(f"澳客{label}：{reason}" for reason in page["reasons"])
        elif page["issue_no"] == issue:
            complete.append(page)
        else:
            reasons.append(f"澳客{label}期号不符")
    if not complete:
        reasons.append("澳客同一期无完整候选页")
        return result

    conflicts = compare_sheets(complete, issue)["differences"]
    if conflicts:
        reasons.extend(
            f"澳客页面冲突：第 {row['seq']} 场 {row['field']}"
            if "seq" in row
            else f"澳客页面冲突：{row['field']}"
            for row in conflicts
        )
        return result

    page = min(complete, key=lambda row: row["game"] != "sfc")
    if datetime.fromisoformat(page["sale_end_utc"]) != datetime.fromisoformat(
        notice["sale_end_utc"]
    ):
        reasons.append("澳客停售时间与官方公告不符")
        return result
    fixtures = _verified_fixtures(page, notice, evidence.get(issue, []), reasons)
    if len(fixtures) != len(SEQUENCES):
        return result
    candidate = {
        "provider": "okooo.com",
        "issue_no": issue,
        "game": page["game"],
        "url": page["url"],
        "sale_end_utc": page["sale_end_utc"],
        "fixtures": fixtures,
    }
    _record_official_check(candidate, notice, at, reasons)
    if candidate["official_status"] != "conflict":
        result["candidate"] = candidate
    return result
