"""Rate budgets and diagnostics without GitHub network calls."""

import json
from datetime import UTC, datetime

import pytest
import requests

from zucai import github_api

NOW = int(datetime(2026, 11, 25, 12, tzinfo=UTC).timestamp())
TOKEN = "never-print-this-github-token"


def response(status=200, *, body=None, **headers):
    result = requests.Response()
    result.status_code = status
    result.headers.update(headers)
    result._content = json.dumps(body or {}).encode()
    result._content_consumed = True
    return result


@pytest.fixture
def clock(monkeypatch, tmp_path):
    now, waits = [NOW], []

    def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(github_api.time, "time", lambda: now[0])
    monkeypatch.setattr(github_api.time, "sleep", sleep)
    monkeypatch.setenv("ZUCAI_GITHUB_DEADLINE", str(NOW + 60))
    monkeypatch.setenv("ZUCAI_GITHUB_USAGE_FILE", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary"))
    return waits, tmp_path


def test_exhausted_budget_waits_for_reset_then_retries_once(clock, caplog):
    waits, root = clock
    releases = iter(
        [
            response(
                403,
                body={"message": f"API rate limit exceeded for installation {TOKEN}"},
                **{"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(NOW + 4)},
            ),
            response(201, **{"X-RateLimit-Remaining": "999", "X-RateLimit-Reset": str(NOW + 3600)}),
        ]
    )
    rewinds = []
    api = github_api.GitHubApi()

    assert (
        api.request(lambda: next(releases), before_retry=lambda: rewinds.append(1)).status_code
        == 201
    )
    assert waits == [5] and rewinds == [1]
    github_api.main(["summary"])
    summary = (root / "summary").read_text()
    assert "调用 2 次" in summary and "999" in summary and "21:00:00" in summary
    assert TOKEN not in summary + (root / "usage.jsonl").read_text() + caplog.text


def test_deadline_shorter_than_reset_never_retries(clock, monkeypatch):
    waits, _ = clock
    monkeypatch.setenv("ZUCAI_GITHUB_DEADLINE", str(NOW + 2))
    calls = []

    def send():
        calls.append(1)
        return response(
            403,
            body={"message": f"API rate limit exceeded for installation {TOKEN}"},
            **{
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(NOW + 4),
                "X-GitHub-Request-Id": "AB12:12AB",
            },
        )

    with pytest.raises(github_api.GitHubRateLimited, match="GitHub 接口次数用光") as error:
        github_api.GitHubApi().request(send)
    assert error.value.status == 403 and error.value.category == "rate_limited"
    assert error.value.request_id == "AB12:12AB"
    assert "20:00:04" in str(error.value) and TOKEN not in str(error.value)
    assert calls == [1] and waits == []


def test_retry_after_and_ordinary_permission_failures_are_distinct(clock):
    waits, _ = clock
    api = github_api.GitHubApi()
    forbidden = response(
        403,
        body={"message": f"Resource not accessible by integration {TOKEN}"},
        **{"X-RateLimit-Remaining": "99", "X-RateLimit-Reset": str(NOW + 60)},
    )
    assert api.request(lambda: forbidden) is forbidden
    assert github_api.diagnose(forbidden).category == "permission_denied"
    throttled = response(429, **{"Retry-After": "2", "X-RateLimit-Remaining": "99"})
    pages = iter([throttled, response()])
    assert api.request(lambda: next(pages)).status_code == 200 and waits == [3]


def test_persistent_throttle_returns_to_caller_after_one_wait(clock):
    waits, _ = clock
    calls = []

    def send():
        calls.append(1)
        return response(429, **{"Retry-After": "2"})

    with pytest.raises(github_api.GitHubRateLimited):
        github_api.GitHubApi().request(send)
    assert waits == [3] and calls == [1, 1]


def test_release_reserve_keeps_core_calls_available_for_digest(clock, monkeypatch, caplog):
    waits, root = clock
    monkeypatch.setenv("ZUCAI_GITHUB_RESERVE", "25")
    monkeypatch.setenv("ZUCAI_GITHUB_DEADLINE", str(NOW + 7200))
    remaining = response(
        200, **{"X-RateLimit-Remaining": "25", "X-RateLimit-Reset": str(NOW + 3600)}
    )
    github_api.GitHubApi().request(lambda: remaining, reserve=True)
    # A separate Release client must also see the earlier observation for this job.
    with pytest.raises(github_api.GitHubRateLimited, match="预留"):
        github_api.GitHubApi().request(lambda: pytest.fail("reserve leaked"), reserve=True)
    assert not waits
    assert github_api.GitHubApi().request(lambda: response(201)).status_code == 201
    github_api.main(["summary"])
    assert "调用 2 次" in (root / "summary").read_text() and TOKEN not in caplog.text


def test_graphql_retries_rejected_mutation_but_not_partial_success(clock):
    waits, _ = clock
    api = github_api.GitHubApi()
    denied = response(
        200,
        body={"errors": [{"type": "RATE_LIMITED", "message": TOKEN}]},
        **{
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(NOW + 2),
            "X-RateLimit-Resource": "graphql",
        },
    )
    pages = iter([denied, response(body={"data": {"pinIssue": {"id": "1"}}})])
    assert api.request(lambda: next(pages), graphql=True).json()["data"]
    assert waits == [3]
    partial = response(
        200,
        body={"data": {"mutation": {"id": "1"}}, "errors": [{"type": "RATE_LIMITED"}]},
        **{"X-RateLimit-Remaining": "0"},
    )
    assert api.request(lambda: partial, graphql=True) is partial and waits == [3]


def test_explicit_permission_error_is_not_retried_even_with_empty_quota_header(clock):
    waits, _ = clock
    forbidden = response(
        403,
        body={"message": "Resource not accessible by integration"},
        **{"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(NOW + 4)},
    )
    api = github_api.GitHubApi()
    assert api.request(lambda: forbidden) is forbidden
    assert github_api.diagnose(forbidden).category == "permission_denied"
    assert waits == []
