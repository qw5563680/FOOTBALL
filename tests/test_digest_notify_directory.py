"""Offline monthly Issue directory replays with frozen daily reports."""

import hashlib
import json
from datetime import UTC, date, datetime

from test_digest_notify import FIXTURE
from zucai.digest_notify import OVERVIEW_END, OVERVIEW_START, monthly_issue_body

REPO = FIXTURE["repository"]
TODAY = date(2026, 11, 25)
ISSUE_URL = f"https://github.com/{REPO}/issues/101"
ARCHIVE_URL = f"https://github.com/{REPO}/blob/data/football-feeds/daily"


def freeze(root, day, version, hour):
    directory = root / day.isoformat()
    directory.mkdir(parents=True, exist_ok=True)
    body = f"非购买建议 · BET 禁用\n<!-- zucai-daily:{day} -->\n日报版本 {version}\n"
    metadata = {
        "schema_version": 1,
        "date": day.isoformat(),
        "version_id": version,
        "generated_utc": datetime(2026, 11, day.day, hour, tzinfo=UTC).isoformat(),
        "pending": {},
        "markdown_sha256": hashlib.sha256(body.encode()).hexdigest(),
    }
    (directory / f"{version}.md").write_text(body, encoding="utf-8")
    (directory / f"{version}.json").write_text(json.dumps(metadata), encoding="utf-8")
    return body


def directory_rows(body):
    managed = body.split(OVERVIEW_START, 1)[1].split(OVERVIEW_END, 1)[0]
    index = managed.split("**最新日报 ", 1)[0]
    return [line for line in index.splitlines() if line.startswith("- ")]


def test_rewrites_month_of_archive_versions_as_25_robot_comment_links(tmp_path):
    comments, old_rows = [], []
    for n in range(1, 26):
        day = date(2026, 11, n)
        for hour in (2, 6, 10):
            version = f"run-{n}-{hour}"
            body = freeze(tmp_path, day, version, hour)
            old_rows.append(f"- [{day} · {version}]({ARCHIVE_URL}/{day}/{version}.md)")
        comments.append({**FIXTURE["comment"], "id": 200 + n, "body": body})
    before, after = "保留前面的人工说明\n", "\n保留后面的人工说明"
    issue = {
        **FIXTURE["issue"],
        "body": before + OVERVIEW_START + "\n" + "\n".join(old_rows) + "\n" + OVERVIEW_END + after,
    }

    result = monthly_issue_body(issue, comments, REPO, TODAY, tmp_path)

    assert len(directory_rows(issue["body"])) == 75
    assert directory_rows(result) == [
        f"- [2026-11-{n:02d}]({ISSUE_URL}#issuecomment-{200 + n})" for n in range(25, 0, -1)
    ]
    assert result.startswith(before) and result.endswith(after)
    assert comments[-1]["body"] in result


def test_day_without_comment_links_to_latest_frozen_version(tmp_path):
    freeze(tmp_path, TODAY, "z-earlier", 2)
    latest = freeze(tmp_path, TODAY, "a-latest", 10)
    human = {**FIXTURE["comment"], "body": latest, "user": {"login": "owner"}}

    result = monthly_issue_body(FIXTURE["issue"], [human], REPO, TODAY, tmp_path)

    assert directory_rows(result) == [f"- [2026-11-25]({ARCHIVE_URL}/2026-11-25/a-latest.md)"]
    assert latest in result


def test_next_shift_updates_embedded_report_without_adding_directory_row(tmp_path):
    first = freeze(tmp_path, TODAY, "earlier", 2)
    comment = {**FIXTURE["comment"], "body": first}
    before = monthly_issue_body(FIXTURE["issue"], [comment], REPO, TODAY, tmp_path)
    newest = freeze(tmp_path, TODAY, "later", 10)

    updated_issue = {**FIXTURE["issue"], "body": before}
    after = monthly_issue_body(updated_issue, [{**comment, "body": newest}], REPO, TODAY, tmp_path)
    after_redelivery = monthly_issue_body(
        {**updated_issue, "body": after}, [{**comment, "body": newest}], REPO, TODAY, tmp_path
    )

    assert (
        directory_rows(before)
        == directory_rows(after)
        == [f"- [2026-11-25]({ISSUE_URL}#issuecomment-201)"]
    )
    assert first in before and newest in after and first not in after
    assert after_redelivery == after


def test_unavailable_comments_keep_previous_day_links_and_add_latest_archive(tmp_path):
    known_comment = f"- [2026-11-24]({ISSUE_URL}#issuecomment-224)"
    known_archive = f"- [2026-11-23]({ARCHIVE_URL}/2026-11-23/archived.md)"
    legacy_newest = f"{ARCHIVE_URL}/2026-11-22/newest.md"
    issue = {
        **FIXTURE["issue"],
        "body": f"{OVERVIEW_START}\n{known_comment}\n{known_archive}\n"
        f"- [2026-11-22 · newest]({legacy_newest})\n"
        f"- [2026-11-22 · earlier]({ARCHIVE_URL}/2026-11-22/earlier.md)\n"
        f"**最新日报 2026-11-24**\n\n{OVERVIEW_END}",
    }
    freeze(tmp_path, date(2026, 11, 24), "another-archive", 10)
    latest = freeze(tmp_path, TODAY, "today", 10)

    result = monthly_issue_body(issue, None, REPO, TODAY, tmp_path)

    assert directory_rows(result) == [
        f"- [2026-11-25]({ARCHIVE_URL}/2026-11-25/today.md)",
        known_comment,
        known_archive,
        f"- [2026-11-22]({legacy_newest})",
    ]
    assert latest in result
