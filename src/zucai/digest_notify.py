"""Publish the exact daily Markdown to a monthly Issue, only from Actions."""

import argparse
import hashlib
import json
import logging
import os
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import requests

from zucai.daily_digest import DISCLAIMER, _day
from zucai.digest_history import notification_path, read_notification, rendered_versions
from zucai.scoreboard.score import instant
from zucai.sources.base import _atomic_write

LOG = logging.getLogger(__name__)
API = "https://api.github.com"
OVERVIEW_START = "<!-- zucai-daily-overview:start -->"
OVERVIEW_END = "<!-- zucai-daily-overview:end -->"


def validate_repository(repository):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("无效 GitHub owner/repository")


class _GitHub:
    def __init__(self, session, repository):
        self.session = session
        self.root = f"/repos/{repository}"
        self.owner, self.name = repository.split("/")

    def request(self, method, path, **kwargs):
        try:
            response = self.session.request(
                method, API + path, timeout=30, allow_redirects=False, **kwargs
            )
        except requests.RequestException:
            raise ValueError("GitHub 请求失败；未输出响应或凭证") from None
        if not 200 <= response.status_code < 300:
            raise ValueError(f"GitHub HTTP {response.status_code}；未输出响应或凭证")
        try:
            return response.json()
        except ValueError:
            raise ValueError("GitHub 响应不是 JSON") from None

    def pages(self, path, **params):
        result = []
        page = 1
        while True:
            rows = self.request("GET", path, params={**params, "per_page": 100, "page": page})
            if not isinstance(rows, list):
                raise ValueError("GitHub 列表响应格式无效")
            result.extend(rows)
            if len(rows) < 100:
                return result
            page += 1

    def graph(self, query, variables):
        result = self.request("POST", "/graphql", json={"query": query, "variables": variables})
        if result.get("errors") or not result.get("data"):
            raise ValueError("GitHub 置顶操作失败，请检查 Actions 的 issues: write 权限与运行记录")
        return result["data"]

    def pin_current(self, issue, previous):
        data = self.graph(
            "query($owner:String!,$name:String!){repository(owner:$owner,name:$name)"
            "{pinnedIssues(first:100){nodes{issue{id}}}}}",
            {"owner": self.owner, "name": self.name},
        )
        pinned = {node["issue"]["id"] for node in data["repository"]["pinnedIssues"]["nodes"]}
        if previous and previous["node_id"] in pinned:
            self.graph(
                "mutation($id:ID!){unpinIssue(input:{issueId:$id}){clientMutationId}}",
                {"id": previous["node_id"]},
            )
        if issue["node_id"] not in pinned:
            self.graph(
                "mutation($id:ID!){pinIssue(input:{issueId:$id}){clientMutationId}}",
                {"id": issue["node_id"]},
            )


def _notification_version(root, body, today, repository):
    root = Path(root).absolute()
    if any(path.is_symlink() for path in (root, *root.parents)):
        raise ValueError("日报通知回执路径不允许符号链接")
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    matches = [v for v in rendered_versions(root, today) if v["markdown_sha256"] == sha]
    if len(matches) != 1:
        raise ValueError("发布正文必须对应唯一已冻结日报版本")
    version = matches[0]
    if version["inputs"].get("repository") not in (None, repository):
        raise ValueError("日报冻结版本与通知仓库不符")
    read_notification(root, version)  # Reject corrupt existing evidence before any request.
    return root, version


def _record_notification(root, version, repository, issue, comment, body):
    if comment.get("body") != body:
        raise ValueError("GitHub 评论响应正文与冻结日报不符，无法确认通知")
    if any(type(value) is not int or value <= 0 for value in (issue["number"], comment["id"])):
        raise ValueError("GitHub 评论或 Issue 身份无效")
    try:
        notified = instant(comment.get("updated_at") or comment["created_at"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("GitHub 评论响应缺少有效发布时间，无法确认通知") from None
    receipt = {
        "schema_version": 1,
        "status": "published",
        "repository": repository,
        "issue_number": issue["number"],
        "comment_id": comment["id"],
        **{key: version[key] for key in ("date", "run_id", "version_id", "markdown_sha256")},
        "notified_utc": notified.isoformat(),
    }
    existing = read_notification(root, version)
    if existing:
        if any(
            existing[key] != receipt[key] for key in ("repository", "issue_number", "comment_id")
        ):
            raise ValueError("已有日报通知回执的目标冲突")
        return
    _atomic_write(
        notification_path(root, version),
        (json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
    )


def monthly_issue_body(issue, comments, repository, today):
    """Keep dated report links newest first and show the latest report at the top."""
    reports = []
    for comment in comments:
        body = comment.get("body") or ""
        markers = re.findall(r"<!-- zucai-daily:(\d{4}-\d{2}-\d{2}) -->", body)
        if len(markers) != 1 or comment.get("user", {}).get("login") != "github-actions[bot]":
            continue
        try:
            day = date.fromisoformat(markers[0])
        except ValueError:
            continue
        if (day.year, day.month) == (today.year, today.month):
            reports.append((day, comment["id"], body))
    if not reports:
        raise ValueError("月报缺少可核实的当月日报，无法更新目录")
    reports.sort(reverse=True)
    issue_url = f"https://github.com/{repository}/issues/{issue['number']}"
    links = "\n".join(
        f"- [{day}]({issue_url}#issuecomment-{comment_id})" for day, comment_id, _ in reports
    )
    latest_day, latest_id, latest_body = reports[0]
    overview = f"**日报目录（最新在前）**\n\n{links}\n\n**最新日报 {latest_day}**\n\n"
    full_link = f"[查看完整日报]({issue_url}#issuecomment-{latest_id})"
    original = issue.get("body") or ""
    if original.count(OVERVIEW_START) != original.count(OVERVIEW_END):
        raise ValueError("月报自动目录标记不完整，保留现有正文")
    if OVERVIEW_START in original:
        start, end = original.index(OVERVIEW_START), original.index(OVERVIEW_END)
        if original.count(OVERVIEW_START) != 1 or end < start:
            raise ValueError("月报自动目录标记重复或顺序错误，保留现有正文")
        prefix, suffix = original[:start], original[end + len(OVERVIEW_END) :]
    else:
        prefix, suffix = original.rstrip() + "\n\n", ""
    leading = prefix + OVERVIEW_START + "\n\n" + overview
    trailing = "\n\n" + OVERVIEW_END + suffix
    result = leading + latest_body + trailing
    if len(result) > 65536:
        result = leading + full_link + trailing
    if len(result) > 65536:
        raise ValueError("月报正文超过 GitHub 长度限制，保留现有正文和完整日报评论")
    return result


def publish(body, repository, *, now=None, history_root=None):
    """Workflow concurrency serializes the find/create/update operation across both shifts."""
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("GITHUB_EVENT_NAME") not in {
        "schedule",
        "workflow_dispatch",
    }:
        raise ValueError("真实 Issue 推送只允许 Actions schedule / workflow_dispatch")
    validate_repository(repository)
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ValueError("缺少 GITHUB_TOKEN")
    today = _day(now or datetime.now(UTC))
    marker = f"<!-- zucai-daily:{today} -->"
    if (
        not body.startswith(DISCLAIMER + "\n")
        or re.findall(r"<!-- zucai-daily:[^\n]* -->", body) != [marker]
        or "建议购买" in body
        or "推荐" in body
    ):
        raise ValueError("只能发布含固定免责行与唯一当日标记的摘要")
    if len(body) > 65536:
        raise ValueError("摘要超过单条 GitHub 评论长度限制；保留数据分支全文，未截断发布")
    version = _notification_version(history_root, body, today, repository) if history_root else None
    title = f"每日报告 {today:%Y-%m}"
    previous_title = f"每日报告 {today.replace(day=1) - timedelta(days=1):%Y-%m}"
    with requests.Session() as session:
        session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )
        api = _GitHub(session, repository)
        issues = api.pages(api.root + "/issues", state="all", sort="created", direction="desc")

        def find(wanted):
            matching = [i for i in issues if i["title"] == wanted and "pull_request" not in i]
            if len(matching) > 1:
                raise ValueError(f"存在多条同名 Issue：{wanted}；停止以避免重复推送")
            return matching[0] if matching else None

        issue, previous = find(title), find(previous_title)
        if issue is None:
            issue = api.request(
                "POST",
                api.root + "/issues",
                json={
                    "title": title,
                    "body": DISCLAIMER
                    + "\n\n本月每日摘要；同日第二班更新原评论。请订阅此 Issue 查看通知。",
                },
            )
        elif issue["state"] == "closed":
            api.request("PATCH", f"{api.root}/issues/{issue['number']}", json={"state": "open"})
        api.pin_current(issue, previous)
        comment_path = f"{api.root}/issues/{issue['number']}/comments"
        comments = api.pages(comment_path)
        matches = [
            comment
            for comment in comments
            if marker in comment.get("body", "")
            and comment.get("user", {}).get("login") == "github-actions[bot]"
        ]
        if len(matches) > 1:
            raise ValueError("同日已有多条机器人评论；停止以避免选择错误")
        if matches:
            comment = api.request(
                "PATCH", f"{api.root}/issues/comments/{matches[0]['id']}", json={"body": body}
            )
        else:
            comment = api.request("POST", comment_path, json={"body": body})
        # Persist success immediately: a later rollover cleanup failure must not
        # erase proof that this exact version became the visible Issue comment.
        if version:
            _record_notification(*version, repository, issue, comment, body)
        current_comments = [item for item in comments if item["id"] != comment["id"]]
        current_comments.append({**comment, "body": body})
        overview = monthly_issue_body(issue, current_comments, repository, today)
        if overview != (issue.get("body") or ""):
            api.request("PATCH", f"{api.root}/issues/{issue['number']}", json={"body": overview})
        # Close only after the new month's report is successfully visible; safe to retry.
        if previous and previous["state"] != "closed":
            api.request(
                "PATCH", f"{api.root}/issues/{previous['number']}", json={"state": "closed"}
            )
    LOG.info("摘要已发布到 Issue #%s，评论 %s", issue["number"], comment["id"])
    return issue["number"], comment["id"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--history-root", type=Path, help="保存成功评论回执的 daily 根目录")
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"), required=False)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        publish(
            args.file.read_text(encoding="utf-8"),
            args.repository or "",
            history_root=args.history_root,
        )
    except (ValueError, OSError, KeyError, TypeError) as exc:
        LOG.error("摘要未推送：%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
