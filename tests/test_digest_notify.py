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
    publish(BODY, FIXTURE["repository"], now=NOW)
    assert "page=2" in responses.calls[-2].request.url
    assert responses.calls[-1].request.url.endswith("/comments/201")


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
    body, *, now=NOW, edit=False, previous=False, status=200, changed=False, published_at=None
):
    issues = [FIXTURE["issue"]] + ([FIXTURE["previous"]] if previous else [])
    responses.get(ROOT + "/issues", json=issues)
    responses.post(API + "/graphql", json=pinned([FIXTURE["issue"]["node_id"]]))
    responses.get(ROOT + "/issues/101/comments", json=[FIXTURE["comment"]] if edit else [])
    published = {
        **FIXTURE["comment"],
        "body": body + "changed" if changed else body,
        "updated_at": (published_at or now + timedelta(seconds=1)).isoformat(),
    }
    if edit:
        responses.patch(ROOT + "/issues/comments/201", json=published, status=status)
    else:
        responses.post(ROOT + "/issues/101/comments", json=published, status=status)
    if previous:
        responses.patch(ROOT + "/issues/100", status=500)


@responses.activate
def test_failed_notification_version_is_not_the_next_issue_previous(actions, tmp_path):
    from zucai.digest_history import version_context
    from zucai.digest_notify import publish

    out = tmp_path / "daily"
    first, _ = render_version(out, "published", notified=True)
    when = NOW + timedelta(minutes=1)
    _, body = render_version(out, "failed", now=when, update=2)
    notification_routes(body, now=when, edit=True, status=500)
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
    notification_routes(second_body, now=when, edit=True)
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
