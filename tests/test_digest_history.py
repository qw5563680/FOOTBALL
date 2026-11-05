"""Immutable daily versions distinguish local renders from successful Issue delivery."""

import hashlib
import json
from datetime import UTC, date, datetime, timedelta

import pytest

from zucai.digest_history import version_context

NOW = datetime(2026, 11, 3, 10, tzinfo=UTC)
TODAY = date(2026, 11, 3)
REPOSITORY = "qw5563680/FOOTBALL"
INPUTS = {"repository": REPOSITORY, "require_notified_history": True}


def render_version(root, name, *, now=NOW, update=1, body=None, notified=False):
    body = body or f"非购买建议 · BET 禁用\n<!-- zucai-daily:{TODAY} -->\n{name}\n"
    directory = root / str(TODAY)
    directory.mkdir(parents=True, exist_ok=True)
    value = {
        "schema_version": 1,
        "date": str(TODAY),
        "run_id": name,
        "version_id": name,
        "generated_utc": now.isoformat(),
        "update_number": update,
        "inputs": INPUTS,
        "pending": {"A": []},
        "markdown_sha256": hashlib.sha256(body.encode()).hexdigest(),
    }
    (directory / f"{name}.md").write_text(body, encoding="utf-8")
    (directory / f"{name}.json").write_text(json.dumps(value), encoding="utf-8")
    if notified:
        receipt = {
            "schema_version": 1,
            "status": "published",
            "repository": REPOSITORY,
            "issue_number": 101,
            "comment_id": 201,
            "date": str(TODAY),
            "run_id": name,
            "version_id": name,
            "markdown_sha256": value["markdown_sha256"],
            "notified_utc": (now + timedelta(seconds=1)).isoformat(),
        }
        (directory / f"{name}.notification.json").write_text(json.dumps(receipt))
    return value, body


def test_actions_previous_ignores_unnotified_renders_but_local_replay_can_use_them(tmp_path):
    archive, out = tmp_path / "archive", tmp_path / "daily"
    first, _ = render_version(out, "first", notified=True)
    second, _ = render_version(out, "unsent", now=NOW + timedelta(minutes=1), update=2)
    now = NOW + timedelta(minutes=2)
    previous, update, name = version_context(
        archive, out, TODAY, now, "next", INPUTS, require_notified=True
    )
    assert previous == first and update == 2 and name == "next"
    previous, update, _ = version_context(archive, out, TODAY, now, "offline", INPUTS)
    assert previous == second and update == 3


def test_unnotified_same_version_is_reused_without_becoming_its_own_previous(tmp_path):
    archive, out = tmp_path / "archive", tmp_path / "daily"
    render_version(out, "unsent")
    assert version_context(archive, out, TODAY, NOW, "unsent", INPUTS, require_notified=True) == (
        None,
        1,
        "unsent",
    )


def test_success_receipt_must_be_visible_by_this_digest_time(tmp_path):
    archive, out = tmp_path / "archive", tmp_path / "daily"
    render_version(out, "just-rendered", notified=True)
    assert version_context(archive, out, TODAY, NOW, "next", INPUTS, require_notified=True) == (
        None,
        1,
        "next",
    )


@pytest.mark.parametrize("changed", ["markdown_sha256", "version_id", "comment_id"])
def test_mismatched_notification_receipt_is_never_accepted(tmp_path, changed):
    archive, out = tmp_path / "archive", tmp_path / "daily"
    render_version(out, "first", notified=True)
    path = out / str(TODAY) / "first.notification.json"
    receipt = json.loads(path.read_bytes())
    receipt[changed] = "wrong" if changed != "comment_id" else 0
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="回执"):
        version_context(
            archive,
            out,
            TODAY,
            NOW + timedelta(minutes=1),
            "next",
            INPUTS,
            require_notified=True,
        )


def test_time_regression_cannot_rewind_existing_daily_pointers(tmp_path):
    archive, out = tmp_path / "archive", tmp_path / "daily"
    render_version(out, "early")
    render_version(out, "late", now=NOW + timedelta(minutes=10), update=2)
    with pytest.raises(ValueError, match="不可倒退已有日报版本"):
        version_context(archive, out, TODAY, NOW, "early", INPUTS)
