"""A reviewed snapshot must not be rebased over an unseen remote data update."""

import subprocess
from pathlib import Path

import pytest

from test_publish_script import execute, git
from test_publish_script import publication as publication


@pytest.mark.parametrize("when", ["before_fetch", "during_push"])
def test_expected_base_rejects_remote_advance_even_without_merge_conflict(publication, when):
    remote, other, _, summary, env = publication
    original = git(remote, "rev-parse", "data/football-feeds")
    env["PUBLISH_EXPECTED_BASE"] = original
    (other / "field-notes.txt").write_text("changed since plan was checked\n")
    git(other, "add", ".")
    git(other, "commit", "-m", "new remote snapshot")
    changed = git(other, "rev-parse", "HEAD")
    if when == "before_fetch":
        subprocess.run(
            [env["TEST_REAL_GIT"], "-C", str(other), "push", "origin", "HEAD"],
            env=env,
            check=True,
            capture_output=True,
        )
    else:
        env["TEST_OTHER_WRITER"] = str(other)
    result = execute(publication)
    assert result.returncode != 0
    assert git(remote, "rev-parse", "data/football-feeds") == changed
    assert git(remote, "show", "data/football-feeds:state/teams.jsonl") == "original"
    assert "核验基线" in summary.read_text()
    commands = Path(env["TEST_GIT_LOG"]).read_text().splitlines()
    assert sum(c.startswith("push ") for c in commands) == (0 if when == "before_fetch" else 1)
    assert not any(c.startswith("rebase ") for c in commands)


def test_expected_base_allows_the_same_snapshot(publication):
    remote, _, _, _, env = publication
    env["PUBLISH_EXPECTED_BASE"] = git(remote, "rev-parse", "data/football-feeds")
    result = execute(publication)
    assert result.returncode == 0, result.stderr
    assert git(remote, "show", "data/football-feeds:state/teams.jsonl") == "new observation"


def test_review_publish_does_not_migrate_unrelated_archive_attributes(publication):
    remote, seed, run, _, env = publication
    queue = "state/alias_candidates/jc.json"
    for name in (
        queue,
        "forecast_manifest.json",
        "models/old/model.json",
        "forecasts/old/forecast.json",
        "scoreboard/ledger.jsonl",
        "daily/old.md",
        "sfc/old.json",
    ):
        path = seed / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"old archive bytes\n")
    (seed / ".gitattributes").write_text("state/** -text\n")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "legacy archive with existing records")
    subprocess.run(
        [env["TEST_REAL_GIT"], "-C", str(seed), "push", "origin", "HEAD"],
        env=env,
        check=True,
        capture_output=True,
    )
    git(run, "restore", "state/teams.jsonl")
    git(run, "fetch", "origin")
    git(run, "merge", "--ff-only", "origin/data/football-feeds")
    original = git(run, "rev-parse", "HEAD")
    env["PUBLISH_EXPECTED_BASE"] = original
    (run / queue).write_bytes(b"reviewed queue\n")

    result = execute(publication)

    assert result.returncode == 0, result.stderr
    assert git(remote, "diff", "--name-only", original, "data/football-feeds").splitlines() == [
        queue
    ]
