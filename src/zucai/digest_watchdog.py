"""Read the Actions record for the current daily watchdog slot."""

from datetime import UTC, datetime, time

import requests

from zucai.digest_display import BEIJING, _day, _instant
from zucai.github_api import GitHubApi, GitHubRateLimited
from zucai.watchdog import SLOTS, _cutoff

ALARM_TITLE = "Football daily watchdog [collection-machine-alarm]"
TRIGGERS = {"schedule": "GitHub 定时", "alarm": "采集机闹钟", "manual": "手动"}


def _latest(rows, start, at):
    candidates = []
    for run in rows:
        if run["head_branch"] != "main":
            continue
        created = _instant(run["created_at"])
        if start <= created <= at:
            if type(run["id"]) is not int or run["id"] <= 0:
                raise ValueError("无效的保险检查运行编号")
            candidates.append((created, run["id"], run))
    if not candidates:
        return {"status": "missing"}
    created, run_id, run = max(candidates, key=lambda candidate: candidate[:2])
    if run["event"] == "schedule":
        source = "schedule"
    elif run["event"] == "workflow_dispatch" and isinstance(run["display_title"], str):
        source = "alarm" if run["display_title"] == ALARM_TITLE else "manual"
    else:
        raise ValueError("无效的保险检查触发记录")
    return {
        "status": "found",
        "run_id": run_id,
        "source": source,
        "created_utc": created.isoformat(),
    }


def read(at, repository, token):
    if not repository or not token:
        return {"status": "unavailable"}
    at = _instant(at)
    # Daily slots share the watchdog's start times and belong to the digest's Beijing date.
    utc_date = datetime.combine(_day(at), time.min, UTC)
    starts = (_cutoff(utc_date, hour) for hour in SLOTS)
    start = max((slot_start for slot_start in starts if slot_start <= at), default=None)
    if start is None:
        return {"status": "missing"}
    url = f"https://api.github.com/repos/{repository}/actions/workflows/football-watchdog.yml/runs"
    params = {
        "branch": "main",
        "created": f"{start:%Y-%m-%dT%H:%M:%SZ}..{at:%Y-%m-%dT%H:%M:%SZ}",
        "per_page": 100,
    }
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        with GitHubApi(wait_for_quota=False).request(
            lambda: requests.get(
                url, params=params, headers=headers, timeout=10, allow_redirects=False
            )
        ) as response:
            if response.status_code != 200 or "next" in response.links:
                return {"status": "unavailable"}
            payload = response.json()
        count, rows = payload["total_count"], payload["workflow_runs"]
        if (
            not isinstance(rows, list)
            or type(count) is not int
            or count != len(rows)
            or count > 100
        ):
            return {"status": "unavailable"}
        return _latest(rows, start, at)
    except (OSError, KeyError, TypeError, ValueError, OverflowError, GitHubRateLimited):
        return {"status": "unavailable"}


def header_line(evidence):
    if evidence["status"] == "missing":
        return "本档自动保险检查没有运行。"
    if evidence["status"] == "unavailable":
        return "自动保险检查：未能读取 Actions 运行记录。"
    created = _instant(evidence["created_utc"]).astimezone(BEIJING)
    trigger = TRIGGERS[evidence["source"]]
    return f"自动保险检查：北京时间 {created:%Y-%m-%d %H:%M:%S}，{trigger}触发。"
