"""Budgeted GitHub API requests and job-local quota observations."""

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

LOG = logging.getLogger(__name__)
BEIJING = timezone(timedelta(hours=8))
RESOURCES = {"core", "graphql", "search", "integration_manifest", "issues"}
NUMERIC_HEADERS = (
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
    "x-ratelimit-used",
    "retry-after",
)
MAX_EPOCH = 253402271999  # Last representable local day after conversion to UTC+8.


def _integer(value):
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,14}", value):
        return int(value)
    return None


def _epoch(value):
    if type(value) in (int, float) and 0 < value <= MAX_EPOCH:
        return value
    return None


def _local_time(value):
    return (
        datetime.fromtimestamp(value, BEIJING).strftime("%Y-%m-%d %H:%M:%S（北京时间）")
        if _epoch(value) is not None
        else "未知"
    )


def _retry_time(header):
    if (seconds := _integer(header)) is not None:
        return _epoch(time.time() + seconds)
    if header and len(header) <= 80:
        try:
            value = parsedate_to_datetime(header)
            return _epoch(value.replace(tzinfo=value.tzinfo or UTC).timestamp())
        except (ValueError, TypeError, OverflowError, OSError):
            pass
    return None


@dataclass(frozen=True)
class GitHubDiagnostic:
    status: int | None
    category: str
    request_id: str | None
    rate_limit: dict
    restored_at: float | None


def diagnose(response, *, graphql=False):
    """Classify GitHub's response without retaining URLs, tokens or response text."""
    headers = {key.lower(): value for key, value in response.headers.items()}
    status = response.status_code
    quota = {key: headers[key] for key in NUMERIC_HEADERS if _integer(headers.get(key)) is not None}
    if headers.get("x-ratelimit-resource") in RESOURCES:
        quota["x-ratelimit-resource"] = headers["x-ratelimit-resource"]
    rid = headers.get("x-github-request-id", "")
    request_id = rid if re.fullmatch(r"[A-Fa-f0-9:]{1,100}", rid) else None
    retry = _retry_time(headers.get("retry-after"))
    remaining = _integer(quota.get("x-ratelimit-remaining"))
    reset = _epoch(_integer(quota.get("x-ratelimit-reset")))
    message = response.text[:2400].lower() if status in {403, 429} else ""
    secondary = "secondary rate limit" in message or "abuse detection" in message
    permission_message = "not accessible by integration" in message
    denied = status == 429 or (
        status == 403
        and (
            secondary
            or "rate limit exceeded" in message
            or (not permission_message and (remaining == 0 or retry is not None))
        )
    )
    forbidden = status in {401, 403}
    if graphql and 200 <= status < 300:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            data = payload.get("data")
            succeeded = isinstance(data, dict) and any(v is not None for v in data.values())
            errors = payload.get("errors")
            if isinstance(errors, list):
                types = {str(e.get("type")) for e in errors if isinstance(e, dict)}
                forbidden = "FORBIDDEN" in types
                # Never replay a GraphQL operation which returned successful data.
                denied = "RATE_LIMITED" in types and not succeeded
    restored = None
    if denied:
        restored = (
            max(retry or 0, reset or 0)
            if remaining == 0
            else retry or (reset if not secondary else None)
        )
    category = "rate_limited" if denied else "permission_denied" if forbidden else "other"
    return GitHubDiagnostic(status, category, request_id, quota, _epoch(restored))


class GitHubRateLimited(RuntimeError):
    """A rejected or deliberately deferred GitHub operation, safe to log."""

    category = "rate_limited"

    def __init__(self, diagnostic, *, reserved=None):
        self.status = diagnostic.status
        self.request_id = diagnostic.request_id
        self.rate_limit = diagnostic.rate_limit
        self.restored_at = diagnostic.restored_at
        reason = (
            "GitHub 接口次数用光"
            if reserved is None
            else f"GitHub 接口剩余 {reserved} 次，预留给日报发布"
        )
        super().__init__(f"{reason}，恢复时间 {_local_time(self.restored_at)}，当前操作未完成。")


def _read_usage(path):
    if path is None or not path.is_file():
        return 0, {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        LOG.warning("GitHub 接口用量读取失败")
        return 0, {}
    count, latest = 0, {}
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("resource"), str)
            or row["resource"] not in RESOURCES
        ):
            continue
        count += 1
        if type(row.get("remaining")) is int and row["remaining"] >= 0:
            latest[row["resource"]] = row["remaining"], _epoch(row.get("reset"))
    return count, latest


def _usage_path():
    path = os.environ.get("ZUCAI_GITHUB_USAGE_FILE")
    return Path(path) if path else None


class GitHubApi:
    """Retry only a known rejected operation, using the time its step can spare."""

    def __init__(self):
        self.deadline = _epoch(_integer(os.environ.get("ZUCAI_GITHUB_DEADLINE")))
        self.reserve = _integer(os.environ.get("ZUCAI_GITHUB_RESERVE")) or 0
        self.usage = _usage_path()
        _, self.quota = _read_usage(self.usage)
        self.denials = {}

    def _note(self, resource, diagnostic=None):
        quota = diagnostic.rate_limit if diagnostic else {}
        resource = quota.get("x-ratelimit-resource", resource)
        remaining = _integer(quota.get("x-ratelimit-remaining"))
        reset = _epoch(_integer(quota.get("x-ratelimit-reset")))
        if remaining is not None:
            self.quota[resource] = remaining, reset
        if self.usage is not None:
            # One short append per attempt, including failures, shared by job subprocesses.
            row = dict(resource=resource, remaining=remaining, reset=reset)
            try:
                self.usage.parent.mkdir(parents=True, exist_ok=True)
                with self.usage.open("ab", buffering=0) as output:
                    output.write((json.dumps(row) + "\n").encode())
            except OSError:
                LOG.warning("GitHub 接口用量写入失败")

    def _wait(self, error):
        now = time.time()
        if error.restored_at is None or self.deadline is None:
            raise error
        ready = error.restored_at + 1
        if not now < ready < self.deadline:
            raise error
        LOG.info("GitHub 限流，等待至 %s 后继续。", _local_time(ready))
        time.sleep(ready - now)
        if time.time() >= self.deadline:
            raise error

    def _protect_quota(self, resource, reserve):
        if error := self.denials.get(resource):
            if error.restored_at is None or time.time() < error.restored_at + 1:
                self._wait(error)
            self.denials.pop(resource)
            return
        remaining, reset = self.quota.get(resource, (None, None))
        if remaining is None or (reset is not None and time.time() >= reset + 1):
            return
        floor = self.reserve if reserve and resource == "core" else 0
        if 0 < remaining <= floor:
            diagnostic = GitHubDiagnostic(None, "rate_limited", None, {}, reset)
            raise GitHubRateLimited(diagnostic, reserved=remaining)
        if remaining == 0:
            diagnostic = GitHubDiagnostic(None, "rate_limited", None, {}, reset)
            self._wait(GitHubRateLimited(diagnostic))

    def request(self, send, *, graphql=False, before_retry=None, reserve=False):
        resource = "graphql" if graphql else "core"
        self._protect_quota(resource, reserve)
        for attempt in range(2):
            try:
                response = send()
            except Exception:
                self._note(resource)
                raise
            diagnostic = diagnose(response, graphql=graphql)
            self._note(resource, diagnostic)
            if diagnostic.category != "rate_limited":
                return response
            response.close()
            error = GitHubRateLimited(diagnostic)
            self.denials[resource] = error
            if attempt:
                raise error
            self._wait(error)
            self.denials.pop(resource)
            if before_retry is not None:
                before_retry()
        raise AssertionError("unreachable")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("summary",))
    parser.parse_args(argv)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        write_summary(summary)
    else:
        sys.stdout.write(summary_line())


def summary_line():
    count, quota = _read_usage(_usage_path())
    scopes = [
        f"{resource} 最近观测剩余 {remaining} 次，恢复时间 {_local_time(reset)}"
        for resource, (remaining, reset) in sorted(quota.items())
    ]
    return (
        f"GitHub 接口调用 {count} 次，"
        + ("。".join(scopes) if scopes else "余量未知，恢复时间未知")
        + "。\n"
    )


def write_summary(path):
    with Path(path).open("a", encoding="utf-8") as output:
        output.write(summary_line())


if __name__ == "__main__":
    main()
