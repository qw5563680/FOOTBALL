"""Offline data-branch publication evidence using temporary local Git repositories."""

import os
import subprocess

REF = "refs/remotes/origin/data/football-feeds"


def git(root, *args, at=None):
    env = dict(os.environ)
    if at:
        env.update(GIT_AUTHOR_DATE=at, GIT_COMMITTER_DATE=at)
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout.strip()


def prepare_remote(archive, at="2026-11-01T15:00:00+00:00"):
    remote = archive.parent / (archive.name + "-remote.git")
    git(archive.parent, "init", "--bare", str(remote))
    git(archive, "init", "-b", "data/football-feeds")
    git(archive, "config", "user.name", "Offline fixture")
    git(archive, "config", "user.email", "offline@example.invalid")
    git(archive, "remote", "add", "origin", str(remote))
    commit_report(archive, at)
    return remote


def commit_report(archive, at, *, push=True):
    git(archive, "add", ".")
    git(archive, "commit", "--allow-empty", "-m", "Offline publication fixture", at=at)
    commit = git(archive, "rev-parse", "HEAD")
    if push:
        git(archive, "push", "origin", "HEAD:refs/heads/data/football-feeds")
        git(archive, "fetch", "origin", "refs/heads/data/football-feeds:" + REF)
    return commit


def confirm_report(archive, at="2026-11-01T15:01:00+00:00"):
    from zucai.sfc_reports import confirm_publication

    prepare_remote(archive, at)
    return confirm_publication(archive, published_ref=REF)
