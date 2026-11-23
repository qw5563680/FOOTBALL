"""Compare archived 500.com rows with the official 14-match sale schedule."""

import re
from collections import defaultdict
from datetime import date

from zucai.nations import LEAGUE, sfc_team
from zucai.sfc_club_names import club_name
from zucai.sfc_sequence import _instant
from zucai.sources.sporttery import BEIJING


def _time(value):
    try:
        return _instant(value)
    except (TypeError, ValueError):
        return None


def _version_conflict(notices):
    return {
        "reason": "同日官方公告的内容或修订次序无法核实",
        "versions": [
            {
                key: n.get(key)
                for key in (
                    "source_url",
                    "raw_sha256",
                    "title",
                    "notice_date",
                    "published_at",
                    "observed_utc",
                )
            }
            for n in notices
        ],
    }


def _receipt(notice):
    result = notice.copy()
    start = _time(result.get("sale_start_utc"))
    end = _time(result.get("sale_end_utc"))
    if not result.get("source_url") or not re.fullmatch(
        r"[a-f0-9]{64}", result.get("raw_sha256") or ""
    ):
        result["reason"] = "官方公告缺少来源或原件摘要"
    elif start is None or end is None or start >= end:
        result["reason"] = result.get("reason") or "官方售期不完整"
    if result.get("reason"):
        result["sale_start_utc"] = result["sale_end_utc"] = None
    return result


def _choose_version(notices):
    """Use a complete republication only for the very same signed document."""
    if len(notices) == 1:
        return _receipt(notices[0])
    identity = {(n.get("notice_date"), n.get("title"), n.get("kind")) for n in notices}
    same_document = len(identity) == 1 and all(
        (notices[0].get("notice_date"), notices[0].get("title"))
    )
    complete = [
        n
        for n in notices
        if not _receipt(n).get("reason")
        and (len(n.get("fixtures") or []) == 14 or (n.get("from_archive") and "fixtures" not in n))
    ]
    first = (complete or notices)[0]
    # A mirror without an attachment cannot invalidate a complete copy of the same
    # issuer notice. If the mirror does provide fields, they must still agree.
    for other in notices:
        for field in ("sale_start_utc", "sale_end_utc", "fixtures"):
            left, right = first.get(field), other.get(field)
            if (
                same_document
                and complete
                and (not right or (field == "fixtures" and "fixtures" not in first))
            ):
                continue
            if field.endswith("_utc"):
                left, right = _time(left), _time(right)
            if left != right:
                return _version_conflict(notices)
    return _receipt(first)


def notice_for_issue(issue_no, notices, checked_at):
    """Resolve the governing official version actually known at the comparison cutoff."""
    cutoff = _instant(checked_at)
    versions = []
    for notice in notices:
        if notice.get("game", "sfc") != "sfc" or str(notice["issue_no"]) != str(issue_no):
            continue
        published = _time(notice.get("published_at"))
        observed = _time(notice.get("observed_utc"))
        if any(stamp and stamp > cutoff for stamp in (observed, published)):
            continue
        if published is None or observed is None:
            if notice.get("from_archive"):
                continue
            return {
                **notice,
                "sale_start_utc": None,
                "sale_end_utc": None,
                "reason": "公告版本时间缺失",
            }
        # A province may republish the initial schedule *after* an issuer amendment.
        try:
            day = (
                date.fromisoformat(notice["notice_date"])
                if notice.get("notice_date")
                else published.astimezone(BEIJING).date()
            )
        except (TypeError, ValueError):
            if notice.get("from_archive"):
                continue
            return {
                **notice,
                "sale_start_utc": None,
                "sale_end_utc": None,
                "reason": "公告落款日期无效",
            }
        if day <= cutoff.astimezone(BEIJING).date():
            versions.append((day, published, notice))
    if not versions:
        return {}
    latest = max(day for day, _, _ in versions)
    versions = [(stamp, n) for day, stamp, n in versions if day == latest]
    if not any(n.get("notice_date") for _, n in versions):
        # Without an issuer date our version evidence is the original publication time.
        newest = max(stamp for stamp, _ in versions)
        versions = [(stamp, n) for stamp, n in versions if stamp == newest]
    records = [n for _, n in versions]
    amendments = [n for n in records if n.get("kind") == "amendment"]
    return _choose_version(amendments or records)


def pending_amendments(failures, notices, checked_at):
    """A missing article stays pending until that URL has a usable body at the cutoff."""
    cutoff = _instant(checked_at)
    pending = {}
    for failure in failures:
        seen = _time(failure.get("first_observed_utc"))
        failed_at = _time(failure.get("failed_at_utc", failure.get("first_observed_utc")))
        if seen is None or failed_at is None or not seen <= failed_at <= cutoff:
            continue
        url = failure["source_url"]
        if url not in pending or seen < _time(pending[url]["first_observed_utc"]):
            pending[url] = failure

    for url, failure in list(pending.items()):
        failed_at = _time(failure.get("failed_at_utc", failure["first_observed_utc"]))
        rows = [
            n for n in notices if n.get("source_url") == url and n.get("title") == failure["title"]
        ]
        if any(
            n.get("game") == "other"
            and failed_at <= _time(n["observed_utc"]) <= cutoff
            and _time(n["published_at"]) <= cutoff
            and date.fromisoformat(n["notice_date"]) <= cutoff.astimezone(BEIJING).date()
            for n in rows
        ):
            del pending[url]
            continue
        rows = [n for n in rows if n.get("game", "sfc") == "sfc"]
        if failure.get("issue_hint"):
            rows = [n for n in rows if n["issue_no"] == failure["issue_hint"]]
        if rows and all(
            n.get("kind") == "amendment"
            and len(n.get("fixtures") or ()) == 14
            and notice_for_issue(n["issue_no"], [n], cutoff).get("sale_end_utc")
            and _time(n["observed_utc"]) >= failed_at
            for n in rows
        ):
            del pending[url]
    return [pending[url] for url in sorted(pending)]


def resolve_notices(notices, archived, checked_at):
    """Resolve freshly read notices and archived receipts separately for each issue."""
    by_issue = defaultdict(list)
    for notice in notices:
        if notice.get("game", "sfc") == "sfc":
            by_issue[notice["issue_no"]].append(notice)
    for number, receipt in archived:
        if receipt.get("source_url") and re.fullmatch(
            r"[a-f0-9]{64}", receipt.get("raw_sha256") or ""
        ):
            # A manifest receipt omits the fixture table. It can establish a window,
            # but has no fixture fields to compare with a copy of the same notice.
            archived_notice = {**receipt, "issue_no": number, "from_archive": True}
            archived_notice.pop("pending_amendments", None)
            by_issue[number].append(archived_notice)
    return {
        number: selected
        for number, versions in sorted(by_issue.items())
        if (selected := notice_for_issue(number, versions, checked_at))
    }


def _names(row, side):
    """Return label and auditable exact alternatives scoped to this provider team/fixture."""
    label = row.get(side)
    if not label:
        return {}
    names = {label: None}
    league = row.get("league")
    team_id = row.get(f"provider_{side}_id")
    if league == LEAGUE:
        try:
            canonical = sfc_team(team_id, label)
        except ValueError:
            canonical = None
    else:
        canonical = club_name(league, team_id, label)
    if canonical and canonical != label:
        names[canonical] = {
            "basis": "approved_team_id_and_label",
            "league": league,
            "provider_id": team_id,
        }

    bridge = row.get("mapping_evidence") or {}
    identity = row.get("matched_identity") or {}
    if (
        row.get("mapping") == "matched"
        and bridge.get("basis") == "provider_fixture_and_team_ids"
        and row.get("fixture_id")
        and bridge.get("fixture_id") == row["fixture_id"]
        and bridge.get("jc_id")
        and bridge["jc_id"] == row.get("match_id")
        and all(
            row.get(f"provider_{s}_id") and row[f"provider_{s}_id"] == bridge.get(f"{s}_id")
            for s in ("home", "away")
        )
    ):
        proof = {
            "basis": "provider_fixture_and_team_ids",
            "fixture_id": row["fixture_id"],
            "provider_id": team_id,
            "match_id": row["match_id"],
            "provenance": bridge.get("provenance"),
        }
        for name in (bridge.get(side), identity.get(side)):
            if name and name != label:
                names[name] = proof
    return names


def _compare_fixtures(rows, official, result):
    differences = result["differences"]
    expected = set(range(1, 15))
    for which, fixtures in (("official", official), ("provider", rows)):
        numbers = [row.get("seq") for row in fixtures]
        if (
            len(numbers) != 14
            or any(type(n) is not int for n in numbers)
            or set(numbers) != expected
        ):
            differences.append(
                {
                    "field": "seq",
                    "status": "pending",
                    "source": which,
                    "positions": numbers,
                    "reason": "缺少完整且唯一的 1 至 14 场序号",
                }
            )
            return
    labels = {(row["seq"], side): _names(row, side) for row in rows for side in ("home", "away")}
    # A name can attest a side/order conflict only when its other position is unambiguous.
    official_positions = defaultdict(set)
    for row in official:
        for side in ("home", "away"):
            if row.get(side):
                official_positions[row[side]].add((row["seq"], side))
    provider = {row["seq"]: row for row in rows}
    for row in official:
        seq = row["seq"]
        for side in ("home", "away"):
            name = row.get(side)
            names = labels[seq, side]
            pair = {
                "seq": seq,
                "field": side,
                "official": name,
                "provider": provider[seq].get(side),
            }
            elsewhere = [
                official_positions[n]
                for n in names
                if official_positions[n] and (seq, side) not in official_positions[n]
            ]
            positions = set().union(*elsewhere)
            if len(positions) == 1:
                position, role = positions.pop()
                differences.append(
                    {
                        **pair,
                        "status": "conflict",
                        "reason": "该队在公告中的场序或主客位置不同",
                        "official_seq": position,
                        "official_side": role,
                    }
                )
            elif not positions and name and name in names:
                if names[name]:
                    result["aliases"].append({**pair, **names[name]})
            else:
                differences.append({**pair, "status": "pending", "reason": "球队名称对应关系待核"})


def verify_issue(
    issue_no, provider_sale_end_utc, provider_fixtures, notices, checked_at, *, source_error=None
):
    """Return JSON-safe evidence, without changing provider records or historical notices."""
    official = notice_for_issue(issue_no, notices, checked_at) if not source_error else {}
    return compare_issue(
        issue_no,
        provider_sale_end_utc,
        provider_fixtures,
        official,
        checked_at,
        source_error=source_error,
    )


def compare_issue(
    issue_no, provider_sale_end_utc, provider_fixtures, selected, checked_at, *, source_error=None
):
    """Check the provider against the same version used to select the current issue."""
    official = selected.copy()
    pending = official.pop("pending_amendments", [])
    retained = official.pop("from_archive", False) or bool(pending)
    result = {
        "issue_no": str(issue_no),
        "checked_at": _instant(checked_at).isoformat(),
        "provider": "500.com",
        "status": "pending",
        "official": {} if retained else official,
        "compared_fields": [],
        "differences": [],
        "aliases": [],
    }
    if retained and official:
        result["last_known_official"] = official
        result["reason"] = str(source_error or "本次未重新取得当前适用的官方公告，沿用已归档版本")
    if pending:
        result["pending_amendments"] = pending
        result["reason"] = "已发现的调整公告仍待核实"
    if not official or official.get("reason"):
        result["reason"] = str(
            result.get("reason")
            or source_error
            or official.get("reason")
            or "未取得本玩法本期官方公告"
        )
        return result
    result["compared_fields"] = ["issue_no", "sale_end_utc"]
    end = _time(provider_sale_end_utc)
    if end is None or end != _time(official["sale_end_utc"]):
        result["differences"].append(
            {
                "field": "sale_end_utc",
                "status": "pending" if end is None else "conflict",
                "provider": end.isoformat() if end else None,
                "official": official["sale_end_utc"],
            }
        )
    if not retained or "fixtures" in official:
        result["compared_fields"].extend(("seq", "home", "away"))
        _compare_fixtures(provider_fixtures, official.get("fixtures") or [], result)
    # A comparison against archived fields can disclose known differences without
    # claiming that the governing notice was obtainable in this attempt.
    if retained:
        return result
    statuses = {entry["status"] for entry in result["differences"]}
    if "conflict" in statuses:
        result["status"] = "conflict"
        result["reason"] = "第三方数据与官方公告存在字段冲突"
    elif statuses:
        result["reason"] = "部分字段缺少可核对的证据"
    else:
        result["status"] = "verified"
        result["reason"] = "期号、停售时间和 14 场场序及主客队均已核对"
    return result
