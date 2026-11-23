"""Publish the exact daily Markdown to a monthly Issue, only from Actions."""

import argparse
import hashlib
import json
import logging
import os
import re
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import requests

from zucai.daily_digest import DISCLAIMER, _day
from zucai.digest_history import notification_path, read_notification, rendered_versions
from zucai.forecast_archive import _path
from zucai.publication_history import PublicationHistoryError, git_read
from zucai.scoreboard.score import instant
from zucai.sources.base import _atomic_write

LOG = logging.getLogger(__name__)
API = "https://api.github.com"
OVERVIEW_START = "<!-- zucai-daily-overview:start -->"
OVERVIEW_END = "<!-- zucai-daily-overview:end -->"


def validate_repository(repository):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("无效 GitHub owner/repository")


class GitHubFailure(ValueError):
    def __init__(self, response, message=None):
        super().__init__(message or f"GitHub HTTP {response.status_code}；未输出响应或凭证")
        self.status = response.status_code
        rid = response.headers.get("X-GitHub-Request-Id", "")
        self.request_id = rid if re.fullmatch(r"[A-Fa-f0-9:]{1,100}", rid) else None
        self.rate_limit = {}
        for key in (
            "x-ratelimit-limit",
            "x-ratelimit-remaining",
            "x-ratelimit-reset",
            "x-ratelimit-used",
            "retry-after",
        ):
            value = response.headers.get(key, "")
            if re.fullmatch(r"[0-9]{1,20}", value):
                self.rate_limit[key] = value
        resource = response.headers.get("x-ratelimit-resource")
        if resource in {"core", "graphql", "search", "integration_manifest", "issues"}:
            self.rate_limit["x-ratelimit-resource"] = resource


@contextmanager
def _record_failure(version, api):
    try:
        yield
    except (ValueError, KeyError, TypeError, OSError) as exc:
        if version:
            root, item = version
            evidence = {
                k: item[k] for k in ("date", "version_id", "generated_utc", "markdown_sha256")
            }
            evidence.update(
                stage=api.stage,
                failed_utc=datetime.now(UTC).isoformat(),
                http_status=getattr(exc, "status", None),
                request_id=getattr(exc, "request_id", None),
                rate_limit=getattr(exc, "rate_limit", {}),
            )
            path = _path(
                root,
                f"{item['date']}/notification-failures/{item['version_id']}/{uuid4().hex}.json",
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(path, (json.dumps(evidence, sort_keys=True, indent=2) + "\n").encode())
            LOG.error("日报通知失败 %s", json.dumps(evidence, sort_keys=True))
        raise


class _GitHub:
    def __init__(self, session, repository):
        self.stage = "find_issue"
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
            raise GitHubFailure(response)
        try:
            parsed = response.json()
        except ValueError:
            raise GitHubFailure(response, "GitHub 响应不是 JSON") from None
        if path == "/graphql" and (
            not isinstance(parsed, dict) or parsed.get("errors") or not parsed.get("data")
        ):
            raise GitHubFailure(
                response, "GitHub 置顶操作失败，请检查 Actions 的 issues: write 权限与运行记录"
            )
        return parsed

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


def _notification_version(root, body, today, repository, version_id=None):
    root = Path(root).absolute()
    if any(path.is_symlink() for path in (root, *root.parents)):
        raise ValueError("日报通知回执路径不允许符号链接")
    sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    matches = [
        v
        for v in rendered_versions(root, today)
        if v["markdown_sha256"] == sha and (version_id is None or v["version_id"] == version_id)
    ]
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


def monthly_issue_body(issue, comments, repository, today, root=None):
    """Keep dated report links newest first and show the latest report at the top."""
    reports = []
    for comment in comments or []:
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
    issue_url = f"https://github.com/{repository}/issues/{issue['number']}"
    entries = {}
    original = issue.get("body") or ""
    if comments is None:
        # A forbidden list is not evidence that former comment-only days disappeared.
        managed = original.partition(OVERVIEW_START)[2].partition(OVERVIEW_END)[0]
        index = managed.partition("**最新日报 ")[0]
        link_pattern = (
            rf"^- \[(\d{{4}}-\d{{2}}-\d{{2}})\]\(({re.escape(issue_url)}#issuecomment-[0-9]+)\)$"
        )
        for stamp, url in re.findall(link_pattern, index, re.MULTILINE):
            day = date.fromisoformat(stamp)
            if (day.year, day.month) == (today.year, today.month):
                entries[day] = ([f"- [{day}]({url})"], f"[查看完整日报]({url})", url)
    for day, comment_id, body in sorted(reports):
        link = f"{issue_url}#issuecomment-{comment_id}"
        entries[day] = ([f"- [{day}]({link})"], body, link)
    if root:
        for directory in sorted(root.glob(f"{today:%Y-%m}-??")):
            if not directory.is_dir():
                continue
            day = date.fromisoformat(directory.name)
            versions = sorted(rendered_versions(root, day), key=_version_order, reverse=True)
            if not versions:
                continue
            base_url = f"https://github.com/{repository}/blob/data/football-feeds/daily/{day}"
            links = []
            for item in versions:
                name = item["version_id"]
                link = f"{base_url}/{quote(name, safe='')}.md"
                links.append(f"- [{day} · {name}]({link})")
            newest = versions[0]["version_id"]
            markdown = _path(root, f"{day}/{newest}.md").read_text(encoding="utf-8")
            entries[day] = (links, markdown, f"{base_url}/{quote(newest, safe='')}.md")
    if not entries:
        raise ValueError("月报缺少可核实的当月日报，无法更新目录")
    latest_day = max(entries)
    links = "\n".join(line for day in sorted(entries, reverse=True) for line in entries[day][0])
    _, latest_body, url = entries[latest_day]
    overview = f"**日报目录（最新在前）**\n\n{links}\n\n**最新日报 {latest_day}**\n\n"
    full_link = f"[查看完整日报]({url})"
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


def _update_directory(api, issue, comments, repository, day, version):
    api.stage = "update_directory"
    overview = monthly_issue_body(issue, comments, repository, day, version[0] if version else None)
    if overview != (issue.get("body") or ""):
        api.request("PATCH", f"{api.root}/issues/{issue['number']}", json={"body": overview})


def _version_order(version):
    return instant(version["generated_utc"]), version["version_id"]


def _can_replace_comment(root, version, repository, issue_number, comment_id):
    """Only edit away a body whose exact delivery proof survives a fresh checkout."""
    receipt = read_notification(root, version)
    if not receipt or (receipt["repository"], receipt["issue_number"], receipt["comment_id"]) != (
        repository,
        issue_number,
        comment_id,
    ):
        return False
    path = notification_path(root, version).relative_to(root).as_posix()
    try:
        published = git_read(
            root, "cat-file", "blob", f"refs/remotes/origin/data/football-feeds:./{path}"
        )
        return json.loads(published) == receipt
    except (PublicationHistoryError, json.JSONDecodeError, UnicodeDecodeError):
        return False


def _deliver_comment(api, issue_number, matches, body, version, day):
    exact = [item for item in matches if item["body"] == body]
    if len(exact) > 1:
        raise ValueError("有多个相同版本的机器人评论")
    if exact:
        return exact[0]  # A previous attempt can have succeeded before its receipt was stored.
    target = None
    if matches:
        if version:
            root, requested = version
            by_hash = {}
            for item in rendered_versions(root, day):
                sha = item["markdown_sha256"]
                # Identical bodies cannot prove which of two version receipts is safe to retire.
                by_hash[sha] = item if sha not in by_hash else None
            ranked = {}
            for comment in matches:
                item = by_hash.get(hashlib.sha256(comment["body"].encode()).hexdigest())
                if item is None:
                    continue
                rank = _version_order(item)
                if rank in ranked:
                    raise ValueError("同日已有多条相同归档版本的机器人评论")
                ranked[rank] = (comment, item)
            if ranked:
                newest = max(ranked)
                comment, previous = ranked[newest]
                if newest <= _version_order(requested) and _can_replace_comment(
                    root, previous, f"{api.owner}/{api.name}", issue_number, comment["id"]
                ):
                    target = comment
        elif len(matches) == 1:
            target = matches[0]
        else:
            raise ValueError("同日已有多条机器人评论；停止以避免选择错误")
    if target:
        return api.request(
            "PATCH", f"{api.root}/issues/comments/{target['id']}", json={"body": body}
        )
    return api.request("POST", f"{api.root}/issues/{issue_number}/comments", json={"body": body})


def redeliver(root, day, version_id, repository, *, now=None):
    root = Path(root)
    item = next((v for v in rendered_versions(root, day) if v["version_id"] == version_id), None)
    if item is None:
        raise ValueError("找不到指定日期的已归档日报版本")
    body = _path(root, f"{day}/{version_id}.md").read_bytes().decode("utf-8")
    return publish(
        body, repository, now=now, history_root=root, report_day=day, version_id=version_id
    )


def publish(body, repository, *, now=None, history_root=None, report_day=None, version_id=None):
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
    clock = now or datetime.now(UTC)
    current_day = _day(clock)
    today = report_day or current_day
    manage_month = (today.year, today.month) == (current_day.year, current_day.month)
    if report_day and (not history_root or not version_id or report_day > current_day):
        raise ValueError("补发必须指定历史归档日期和版本")
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
    version = (
        _notification_version(history_root, body, today, repository, version_id)
        if history_root
        else None
    )
    if version and instant(version[1]["generated_utc"]) > instant(clock):
        raise ValueError("不能补发尚未生成的日报版本")
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
        with _record_failure(version, api):
            issues = api.pages(api.root + "/issues", state="all", sort="created", direction="desc")

            def find(wanted):
                matching = [i for i in issues if i["title"] == wanted and "pull_request" not in i]
                if len(matching) > 1:
                    raise ValueError(f"存在多条同名 Issue：{wanted}；停止以避免重复推送")
                return matching[0] if matching else None

            issue, previous = find(title), find(previous_title)
            if issue is None:
                api.stage = "create_issue"
                issue = api.request(
                    "POST",
                    api.root + "/issues",
                    json={
                        "title": title,
                        "body": DISCLAIMER
                        + "\n\n本月每日摘要；同日第二班更新原评论。请订阅此 Issue 查看通知。",
                    },
                )
            elif manage_month and issue["state"] == "closed":
                api.stage = "reopen_issue"
                api.request("PATCH", f"{api.root}/issues/{issue['number']}", json={"state": "open"})
            if manage_month:
                api.stage = "pin_issue"
                api.pin_current(issue, previous)
        comments = None
        try:
            with _record_failure(version, api):
                comment_path = f"{api.root}/issues/{issue['number']}/comments"
                api.stage = "list_comments"
                comments = api.pages(comment_path)
                matches = [
                    comment
                    for comment in comments
                    if marker in comment.get("body", "")
                    and comment.get("user", {}).get("login") == "github-actions[bot]"
                ]
                api.stage = "publish_comment"
                receipt = read_notification(*version) if version else None
                if receipt and (receipt["repository"], receipt["issue_number"]) != (
                    repository,
                    issue["number"],
                ):
                    raise ValueError("已有日报通知回执的目标冲突")
                if receipt:
                    # A subsequent shift may have edited the shared daily comment.
                    # This version already has proof; never write its older body back.
                    comment_id = receipt["comment_id"]
                else:
                    comment = _deliver_comment(
                        api,
                        issue["number"],
                        matches,
                        body,
                        version,
                        today,
                    )
                    comment_id = comment["id"]
                    if version:
                        _record_notification(*version, repository, issue, comment, body)
                    comments = [c for c in comments if c["id"] != comment_id] + [
                        {**comment, "body": body}
                    ]
        except GitHubFailure as exc:
            if version and exc.status == 403:
                with _record_failure(version, api):
                    _update_directory(api, issue, comments, repository, today, version)
            raise
        with _record_failure(version, api):
            _update_directory(api, issue, comments, repository, today, version)
            # Close only after the new month's report is successfully visible; safe to retry.
            if manage_month and previous and previous["state"] != "closed":
                api.stage = "close_previous"
                api.request(
                    "PATCH", f"{api.root}/issues/{previous['number']}", json={"state": "closed"}
                )
    LOG.info("摘要已发布到 Issue #%s，评论 %s", issue["number"], comment_id)
    return issue["number"], comment_id


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path)
    source.add_argument("--date", type=date.fromisoformat)
    parser.add_argument("--version")
    parser.add_argument("--history-root", type=Path, help="保存成功评论回执的 daily 根目录")
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"), required=False)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if args.date:
            if not args.version or not args.history_root:
                parser.error("--date 需要 --version 和 --history-root")
            redeliver(args.history_root, args.date, args.version, args.repository or "")
            return 0
        if args.version:
            parser.error("--version 需要 --date")
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
