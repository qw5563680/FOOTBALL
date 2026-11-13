"""Offline diagnostic plugin: reject any Git read after the shallow marker exists."""

import json
import platform
import subprocess
from pathlib import Path

import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    if item.name != "test_t57_sfc_history_is_saved_transactionally_and_reused[shallow]":
        yield
        return
    original = subprocess.run
    calls = []

    def traced_run(args, *positional, **kwargs):
        if isinstance(args, (tuple, list)) and args[:2] == ["git", "-C"]:
            root = Path(args[2])
            shallow = (root / ".git/shallow").exists()
            call = {"args": list(args), "shallow_marker_exists": shallow}
            dates = kwargs.get("env", {})
            call["author_date"] = dates.get("GIT_AUTHOR_DATE")
            call["committer_date"] = dates.get("GIT_COMMITTER_DATE")
            calls.append(call)
            assert not shallow, "No Git calls should be needed after saved history is available"
        return original(args, *positional, **kwargs)

    subprocess.run = traced_run
    try:
        outcome = yield
    finally:
        subprocess.run = original
        archive = item.funcargs["publication_archive"]
        history = json.loads((archive / "state/publication/sfc_history.json").read_bytes())
        doc = {
            "python": platform.python_version(),
            "nodeid": item.nodeid,
            "git_calls": calls,
            "git_calls_after_shallow_marker": sum(c["shallow_marker_exists"] for c in calls),
            "shallow_marker": (archive / ".git/shallow").read_text().strip(),
            "saved_history": history,
        }
        Path(__file__).with_name("trace-result.json").write_text(json.dumps(doc, indent=2) + "\n")
