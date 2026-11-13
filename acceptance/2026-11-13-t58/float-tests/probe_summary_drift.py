"""Offline repeat-score probe using the repository's synthetic archive fixture."""
import json
import platform
import tempfile
from pathlib import Path
from types import SimpleNamespace

from test_scoreboard_command import scoreboard_archive
from test_scoreboard_score import NOW
from zucai.scoreboard.command import run


def differences(first, second, path="probability"):
    if isinstance(first, dict):
        return [
            diff
            for key in first
            for diff in differences(first[key], second[key], f"{path}.{key}")
        ]
    if isinstance(first, list):
        return [
            diff
            for index, (a, b) in enumerate(zip(first, second, strict=True))
            for diff in differences(a, b, f"{path}[{index}]")
        ]
    if first != second:
        return [{"path": path, "first": first, "second": second,
                 "absolute_delta": abs(first - second)}]
    return []


with tempfile.TemporaryDirectory(prefix="t58-float-probe-") as directory:
    archive = scoreboard_archive.__wrapped__(Path(directory), SimpleNamespace())
    first = run(archive, as_of=NOW)
    second = run(archive, as_of=NOW)
    print(json.dumps({
        "python": platform.python_version(),
        "differences": differences(first["probability"], second["probability"]),
    }, indent=2))
