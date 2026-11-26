"""Read-only presentation of archived JC name candidates and resolutions."""

import json
import re

from zucai.digest_display import _text
from zucai.forecast_archive import _path
from zucai.scoreboard.score import instant

_APPROVAL = re.compile(
    r"https://github\.com/qw5563680/FOOTBALL/pull/([1-9][0-9]*)"
    r"(?:#issuecomment-[1-9][0-9]*)?"
)
_REVIEW_STEPS = {
    "duplicate": "处置结论为重复，等所有者批准对照并确认合并",
    "unresolved": "处置结论未决，待审查者补证复核",
}


def _resolution_reason(resolution):
    if resolution.get("basis") == "approved_api_binding":
        reason = f"已有获批接口绑定 {_text(resolution.get('binding'))}"
    else:
        reason = f"获批队名对照已合并至 {_text(resolution.get('canonical'))}"
    links = resolution.get("approval_pr")
    if isinstance(links, str):
        links = [links]
    for link in links if isinstance(links, list) else ():
        if isinstance(link, str) and (match := _APPROVAL.fullmatch(link)):
            reason += f" [批准 #{match[1]}]({link})"
    if resolution.get("approval_source"):
        reason += f"（依据 {_text(resolution['approval_source'])}）"
    return reason


def pending_name_lines(archive, feed, now, *, run_id=None):
    """List every pending entry and the current collection's auto-resolutions."""
    path = "state/alias_candidates/jc.json"
    if path not in feed["files"]:
        return []
    candidates = json.loads(_path(archive, path).read_bytes())
    at = instant(now)
    feed_run = feed.get("run")
    pending, resolved = [], []
    for league, names in sorted(candidates.items()):
        for name, entry in sorted(names.items()):
            observations = [
                row
                for row in entry.get("evidence", [])
                if row.get("captured_utc") and instant(row["captured_utc"]) <= at
            ]
            if not observations:
                continue
            review = entry.get("review") or {}
            resolution = entry.get("resolution") or {}
            applied = resolution.get("recorded_utc") or review.get("recorded_utc")
            label = f"{_text(name)}（{_text(league)}）"
            if entry.get("status") == "resolved" and applied and instant(applied) <= at:
                if feed_run and resolution.get("run_id") == feed_run and run_id in (None, feed_run):
                    resolved.append(f"- {label}：{_resolution_reason(resolution)}。")
                continue
            if entry.get("status") != "pending" and not (applied and instant(applied) > at):
                continue
            first = min(
                observations,
                key=lambda row: (instant(row["captured_utc"]), row.get("match_id", "")),
            )
            reviewed = review.get("recorded_utc")
            conclusion = review.get("conclusion") if reviewed and instant(reviewed) <= at else None
            step = _REVIEW_STEPS.get(conclusion, "从未处置，待审查者核对")
            pending.append(f"- {label}，首次比赛 {_text(first.get('match_id'))}：{step}。")

    lines = []
    if pending:
        lines += [f"竞彩待核新队名 {len(pending)} 个。", "", *pending, ""]
    if resolved:
        lines += [f"本班自动出队 {len(resolved)} 个。", "", *resolved, ""]
    return lines
