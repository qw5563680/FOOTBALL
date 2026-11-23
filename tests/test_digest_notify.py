"""All GitHub operations intercepted; never post to a real repository."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import responses

from test_digest_history import INPUTS, TODAY, render_version

FIXTURE = json.loads((Path(__file__).parents[1] / "data/fixtures/t25/github.json").read_bytes())
API = "https://api.github.com"
ROOT = f"{API}/repos/{FIXTURE['repository']}"
NOW = datetime(2026, 11, 3, 10, tzinfo=UTC)
BODY = "非购买建议 · BET 禁用\n<!-- zucai-daily:2026-11-03 -->\n摘要\n"


@pytest.fixture
def actions(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("GITHUB_TOKEN", "offline-token")


def pinned(ids):
    return {
        "data": {"repository": {"pinnedIssues": {"nodes": [{"issue": {"id": i}} for i in ids]}}}
    }


@pytest.mark.parametrize("mode", ["create", "reuse", "edit"])
@responses.activate
def test_monthly_issue_create_reuse_and_same_day_edit(actions, mode):
    from zucai.digest_notify import publish

    issue, comment = FIXTURE["issue"], FIXTURE["comment"]
    responses.get(ROOT + "/issues", json=[] if mode == "create" else [issue])
    if mode == "create":
        responses.post(ROOT + "/issues", json=issue, status=201)
        responses.post(API + "/graphql", json=pinned([]))
        responses.post(API + "/graphql", json={"data": {"pinIssue": {"clientMutationId": None}}})
    else:
        responses.post(API + "/graphql", json=pinned([issue["node_id"]]))
    responses.get(ROOT + "/issues/101/comments", json=[comment] if mode == "edit" else [])
    if mode == "edit":
        responses.patch(ROOT + "/issues/comments/201", json=comment)
    else:
        responses.post(ROOT + "/issues/101/comments", json=comment, status=201)
    responses.patch(ROOT + "/issues/101", json=issue)
    assert publish(BODY, FIXTURE["repository"], now=NOW) == (101, 201)
    writes = [call for call in responses.calls if call.request.method in {"POST", "PATCH"}]
    comment_write = [c for c in writes if "/comments" in c.request.url]
    assert len(comment_write) == 1
    assert json.loads(comment_write[0].request.body) == {"body": BODY}
    assert comment_write[0].request.method == ("PATCH" if mode == "edit" else "POST")
    assert all(
        call.request.headers["Authorization"] == "Bearer offline-token" for call in responses.calls
    )


@responses.activate
def test_month_rollover_unpins_and_closes_only_previous_report(actions):
    from zucai.digest_notify import publish

    previous, issue = FIXTURE["previous"], FIXTURE["issue"]
    responses.get(ROOT + "/issues", json=[previous, {"title": "Unrelated", "number": 99}])
    responses.post(ROOT + "/issues", json=issue, status=201)
    responses.post(API + "/graphql", json=pinned([previous["node_id"]]))
    responses.post(API + "/graphql", json={"data": {"unpinIssue": {"clientMutationId": None}}})
    responses.post(API + "/graphql", json={"data": {"pinIssue": {"clientMutationId": None}}})
    responses.get(ROOT + "/issues/101/comments", json=[])
    responses.post(ROOT + "/issues/101/comments", json=FIXTURE["comment"], status=201)
    responses.patch(ROOT + "/issues/101", json=issue)
    responses.patch(ROOT + "/issues/100", json={**previous, "state": "closed"})
    # Nov 1 in Beijing, still Oct 31 UTC. Rollover is based on Beijing, not UTC.
    first = datetime(2026, 10, 31, 16, tzinfo=UTC)
    publish(BODY.replace("11-03", "11-01"), FIXTURE["repository"], now=first)
    mutations = [
        json.loads(c.request.body) for c in responses.calls if c.request.url.endswith("graphql")
    ]
    assert mutations[1]["variables"]["id"] == previous["node_id"]
    assert mutations[2]["variables"]["id"] == issue["node_id"]
    assert responses.calls[-1].request.url.endswith("/issues/100")


@responses.activate
def test_guard_and_stale_digest_do_not_make_requests(monkeypatch, actions):
    from zucai.digest_notify import publish

    monkeypatch.delenv("GITHUB_ACTIONS")
    with pytest.raises(ValueError, match="Actions"):
        publish(BODY, FIXTURE["repository"], now=NOW)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with pytest.raises(ValueError, match="当日"):
        publish(BODY.replace("11-03", "11-02"), FIXTURE["repository"], now=NOW)
    assert not responses.calls


@responses.activate
def test_github_failure_does_not_leak_response_or_token(actions):
    from zucai.digest_notify import publish

    responses.get(ROOT + "/issues", status=403, json={"message": "offline-token secret"})
    with pytest.raises(ValueError, match="GitHub HTTP 403") as error:
        publish(BODY, FIXTURE["repository"], now=NOW)
    assert "secret" not in str(error.value) and "offline-token" not in str(error.value)


@responses.activate
def test_paginated_comments_edit_bot_comment_only(actions):
    from zucai.digest_notify import publish

    responses.get(ROOT + "/issues", json=[FIXTURE["issue"]])
    responses.post(API + "/graphql", json=pinned([FIXTURE["issue"]["node_id"]]))
    human = {**FIXTURE["comment"], "id": 999, "user": {"login": "owner"}}
    responses.get(ROOT + "/issues/101/comments", json=[human] * 100)
    responses.get(ROOT + "/issues/101/comments", json=[FIXTURE["comment"]])
    responses.patch(ROOT + "/issues/comments/201", json=FIXTURE["comment"])
    responses.patch(ROOT + "/issues/101", json=FIXTURE["issue"])
    publish(BODY, FIXTURE["repository"], now=NOW)
    comment_reads = [
        call
        for call in responses.calls
        if call.request.method == "GET" and "/comments?" in call.request.url
    ]
    assert "page=2" in comment_reads[-1].request.url
    comment_writes = [
        call
        for call in responses.calls
        if call.request.method == "PATCH" and "/comments/" in call.request.url
    ]
    assert comment_writes[-1].request.url.endswith("/comments/201")


@responses.activate
def test_pin_failure_is_explicit_and_does_not_post_comment(actions):
    from zucai.digest_notify import publish

    responses.get(ROOT + "/issues", json=[FIXTURE["issue"]])
    responses.post(API + "/graphql", json={"errors": [{"message": "offline-token private"}]})
    with pytest.raises(ValueError, match="置顶操作失败") as error:
        publish(BODY, FIXTURE["repository"], now=NOW)
    assert "private" not in str(error.value)
    assert not any("/comments" in call.request.url for call in responses.calls)


def notification_routes(
    body, *, now=NOW, prior_body=None, previous=False, status=200, changed=False, published_at=None
):
    issues = [FIXTURE["issue"]] + ([FIXTURE["previous"]] if previous else [])
    responses.get(ROOT + "/issues", json=issues)
    responses.post(API + "/graphql", json=pinned([FIXTURE["issue"]["node_id"]]))
    responses.get(
        ROOT + "/issues/101/comments",
        json=[dict(FIXTURE["comment"], body=prior_body)] if prior_body is not None else [],
    )
    published = {
        **FIXTURE["comment"],
        "body": body + "changed" if changed else body,
        "updated_at": (published_at or now + timedelta(seconds=1)).isoformat(),
    }
    if prior_body is not None:
        responses.patch(ROOT + "/issues/comments/201", json=published, status=status)
    else:
        responses.post(ROOT + "/issues/101/comments", json=published, status=status)
    responses.patch(ROOT + "/issues/101", json=FIXTURE["issue"])
    if previous:
        responses.patch(ROOT + "/issues/100", status=500)


@responses.activate
def test_failed_notification_version_is_not_the_next_issue_previous(actions, tmp_path):
    from zucai.digest_history import version_context
    from zucai.digest_notify import publish

    out = tmp_path / "daily"
    first, first_body = render_version(out, "published", notified=True)
    when = NOW + timedelta(minutes=1)
    _, body = render_version(out, "failed", now=when, update=2)
    notification_routes(body, now=when, prior_body=first_body, status=500)
    with pytest.raises(ValueError, match="GitHub HTTP 500"):
        publish(body, FIXTURE["repository"], now=when, history_root=out)
    assert not (out / str(TODAY) / "failed.notification.json").exists()
    previous, update, _ = version_context(
        tmp_path / "archive",
        out,
        TODAY,
        when + timedelta(minutes=1),
        "next",
        INPUTS,
        require_notified=True,
    )
    assert previous == first and update == 2


@responses.activate
def test_success_receipts_follow_same_day_comment_edits_without_changing_frozen_versions(
    actions, tmp_path
):
    from zucai.digest_history import version_context
    from zucai.digest_notify import publish

    out = tmp_path / "daily"
    first, first_body = render_version(out, "first")
    originals = {path: path.read_bytes() for path in (out / str(TODAY)).iterdir() if path.is_file()}
    notification_routes(first_body)
    assert publish(first_body, FIXTURE["repository"], now=NOW, history_root=out) == (101, 201)
    first_receipt = json.loads((out / str(TODAY) / "first.notification.json").read_bytes())
    assert first_receipt["markdown_sha256"] == first["markdown_sha256"]
    assert first_receipt["notified_utc"] == (NOW + timedelta(seconds=1)).isoformat()
    assert all(path.read_bytes() == raw for path, raw in originals.items())
    when = NOW + timedelta(minutes=1)
    second, second_body = render_version(out, "second", now=when, update=2)
    notification_routes(second_body, now=when, prior_body=first_body)
    assert publish(second_body, FIXTURE["repository"], now=when, history_root=out) == (101, 201)
    previous, update, _ = version_context(
        tmp_path / "archive",
        out,
        TODAY,
        when + timedelta(minutes=1),
        "next",
        INPUTS,
        require_notified=True,
    )
    assert previous == second and update == 3
    writes = [
        call.request.method
        for call in responses.calls
        if "/comments" in call.request.url and call.request.method in {"POST", "PATCH"}
    ]
    assert writes == ["POST", "PATCH"]
    assert all(path.read_bytes() == raw for path, raw in originals.items())


@responses.activate
def test_success_receipt_survives_subsequent_month_cleanup_failure(actions, tmp_path):
    from zucai.digest_notify import publish

    out = tmp_path / "daily"
    value, body = render_version(out, "published-before-cleanup")
    notification_routes(body, previous=True)
    with pytest.raises(ValueError, match="GitHub HTTP 500"):
        publish(body, FIXTURE["repository"], now=NOW, history_root=out)
    receipt = json.loads(
        (out / str(TODAY) / "published-before-cleanup.notification.json").read_bytes()
    )
    assert receipt["markdown_sha256"] == value["markdown_sha256"]
    assert (receipt["issue_number"], receipt["comment_id"]) == (101, 201)


@responses.activate
def test_mismatched_success_response_cannot_create_a_hash_receipt(actions, tmp_path):
    from zucai.digest_notify import publish

    out = tmp_path / "daily"
    _, body = render_version(out, "first")
    notification_routes(body, changed=True)
    with pytest.raises(ValueError, match="响应正文"):
        publish(body, FIXTURE["repository"], now=NOW, history_root=out)
    assert not list(out.rglob("*.notification.json"))


@responses.activate
def test_unfrozen_body_is_rejected_before_notification_requests(actions, tmp_path):
    from zucai.digest_notify import publish

    with pytest.raises(ValueError, match="唯一已冻结"):
        publish(BODY, FIXTURE["repository"], now=NOW, history_root=tmp_path / "daily")
    assert not responses.calls


@responses.activate
def test_github_whole_second_receipt_accepts_same_second_microsecond_generation(actions, tmp_path):
    from zucai.digest_history import version_context
    from zucai.digest_notify import publish

    out = tmp_path / "daily"
    generated = NOW + timedelta(microseconds=900_000)
    first, body = render_version(out, "same-second", now=generated)
    notification_routes(body, now=generated, published_at=NOW)
    publish(body, FIXTURE["repository"], now=generated, history_root=out)
    receipt = json.loads((out / str(TODAY) / "same-second.notification.json").read_bytes())
    assert receipt["notified_utc"] == NOW.isoformat()  # Preserve the server time exactly.
    previous, update, _ = version_context(
        tmp_path / "archive",
        out,
        TODAY,
        NOW + timedelta(microseconds=950_000),
        "next",
        INPUTS,
        require_notified=True,
    )
    assert previous == first and update == 2


@pytest.mark.parametrize("edit", [False, True])
@responses.activate
def test_monthly_issue_shows_latest_report_and_reverse_date_links(actions, edit):
    from zucai.digest_notify import publish

    issue = {
        **FIXTURE["issue"],
        "body": "Owner notes\n<!-- zucai-daily-overview:start -->\nStale overview\n"
        "<!-- zucai-daily-overview:end -->\nMore owner notes",
    }
    older = {**FIXTURE["comment"], "id": 190, "body": BODY.replace("11-03", "11-02")}
    oldest = {
        **FIXTURE["comment"],
        "id": 180,
        "body": BODY.replace("11-03", "11-01"),
        "updated_at": "2026-11-04T10:00:00Z",
    }
    human = {**FIXTURE["comment"], "id": 999, "user": {"login": "owner"}}
    other_month = {**FIXTURE["comment"], "id": 170, "body": BODY.replace("11-03", "10-31")}
    comments = [oldest, older, human, other_month]
    if edit:
        comments.append(FIXTURE["comment"])
    responses.get(ROOT + "/issues", json=[issue])
    responses.post(API + "/graphql", json=pinned([issue["node_id"]]))
    responses.get(ROOT + "/issues/101/comments", json=comments)
    published = {**FIXTURE["comment"], "body": BODY}
    if edit:
        responses.patch(ROOT + "/issues/comments/201", json=published)
    else:
        responses.post(ROOT + "/issues/101/comments", json=published, status=201)
    responses.patch(ROOT + "/issues/101", json=issue)

    publish(BODY, FIXTURE["repository"], now=NOW)

    updates = [c for c in responses.calls if c.request.url == ROOT + "/issues/101"]
    assert len(updates) == 1
    visible = json.loads(updates[0].request.body)["body"]
    assert visible.index("[2026-11-03]") < visible.index("[2026-11-02]")
    assert visible.index("[2026-11-02]") < visible.index("[2026-11-01]")
    assert "issues/101#issuecomment-201" in visible
    assert "issues/101#issuecomment-999" not in visible
    assert "2026-10-31" not in visible
    assert BODY in visible and "旧摘要" not in visible and "Stale overview" not in visible
    assert visible.startswith("Owner notes\n") and visible.endswith("\nMore owner notes")
    assert visible.count("<!-- zucai-daily-overview:start -->") == 1


@responses.activate
def test_monthly_issue_links_full_comment_when_report_nearly_fills_body_limit(actions):
    from zucai.digest_notify import publish

    body = BODY + "数" * (65536 - len(BODY))
    issue = {**FIXTURE["issue"], "body": "Owner notes"}
    responses.get(ROOT + "/issues", json=[issue])
    responses.post(API + "/graphql", json=pinned([issue["node_id"]]))
    responses.get(ROOT + "/issues/101/comments", json=[])
    responses.post(
        ROOT + "/issues/101/comments", json={**FIXTURE["comment"], "body": body}, status=201
    )
    responses.patch(ROOT + "/issues/101", json=issue)

    publish(body, FIXTURE["repository"], now=NOW)

    updates = [c for c in responses.calls if c.request.url == ROOT + "/issues/101"]
    assert len(updates) == 1
    visible = json.loads(updates[0].request.body)["body"]
    assert len(visible) <= 65536 and visible.startswith("Owner notes")
    assert "issues/101#issuecomment-201" in visible
    comment_write = next(
        c for c in responses.calls if c.request.method == "POST" and "/comments" in c.request.url
    )
    assert json.loads(comment_write.request.body)["body"] == body


def test_monthly_overview_repeat_is_identical_and_keeps_manual_notes():
    from zucai.digest_notify import monthly_issue_body

    issue = {**FIXTURE["issue"], "body": "Owner notes"}
    comments = [{**FIXTURE["comment"], "body": BODY}]
    visible = monthly_issue_body(issue, comments, FIXTURE["repository"], NOW.date())
    assert (
        monthly_issue_body({**issue, "body": visible}, comments, FIXTURE["repository"], NOW.date())
        == visible
    )


@pytest.mark.parametrize(
    "existing", ["<!-- zucai-daily-overview:start -->", "<!-- zucai-daily-overview:end -->"]
)
def test_broken_monthly_overview_keeps_existing_manual_text(existing):
    from zucai.digest_notify import monthly_issue_body

    issue = {**FIXTURE["issue"], "body": "Owner notes\n" + existing}
    comments = [{**FIXTURE["comment"], "body": BODY}]
    with pytest.raises(ValueError, match="标记不完整"):
        monthly_issue_body(issue, comments, FIXTURE["repository"], NOW.date())
    assert issue["body"] == "Owner notes\n" + existing


@responses.activate
def test_notification_receipt_survives_overview_update_failure(actions, tmp_path):
    from zucai.digest_notify import publish

    out = tmp_path / "daily"
    value, body = render_version(out, "published-before-overview")
    notification_routes(body)
    responses.replace(responses.PATCH, ROOT + "/issues/101", status=500)
    with pytest.raises(ValueError, match="GitHub HTTP 500"):
        publish(body, FIXTURE["repository"], now=NOW, history_root=out)
    receipt = json.loads(
        (out / str(TODAY) / "published-before-overview.notification.json").read_bytes()
    )
    assert receipt["markdown_sha256"] == value["markdown_sha256"]
    assert (receipt["issue_number"], receipt["comment_id"]) == (101, 201)


@responses.activate
def test_redeliver_archived_version_after_forbidden_is_idempotent(actions, tmp_path):
    from zucai.digest_notify import redeliver

    root = tmp_path / "daily"
    version, body = render_version(root, "first")
    render_version(root, "other", now=NOW + timedelta(minutes=5), update=2)
    before = {p: p.read_bytes() for p in (root / str(TODAY)).iterdir()}
    repo = FIXTURE["repository"]
    later = NOW + timedelta(days=2)
    responses.get(ROOT + "/issues", json=[FIXTURE["issue"]])
    responses.post(API + "/graphql", json=pinned([FIXTURE["issue"]["node_id"]]))
    responses.get(ROOT + "/issues/101/comments", json=[])
    responses.post(
        ROOT + "/issues/101/comments",
        status=403,
        body="secret response",
        headers={
            "X-GitHub-Request-Id": "FACE:1234",
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": "1793760000",
            "Retry-After": "60",
            "Set-Cookie": "secret",
        },
    )
    responses.patch(ROOT + "/issues/101", json=FIXTURE["issue"])
    with pytest.raises(ValueError, match="403"):
        redeliver(root, TODAY, "first", repo, now=later)
    (failure,) = (root / str(TODAY) / "notification-failures").rglob("*.json")
    evidence = json.loads(failure.read_bytes())
    assert (evidence["stage"], evidence["http_status"], evidence["request_id"]) == (
        "publish_comment",
        403,
        "FACE:1234",
    )
    assert evidence["rate_limit"] == {
        "x-ratelimit-remaining": "0",
        "x-ratelimit-reset": "1793760000",
        "retry-after": "60",
    }
    assert "secret" not in failure.read_text() and "offline-token" not in failure.read_text()
    assert not (root / str(TODAY) / "first.notification.json").exists()
    responses.reset()
    notification_routes(body, now=later)
    assert redeliver(root, TODAY, "first", repo, now=later) == (101, 201)
    patches = [
        json.loads(c.request.body)["body"]
        for c in responses.calls
        if c.request.method == "PATCH" and c.request.url.endswith("/101")
    ]
    assert all(
        f"/blob/data/football-feeds/daily/{TODAY}/{name}.md" in patches[0]
        for name in ("first", "other")
    )
    receipt = root / str(TODAY) / "first.notification.json"
    raw = receipt.read_bytes()
    assert json.loads(raw)["markdown_sha256"] == version["markdown_sha256"]
    responses.reset()
    responses.get(ROOT + "/issues", json=[dict(FIXTURE["issue"], body=patches[0])])
    responses.post(API + "/graphql", json=pinned([FIXTURE["issue"]["node_id"]]))
    responses.get(ROOT + "/issues/101/comments", json=[dict(FIXTURE["comment"], body=body)])
    assert redeliver(root, TODAY, "first", repo, now=later) == (101, 201)
    assert all(
        c.request.method == "GET" or c.request.url.endswith("/graphql") for c in responses.calls
    )
    assert receipt.read_bytes() == raw and all(p.read_bytes() == v for p, v in before.items())


@responses.activate
def test_older_unnotified_version_preserves_newer_comment_and_history(actions, tmp_path):
    from zucai.digest_history import version_context
    from zucai.digest_notify import redeliver

    out = tmp_path / "daily"
    _, older = render_version(out, "older")
    newer, newer_body = render_version(
        out, "newer", now=NOW + timedelta(hours=1), update=2, notified=True
    )
    when = NOW + timedelta(days=31)
    responses.get(ROOT + "/issues", json=[dict(FIXTURE["issue"], state="closed")])
    responses.get(ROOT + "/issues/101/comments", json=[dict(FIXTURE["comment"], body=newer_body)])
    responses.post(
        ROOT + "/issues/101/comments",
        json=dict(FIXTURE["comment"], id=202, body=older, updated_at=when.isoformat()),
        status=201,
    )
    responses.patch(ROOT + "/issues/101", json=FIXTURE["issue"])
    assert redeliver(out, TODAY, "older", FIXTURE["repository"], now=when) == (101, 202)
    index = json.loads(responses.calls[-1].request.body)
    assert list(index) == ["body"] and newer_body in index["body"]
    assert not any(
        c.request.url.endswith("graphql") or c.request.url.endswith("/comments/201")
        for c in responses.calls
    )
    prev, *_ = version_context(
        tmp_path / "archive",
        out,
        TODAY,
        when + timedelta(minutes=1),
        "next",
        INPUTS,
        require_notified=True,
    )
    assert prev == newer


@responses.activate
def test_named_retry_rejects_wrong_day_version_or_hash_before_network(actions, tmp_path):
    from zucai.digest_notify import main

    root = tmp_path / "daily"
    _, body = render_version(root, "valid")
    args = [
        "--history-root",
        str(root),
        "--repository",
        FIXTURE["repository"],
        "--date",
        str(TODAY),
        "--version",
    ]
    assert main([*args, "wrong"]) == 1
    (root / str(TODAY) / "valid.md").write_text(body + "modified")
    assert main([*args, "valid"]) == 1
    assert not responses.calls


@pytest.mark.parametrize("method", ["GET", "POST", "PATCH"])
@responses.activate
def test_forbidden_comments_still_link_archive_without_receipt(actions, tmp_path, method):
    from zucai.digest_notify import monthly_issue_body, publish

    root = tmp_path / "daily"
    version, body = render_version(root, "frozen")
    repo = FIXTURE["repository"]
    older = dict(FIXTURE["comment"], id=170, body=BODY.replace("11-03", "11-02"))
    issue = dict(FIXTURE["issue"], body="人工说明")
    issue["body"] = monthly_issue_body(issue, [older], repo, TODAY)
    responses.get(ROOT + "/issues", json=[issue])
    responses.post(API + "/graphql", json=pinned([issue["node_id"]]))
    if method == "GET":
        responses.get(ROOT + "/issues/101/comments", status=403)
    elif method == "POST":
        responses.get(ROOT + "/issues/101/comments", json=[older])
        responses.post(ROOT + "/issues/101/comments", status=403)
    else:
        _, prior = render_version(root, "prior", now=NOW - timedelta(hours=1))
        responses.get(
            ROOT + "/issues/101/comments", json=[older, dict(FIXTURE["comment"], body=prior)]
        )
        responses.patch(ROOT + "/issues/comments/201", status=403)
    responses.patch(ROOT + "/issues/101", json=issue)
    with pytest.raises(ValueError, match="403"):
        publish(body, repo, now=NOW, history_root=root)
    (patch,) = [
        c
        for c in responses.calls
        if c.request.method == "PATCH" and c.request.url.endswith("/issues/101")
    ]
    directory = json.loads(patch.request.body)["body"]
    assert f"/daily/{TODAY}/frozen.md" in directory and body in directory
    assert "人工说明" in directory and "#issuecomment-170" in directory
    assert not list(root.rglob("*.notification.json"))
    evidence = json.loads(next(root.rglob("notification-failures/*/*.json")).read_bytes())
    assert (evidence["stage"], evidence["http_status"], evidence["markdown_sha256"]) == (
        "list_comments" if method == "GET" else "publish_comment",
        403,
        version["markdown_sha256"],
    )


@pytest.mark.parametrize("first_regular", [False, True])
@responses.activate
def test_legacy_orphan_retries_then_regular_shift(actions, tmp_path, first_regular):
    from zucai.digest_notify import publish, redeliver

    root = tmp_path / "daily"
    repo = FIXTURE["repository"]
    issue = dict(FIXTURE["issue"])
    legacy = dict(FIXTURE["comment"])
    comments = {legacy["id"]: legacy}
    writes = []

    def list_issues(_):
        return 200, {}, json.dumps([issue])

    def list_comments(_):
        return 200, {}, json.dumps(list(comments.values()))

    def write_comment(request):
        comment_id = (
            int(request.url.rsplit("/", 1)[1]) if request.method == "PATCH" else max(comments) + 1
        )
        assert comment_id != legacy["id"]
        body = json.loads(request.body)["body"]
        comments[comment_id] = dict(
            legacy, id=comment_id, body=body, updated_at=(NOW + timedelta(hours=4)).isoformat()
        )
        writes.append((request.method, comment_id))
        return 200, {}, json.dumps(comments[comment_id])

    def write_issue(request):
        issue.update(json.loads(request.body))
        return 200, {}, json.dumps(issue)

    responses.add_callback(responses.GET, ROOT + "/issues", callback=list_issues)
    responses.post(API + "/graphql", json=pinned([issue["node_id"]]))
    responses.add_callback(responses.GET, ROOT + "/issues/101/comments", callback=list_comments)
    responses.add_callback(responses.POST, ROOT + "/issues/101/comments", callback=write_comment)
    responses.add_callback(responses.PATCH, ROOT + "/issues/comments/202", callback=write_comment)
    responses.add_callback(responses.PATCH, ROOT + "/issues/101", callback=write_issue)
    later = NOW + timedelta(hours=4)
    originals = {}
    for n, name in enumerate(("A", "B", "C"), start=1):
        _, body = render_version(root, name, now=NOW + timedelta(hours=n), update=n)
        originals[name] = body
        if name == "C" or (name == "A" and first_regular):
            result = publish(body, repo, now=later, history_root=root)
        else:
            result = redeliver(root, TODAY, name, repo, now=later)
            assert redeliver(root, TODAY, name, repo, now=later) == result
        assert result == (101, 202) and comments[202]["body"] == body
    assert redeliver(root, TODAY, "A", repo, now=later) == (101, 202)
    assert redeliver(root, TODAY, "B", repo, now=later) == (101, 202)
    assert writes == [("POST", 202), ("PATCH", 202), ("PATCH", 202)]
    assert comments == {201: legacy, 202: dict(comments[202], body=originals["C"])}
    assert all(f"/daily/{TODAY}/{name}.md" in issue["body"] for name in originals)
    assert len(list(root.rglob("*.notification.json"))) == 3
