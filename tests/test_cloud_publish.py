"""Exercise the workflow's actual writeback script using a local Git remote."""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/publish-football-feeds.sh"


def git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.mark.parametrize("conflict", [False, True])
def test_writeback_rebases_or_fails_without_overwriting_remote(tmp_path, conflict):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    base = tmp_path / "seed"
    git(tmp_path, "clone", str(remote), str(base))
    git(base, "config", "user.name", "Test")
    git(base, "config", "user.email", "test@example.invalid")
    git(base, "checkout", "-b", "data/football-feeds")
    (base / "state").mkdir()
    (base / "matches").mkdir()
    (base / "state/teams.jsonl").write_text("original\n")
    (base / "matches/2026-11.csv").write_text("original\n")
    (base / "feed_manifest.json").write_text("{}\n")
    git(base, "add", ".")
    git(base, "commit", "-m", "seed")
    git(base, "push", "origin", "HEAD")
    run = tmp_path / "run"
    git(tmp_path, "clone", "--branch", "data/football-feeds", str(remote), str(run))
    (run / "state/teams.jsonl").write_text("local observation\n")
    (base / ("state/teams.jsonl" if conflict else "field-notes.txt")).write_text(
        "other observation\n"
    )
    git(base, "add", ".")
    git(base, "commit", "-m", "other writer")
    git(base, "push", "origin", "HEAD")
    before = git(remote, "rev-parse", "refs/heads/data/football-feeds")
    env = {
        **os.environ,
        "GITHUB_RUN_ID": "offline",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
    }
    result = subprocess.run(["bash", str(SCRIPT)], cwd=run, env=env, capture_output=True, text=True)
    if conflict:
        assert result.returncode != 0
        assert git(remote, "rev-parse", "refs/heads/data/football-feeds") == before
        summary = (tmp_path / "summary.md").read_text()
        assert "变基 1/3 失败" in summary
        assert "发布未确认成功" in summary
        assert "推送 1/3 成功" not in summary
    else:
        assert result.returncode == 0, result.stderr
        assert git(remote, "show", "data/football-feeds:field-notes.txt") == "other observation"
        assert git(remote, "show", "data/football-feeds:state/teams.jsonl") == "local observation"
        assert git(remote, "rev-parse", "data/football-feeds^") == before
        assert (
            git(remote, "rev-parse", "data/football-feeds") in (tmp_path / "summary.md").read_text()
        )


def test_sporttery_entrypoint_imports_without_unix_quota_lock_module():
    import sys

    # Windows has no fcntl. Sporttery/verify must not import the Linux API quota code.
    result = subprocess.run(
        [sys.executable, "-c", "import sys; sys.modules['fcntl']=None; import zucai.cloud_feeds"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_writeback_publishes_forecasts_with_exact_bytes_on_windows_checkouts(tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    run = tmp_path / "run"
    git(tmp_path, "clone", str(remote), str(run))
    git(run, "checkout", "-b", "data/football-feeds")
    git(run, "config", "user.name", "Test")
    git(run, "config", "user.email", "test@example.invalid")
    git(run, "config", "core.autocrlf", "true")
    for name in ("state/teams.jsonl", "matches/2026-11.csv", "feed_manifest.json"):
        path = run / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{}\n")
    git(run, "add", ".")
    git(run, "commit", "-m", "seed")
    git(run, "push", "origin", "HEAD")
    artifacts = {
        "forecasts/2026-11-03/offline/report.csv": b"match_id,p\r\njc:1,0.4\r\n",
        "models/offline/model.pkl": b"opaque\x00model\r\n",
        "models/latest.json": b"{}\n",
        "forecast_manifest.json": b"{}\n",
        "scoreboard/scored.jsonl": b'{"match_id":"jc:1"}\n',
        "scoreboard/ledger.jsonl": b'{"stake":100}\n',
        "scoreboard/summary.json": b"{}\n",
        "scoreboard/scoreboard.md": b"virtual only\n",
        "daily/2026-11-03.md": b"daily fixture\n",
        "daily/latest.md": b"daily fixture\n",
        "sfc/26156/report.csv": b"issue,seq\r\n26156,1\r\n",
        "sfc/settled.jsonl": b"{}\n",
    }
    for name, raw in artifacts.items():
        path = run / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    env = {**os.environ, "GITHUB_RUN_ID": "offline", "GITHUB_RUN_ATTEMPT": "1"}
    result = subprocess.run(["bash", str(SCRIPT)], cwd=run, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    for name, raw in artifacts.items():
        committed = subprocess.run(
            ["git", "-C", str(remote), "show", f"data/football-feeds:{name}"],
            check=True,
            capture_output=True,
        ).stdout
        assert committed == raw
    restored = tmp_path / "restored"
    git(
        tmp_path,
        "-c",
        "core.autocrlf=true",
        "clone",
        "--branch",
        "data/football-feeds",
        str(remote),
        str(restored),
    )
    assert all((restored / name).read_bytes() == raw for name, raw in artifacts.items())
    git(restored, "config", "core.autocrlf", "true")
    restored_head = git(restored, "rev-parse", "HEAD")
    result = subprocess.run(
        ["bash", str(SCRIPT)], cwd=restored, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert git(restored, "rev-parse", "HEAD") == restored_head
    before = git(run, "rev-parse", "HEAD")
    result = subprocess.run(["bash", str(SCRIPT)], cwd=run, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert git(run, "rev-parse", "HEAD") == before
