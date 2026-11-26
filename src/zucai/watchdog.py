"""Check the daily workflow's Actions evidence and dispatch at most one repair."""

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

LOG = logging.getLogger(__name__)
SLOTS = (2, 7, 9)
ACTIVE = ("queued", "in_progress", "pending", "requested", "waiting")
DAILY_MARKER = "Identify manual daily chain"


@dataclass(frozen=True)
class Decision:
    slot: int | None
    reason: str
    inputs: dict[str, str | bool] | None = None

    @property
    def label(self):
        return f"UTC {self.slot:02d}:40 档" if self.slot is not None else "日报保险检查"


def _slot(now, cron):
    if cron:
        scheduled = {f"{minute} {hour + 1} * * *": hour for hour in SLOTS for minute in (5, 35)}
        return scheduled.get(cron)
    return next((hour for hour in reversed(SLOTS) if (now.hour, now.minute) >= (hour + 1, 5)), None)


def _cutoff(now, slot):
    return now.replace(hour=slot, minute=30, second=0, microsecond=0)


def _due(now, slot):
    if slot is None or now < now.replace(hour=slot + 1, minute=5, second=0, microsecond=0):
        return False
    next_slot = next((hour for hour in SLOTS if hour > slot), None)
    return next_slot is None or now < _cutoff(now, next_slot)


def _in_slot(run, now, slot):
    if run["head_branch"] != "main" or run["event"] not in ("schedule", "workflow_dispatch"):
        return False
    created = datetime.fromisoformat(run["created_at"])
    return _cutoff(now, slot) <= created <= now


def _job_result(run, name):
    return next((job["conclusion"] for job in run["jobs"] if job["name"] == name), None)


def _manual_daily(run):
    return any(
        step["name"] == DAILY_MARKER and step["conclusion"] in {"success", "failure"}
        for job in run["jobs"]
        for step in job["steps"]
    )


def decide(now, runs, *, cron=None):
    """Select a repair from GitHub run objects augmented with the latest attempt's jobs."""
    now = now.astimezone(UTC)
    slot = _slot(now, cron)
    if not _due(now, slot):
        return Decision(slot, "当前不在该档检查时段，未补跑")
    if runs is None:
        return Decision(slot, "读不到 GitHub 运行记录，等下一次检查")
    if any(run["status"] != "completed" for run in runs):
        return Decision(slot, "采集工作流有任务排队、等待或正在运行，未补跑")

    candidates = [run for run in runs if _in_slot(run, now, slot)]
    manual = [run for run in candidates if run["event"] == "workflow_dispatch"]
    if any(_manual_daily(run) for run in manual):
        return Decision(slot, "本档已有手动整条链，未补跑")

    scheduled = [run for run in candidates if run["event"] == "schedule"]
    if slot != 7:
        gates = [_job_result(run, "schedule-gate") for run in scheduled]
        if any(gate is None for gate in gates):
            return Decision(slot, "读不到定时运行的期次检查结论，等下一次检查")
        # A gate that ran identifies a delayed 07:40 schedule.
        scheduled = [run for run, gate in zip(scheduled, gates, strict=True) if gate == "skipped"]
    if not scheduled:
        inputs = {"operation": "daily"}
        if slot == 7:
            inputs["gate"] = True
        return Decision(slot, "本档缺少定时运行和手动整条链", inputs)

    if slot == 9:
        results = [_job_result(run, "predict") for run in scheduled]
        if all(result == "skipped" for result in results):
            if any(_job_result(run, "predict") not in (None, "skipped") for run in manual):
                return Decision(slot, "本档已有手动预测补跑，未再触发")
            return Decision(slot, "本档定时运行跳过了预测", {"operation": "predict"})
        if None in results and all(result in (None, "skipped") for result in results):
            return Decision(slot, "读不到定时运行的预测结论，等下一次检查")
    return Decision(slot, "本档已有定时运行，未补跑")


class GitHub:
    def __init__(self, repository, token):
        self.root = f"/repos/{repository}/actions"
        self.workflow = f"{self.root}/workflows/football-feeds.yml"
        self.token = token

    def request(self, path, *, query=None, body=None):
        url = f"https://api.github.com{path}"
        if query:
            url += "?" + urlencode(query)
        request = Request(
            url,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urlopen(request, timeout=10) as response:
            payload = response.read()
        return json.loads(payload) if payload else None

    def listing(self, path, key, **query):
        rows = []
        for page in range(1, 11):
            response = self.request(path, query={**query, "per_page": 100, "page": page})
            batch = response[key]
            if not isinstance(batch, list):
                raise ValueError("GitHub 列表格式无效")
            rows.extend(batch)
            if len(rows) >= response["total_count"]:
                return rows
            if not batch:
                break
        raise ValueError("GitHub 列表不完整")

    def slot_runs(self, now, slot):
        path = f"{self.workflow}/runs"
        # The production workflow's write lock spans refs and run dates.
        for status in ACTIVE:
            active = self.request(path, query={"status": status, "per_page": 1})["workflow_runs"]
            if not isinstance(active, list):
                raise ValueError("GitHub 活动运行列表格式无效")
            if active:
                return active
        recent = self.listing(
            path, "workflow_runs", branch="main", created=f">={_cutoff(now, slot).isoformat()}"
        )
        for run in recent:
            if run["status"] == "completed" and _in_slot(run, now, slot):
                attempt = f"{int(run['id'])}/attempts/{int(run['run_attempt'])}"
                run["jobs"] = self.listing(f"{self.root}/runs/{attempt}/jobs", "jobs")
        return recent

    def dispatch(self, inputs):
        self.request(f"{self.workflow}/dispatches", body={"ref": "main", "inputs": inputs})


def run(now):
    now = now.astimezone(UTC)
    cron = os.environ.get("WATCHDOG_CRON")
    slot = _slot(now, cron)
    decision = Decision(slot, "当前不在该档检查时段，未补跑")
    detail = decision.reason
    if _due(now, slot):
        operation = None
        try:
            github = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"])
            decision = decide(now, github.slot_runs(now, slot), cron=cron)
            detail = decision.reason
            if decision.inputs:
                operation = decision.inputs["operation"]
                github.dispatch(decision.inputs)
                gate = "（先检查期次）" if decision.inputs.get("gate") else ""
                detail = f"{decision.reason}，已请求补跑 {operation}{gate}"
        except (OSError, KeyError, TypeError, ValueError) as exc:
            status = f"HTTP {exc.code}" if isinstance(exc, HTTPError) else "响应不可用"
            if operation:
                detail = f"GitHub 接口{status}，补跑 {operation} 未获确认，等下一次检查"
            else:
                detail = f"GitHub 接口{status}，无法核对运行记录，等下一次检查"
    message = f"{decision.label}：{detail}。"
    LOG.info("%s", message)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a", encoding="utf-8") as stream:
            stream.write(message + "\n")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    run(datetime.now(UTC))
