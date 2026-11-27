"""T80 offline replays through the Issue notification entry points."""

import hashlib
import json
from datetime import timedelta

import pytest
from responses import RequestsMock

from sfc_publication_support import prepare_remote
from test_digest_history import INPUTS, TODAY, render_version
from test_digest_notify import API, BODY, FIXTURE, NOW, ROOT, pinned
from test_digest_notify import actions as actions
from zucai import digest_notify
from zucai.digest_history import notification_path, read_notification, version_context

LIMIT = 65536
REPO = FIXTURE["repository"]
FIRST = "## 球队预测\n\n一队 | 二队  \n\n### 完整三级小节\n\n完整内容\n\n"
SECOND = "## 技术数据覆盖率\n\n"
ARCHIVE_URL = f"https://github.com/{REPO}/blob/data/football-feeds/daily/{TODAY}/first.md"


def report(size):
    header = BODY + "\n"
    fixed = header + FIRST + SECOND + "\n"
    return header + FIRST + SECOND + "数" * (size - len(fixed)) + "\n"


@pytest.fixture
def mailbox(actions):
    issue = {**FIXTURE["issue"], "body": "保留人工说明"}
    comments = {}
    writes = []
    notified = (NOW + timedelta(minutes=5)).isoformat()

    def list_issue(_):
        return 200, {}, json.dumps([issue])

    def list_comments(_):
        return 200, {}, json.dumps(list(comments.values()))

    def write_comment(request):
        body = json.loads(request.body)["body"]
        assert len(body) <= LIMIT
        comment = {**FIXTURE["comment"], "id": 201, "body": body, "updated_at": notified}
        writes.append((request.method, body))
        comments[201] = comment
        return 200, {}, json.dumps(comment)

    def write_issue(request):
        issue.update(json.loads(request.body))
        return 200, {}, json.dumps(issue)

    with RequestsMock(assert_all_requests_are_fired=False) as api:
        api.add_callback("GET", ROOT + "/issues", callback=list_issue)
        api.post(API + "/graphql", json=pinned([issue["node_id"]]))
        api.add_callback("GET", ROOT + "/issues/101/comments", callback=list_comments)
        api.add_callback("POST", ROOT + "/issues/101/comments", callback=write_comment)
        api.add_callback("PATCH", ROOT + "/issues/comments/201", callback=write_comment)
        api.add_callback("PATCH", ROOT + "/issues/101", callback=write_issue)
        yield issue, writes


@pytest.mark.parametrize("size", [65536, 65537, 70000])
def test_comment_boundary_and_sections_are_preserved(tmp_path, mailbox, size):
    root = tmp_path / "daily"
    version, full = render_version(root, "first", body=report(size))
    markdown = root / str(TODAY) / "first.md"
    details = markdown.with_suffix(".json").read_bytes()
    issue, writes = mailbox

    assert len(full) == size
    assert digest_notify.publish(full, REPO, now=NOW, history_root=root) == (101, 201)
    ((method, sent),) = writes
    assert method == "POST"
    assert len(sent) <= LIMIT
    assert sent.splitlines()[:2] == BODY.splitlines()[:2]
    assert sent.count(f"<!-- zucai-daily:{TODAY} -->") == 1
    if size == LIMIT:
        assert sent == full
    else:
        assert sent.startswith(BODY + "\n" + FIRST)
        assert SECOND not in sent
        assert "超过单条评论上限" in sent
        assert "省略章节" in sent and "技术数据覆盖率" in sent
        assert ARCHIVE_URL in sent
    index, _, latest = issue["body"].partition(f"**最新日报 {TODAY}**")
    assert [line for line in index.splitlines() if line.startswith("- ")] == [
        f"- [{TODAY}](https://github.com/{REPO}/issues/101#issuecomment-201)"
    ]
    assert "### 完整三级小节" in latest and f"[查看完整日报]({ARCHIVE_URL})" in latest
    receipt = read_notification(root, version)
    assert receipt["markdown_sha256"] == hashlib.sha256(full.encode()).hexdigest()
    assert receipt["comment_sha256"] == hashlib.sha256(sent.encode()).hexdigest()
    assert receipt["shortened"] is (size > LIMIT)
    assert (
        markdown.read_bytes() == full.encode()
        and markdown.with_suffix(".json").read_bytes() == details
    )


def test_first_section_cannot_fit_but_explanation_is_delivered(tmp_path, mailbox):
    root = tmp_path / "daily"
    first_section = FIRST + "长" * LIMIT + "\n"
    _, full = render_version(root, "first", body=BODY + first_section + SECOND + "短节\n")
    _, writes = mailbox
    assert digest_notify.publish(full, REPO, now=NOW, history_root=root) == (101, 201)
    ((method, sent),) = writes
    assert method == "POST"
    assert sent.splitlines()[:2] == BODY.splitlines()[:2]
    assert not any(line.startswith("## ") for line in sent.splitlines())
    assert "球队预测" in sent and "技术数据覆盖率" in sent
    assert "短节" not in sent and "完整内容" not in sent
    assert "超过单条评论上限" in sent and ARCHIVE_URL in sent


@pytest.mark.parametrize(
    "watchdog",
    [
        "",
        "自动保险检查：北京时间 2026-11-03 18:05:00，采集机闹钟触发。\n\n",
        "本档自动保险检查没有运行。\n\n",
        "自动保险检查：未能读取 Actions 运行记录。\n\n",
    ],
)
def test_watchdog_and_coverage_survive_when_sections_cannot_fit(tmp_path, mailbox, watchdog):
    root = tmp_path / "daily"
    counts = "覆盖情况｜14 场 26171 期：正式预测 13/14，基准 1/14。"
    first_section = FIRST + "长" * LIMIT + "\n"
    header = "\n".join(BODY.splitlines()[:2])
    _, full = render_version(
        root,
        "first",
        body=f"{header}\n\n# 每日摘要 {TODAY}（北京时间）\n\n{watchdog}{counts}\n\n{first_section}",
    )
    issue, writes = mailbox
    assert digest_notify.publish(full, REPO, now=NOW, history_root=root) == (101, 201)
    ((_, sent),) = writes
    assert counts in sent and counts in issue["body"]
    assert watchdog in sent and watchdog in issue["body"]
    assert ARCHIVE_URL in sent and "完整内容" not in sent


@pytest.mark.parametrize("receipt_failed", [False, True])
def test_redelivery_recovers_short_reply_then_full_shift_edits_it(
    tmp_path, mailbox, monkeypatch, receipt_failed
):
    root = tmp_path / "daily"
    first, full = render_version(root, "first", body=report(70000))
    _, writes = mailbox
    path = notification_path(root, first)
    frozen = path.with_name("first.md").read_bytes()

    if receipt_failed:
        write = digest_notify._atomic_write

        def fail_receipt(destination, data):
            if destination == path:
                raise OSError("simulated receipt write failure")
            return write(destination, data)

        with monkeypatch.context() as failed:
            failed.setattr(digest_notify, "_atomic_write", fail_receipt)
            with pytest.raises(OSError, match="receipt"):
                digest_notify.redeliver(root, TODAY, "first", REPO, now=NOW)
        assert read_notification(root, first) is None
    else:
        assert digest_notify.publish(full, REPO, now=NOW, history_root=root) == (101, 201)

    when = NOW + timedelta(minutes=6)
    assert digest_notify.redeliver(root, TODAY, "first", REPO, now=when) == (101, 201)
    assert [method for method, _ in writes] == ["POST"]
    prior = read_notification(root, first)
    assert prior["shortened"] is True
    assert prior["comment_sha256"] == hashlib.sha256(writes[0][1].encode()).hexdigest()
    assert prior["notified_utc"] == (NOW + timedelta(minutes=5)).isoformat()
    assert version_context(tmp_path, root, TODAY, when, "next", INPUTS, require_notified=True) == (
        first,
        2,
        "next",
    )

    second, body = render_version(root, "second", now=when, update=2)
    prepare_remote(root.parent)
    assert digest_notify.publish(body, REPO, now=when, history_root=root) == (101, 201)
    assert [method for method, _ in writes] == ["POST", "PATCH"]
    assert writes[-1][1] == body
    assert read_notification(root, second)["shortened"] is False
    assert (
        read_notification(root, first) == prior
        and path.with_name("first.md").read_bytes() == frozen
    )


@pytest.mark.parametrize("room", ["shortened", "link"])
def test_monthly_archive_keeps_directory_while_fitting_latest(tmp_path, room):
    root = tmp_path / "daily"
    _, full = render_version(root, "first", body=report(70000))
    suffix = "\n说明结束"
    link = f"[查看完整日报]({ARCHIVE_URL})"
    required = (
        f"\n{digest_notify.OVERVIEW_START}\n\n**日报目录（最新在前）**\n\n"
        f"- [{TODAY}]({ARCHIVE_URL})\n\n**最新日报 {TODAY}**\n\n"
        f"{link}\n\n{digest_notify.OVERVIEW_END}{suffix}"
    )
    prefix = ("注" * (LIMIT - len(required)) if room == "link" else "人工说明") + "\n"
    issue = {
        **FIXTURE["issue"],
        "body": prefix + digest_notify.OVERVIEW_START + digest_notify.OVERVIEW_END + suffix,
    }
    result = digest_notify.monthly_issue_body(issue, [], REPO, TODAY, root)
    assert len(result) <= LIMIT and result.startswith(prefix) and result.endswith(suffix)
    assert f"- [{TODAY}]({ARCHIVE_URL})" in result
    assert full not in result
    if room == "shortened":
        assert FIRST in result and "省略章节" in result and "技术数据覆盖率" in result
    else:
        # Leave just enough space for a link while preserving the managed directory.
        assert len(result) == LIMIT and link in result and "省略章节" not in result
