"""Run the publication shell script against local bare repositories and refusal hooks."""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
FIX = ROOT / "data/fixtures/t32"
ERRORS = json.loads((FIX / "publish_errors.json").read_text())
SCRIPT = ROOT / "scripts/publish-football-feeds.sh"


def git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def publication(tmp_path):
    remote, seed, run = (tmp_path / name for name in ("remote.git", "seed", "run"))
    git(tmp_path, "init", "--bare", str(remote))
    git(tmp_path, "clone", str(remote), str(seed))
    git(seed, "config", "user.name", "Offline Test")
    git(seed, "config", "user.email", "test@example.invalid")
    git(seed, "checkout", "-b", "data/football-feeds")
    for name in ("state/teams.jsonl", "matches/2026-11.csv", "feed_manifest.json"):
        path = seed / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("original\n")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "seed")
    git(seed, "push", "origin", "HEAD")
    git(tmp_path, "clone", "--branch", "data/football-feeds", str(remote), str(run))
    (run / "state/teams.jsonl").write_text("new observation\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, sample in (("sleep", "publish_sleep.sh"), ("git", "publish_git_wrapper.sh")):
        dest = bin_dir / name
        shutil.copyfile(FIX / sample, dest)
        dest.chmod(0o755)
    hook = remote / "hooks/pre-receive"
    shutil.copyfile(FIX / "publish_pre_receive.sh", hook)
    hook.chmod(0o755)
    summary = tmp_path / "summary.md"
    env = {
        **os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "GITHUB_RUN_ID": "offline",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_STEP_SUMMARY": str(summary),
        "TEST_PUSH_FAILURES": "0",
        "TEST_PUSH_MESSAGE": "unused",
        "TEST_REAL_GIT": shutil.which("git"),
        "TEST_GIT_LOG": str(tmp_path / "git.log"),
        "TEST_SLEEP_LOG": str(tmp_path / "sleep.log"),
        "TEST_RACE_MARKER": str(tmp_path / "race"),
    }
    return remote, seed, run, summary, env


def execute(publication):
    _, _, run, _, env = publication
    return subprocess.run(
        ["bash", str(SCRIPT)], cwd=run, env=env, capture_output=True, text=True, timeout=20
    )


@pytest.mark.parametrize("message", ERRORS["transient"])
def test_two_transient_refusals_then_one_commit_is_published(publication, message):
    remote, _, _, summary, env = publication
    before = git(remote, "rev-parse", "data/football-feeds")
    env.update(TEST_PUSH_FAILURES="2", TEST_PUSH_MESSAGE=message)
    result = execute(publication)
    assert result.returncode == 0, result.stderr
    assert git(remote, "rev-parse", "data/football-feeds^") == before
    assert git(remote, "show", "data/football-feeds:state/teams.jsonl") == "new observation"
    assert (remote / "test-push-attempts").read_text().strip() == "3"
    assert Path(env["TEST_SLEEP_LOG"]).read_text().splitlines() == ["30", "30"]
    text = summary.read_text()
    assert "推送 1/3 失败" in text and "推送 2/3 失败" in text and "推送 3/3 成功" in text
    assert text.count(message) == 2
    assert "--force" not in Path(env["TEST_GIT_LOG"]).read_text()


def test_three_transient_failures_preserve_remote_and_report_artifact(publication):
    remote, _, _, summary, env = publication
    before = git(remote, "rev-parse", "data/football-feeds")
    env.update(TEST_PUSH_FAILURES="9", TEST_PUSH_MESSAGE=ERRORS["transient"][0])
    result = execute(publication)
    assert result.returncode != 0
    assert git(remote, "rev-parse", "data/football-feeds") == before
    assert (remote / "test-push-attempts").read_text().strip() == "3"
    assert Path(env["TEST_SLEEP_LOG"]).read_text().splitlines() == ["30", "30"]
    assert "发布未确认成功" in summary.read_text()
    assert "请核对上传结果" in summary.read_text()


@pytest.mark.parametrize("message", ERRORS["permanent"])
def test_non_transient_refusal_fails_immediately(publication, message):
    remote, _, _, summary, env = publication
    before = git(remote, "rev-parse", "data/football-feeds")
    env.update(TEST_PUSH_FAILURES="9", TEST_PUSH_MESSAGE=message)
    result = execute(publication)
    assert result.returncode != 0
    assert git(remote, "rev-parse", "data/football-feeds") == before
    assert (remote / "test-push-attempts").read_text().strip() == "1"
    assert not Path(env["TEST_SLEEP_LOG"]).exists()
    assert message in summary.read_text()


@pytest.mark.parametrize("conflict", [False, True])
def test_fetch_first_rebases_but_conflict_stops_before_another_push(publication, conflict):
    remote, other, _, summary, env = publication
    (other / ("state/teams.jsonl" if conflict else "field-notes.txt")).write_text("other writer\n")
    git(other, "add", ".")
    git(other, "commit", "-m", "other writer")
    before = git(other, "rev-parse", "HEAD")
    env["TEST_OTHER_WRITER"] = str(other)
    result = execute(publication)
    commands = Path(env["TEST_GIT_LOG"]).read_text().splitlines()
    pushes = [c for c in commands if c.startswith("push ")]
    assert len(pushes) == (1 if conflict else 2)
    assert len([c for c in commands if c.startswith("fetch origin ")]) == 2
    if conflict:
        assert result.returncode != 0
        assert git(remote, "rev-parse", "data/football-feeds") == before
    else:
        assert result.returncode == 0, result.stderr
        assert git(remote, "rev-parse", "data/football-feeds^") == before
        assert "推送 2/3 成功" in summary.read_text()
    assert "--force" not in "\n".join(commands)


@pytest.mark.parametrize("message", ERRORS["transient"])
def test_fetch_network_failure_retries_before_push(publication, message):
    remote, _, _, summary, env = publication
    before = git(remote, "rev-parse", "data/football-feeds")
    env.update(TEST_FETCH_FAILURES="2", TEST_FETCH_MESSAGE=message)
    result = execute(publication)
    assert result.returncode == 0, result.stderr
    assert git(remote, "rev-parse", "data/football-feeds^") == before
    assert (remote / "test-push-attempts").read_text().strip() == "1"
    assert Path(env["TEST_SLEEP_LOG"]).read_text().splitlines() == ["30", "30"]
    assert "拉取 1/3 失败" in summary.read_text()


@pytest.mark.parametrize("message", ERRORS["permanent"])
def test_fetch_permanent_error_does_not_retry(publication, message):
    remote, _, _, summary, env = publication
    env.update(TEST_FETCH_FAILURES="9", TEST_FETCH_MESSAGE=message)
    result = execute(publication)
    assert result.returncode != 0
    assert not (remote / "test-push-attempts").exists()
    assert not Path(env["TEST_SLEEP_LOG"]).exists()
    assert message in summary.read_text()


def test_exhausted_fetch_keeps_bundle_recoverable_after_checkout_removed(publication, tmp_path):
    remote, _, run, summary, env = publication
    recovery = tmp_path / "recovery"
    before = git(remote, "rev-parse", "data/football-feeds")
    # Frozen fixtures act as opaque files: publication must never rewrite a ledger or prediction.
    for name in ("scoreboard/ledger.jsonl", "forecasts/offline/forecast.json"):
        path = run / name
        path.parent.mkdir(parents=True, exist_ok=True)
        fixture = ROOT / "data/fixtures/t25" / path.name
        shutil.copyfile(fixture, path)
    expected = {
        p.relative_to(run): p.read_bytes()
        for p in run.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(run).parts
    }
    env.update(
        TEST_FETCH_FAILURES="9",
        TEST_FETCH_MESSAGE=ERRORS["transient"][-1],
        PUBLISH_RECOVERY_DIR=str(recovery),
    )
    result = execute(publication)
    assert result.returncode != 0
    assert git(remote, "rev-parse", "data/football-feeds") == before
    assert not (remote / "test-push-attempts").exists()
    assert Path(env["TEST_SLEEP_LOG"]).read_text().splitlines() == ["30", "30"]
    bundle = recovery / "unpublished.bundle"
    assert bundle.is_file()
    assert (recovery / "SHA256SUMS").read_text().split()[0] == hashlib.sha256(
        bundle.read_bytes()
    ).hexdigest()
    git(run, "bundle", "verify", str(bundle))
    collected = git(run, "rev-parse", "HEAD")
    shutil.rmtree(run)
    git(tmp_path, "clone", "--branch", "data/football-feeds", str(remote), str(run))
    git(run, "fetch", str(bundle), "HEAD")
    git(run, "merge", "--ff-only", "FETCH_HEAD")
    assert git(run, "rev-parse", "HEAD") == collected
    for path, raw in expected.items():
        assert (run / path).read_bytes() == raw
    env["TEST_FETCH_FAILURES"] = "0"
    assert execute(publication).returncode == 0
    published = git(remote, "rev-parse", "data/football-feeds")
    assert published == collected
    assert execute(publication).returncode == 0
    assert git(remote, "rev-parse", "data/football-feeds") == published
    for path, raw in expected.items():
        assert (run / path).read_bytes() == raw


def test_push_succeeded_but_response_lost_does_not_publish_twice(publication):
    remote, _, _, summary, env = publication
    before = git(remote, "rev-parse", "data/football-feeds")
    env["TEST_PUSH_ACK_LOST"] = "1"
    result = execute(publication)
    assert result.returncode == 0, result.stderr
    assert git(remote, "rev-parse", "data/football-feeds^") == before
    assert (remote / "test-push-attempts").read_text().strip() == "1"
    assert Path(env["TEST_SLEEP_LOG"]).read_text().splitlines() == ["30"]
    assert "推送 2/3 成功" in summary.read_text()
