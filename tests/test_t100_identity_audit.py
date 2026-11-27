"""Focused regressions for the read-only archive inventory."""

import hashlib
import json
import runpy
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def audit():
    return runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "examples/t100_identity_audit.py")
    )


@pytest.fixture()
def archive(tmp_path, audit):
    (tmp_path / "state/audit").mkdir(parents=True)
    instants = {
        "previous_season": "2026-07-31T23:59:59Z",
        "current_season": "2026-08-01T00:00:00+00:00",
        "at_cutoff": "2026-08-02T00:00:00Z",
        "later": "2026-08-02T00:00:01+00:00",
    }
    matches = [
        [f"apif:{key}", "apif", "日职", 1, 2, timestamp, None, "finished"]
        for key, timestamp in instants.items()
    ]
    inputs = {
        "state/teams.jsonl": [(1, "甲", "[]"), (2, "乙", "[]")],
        "state/matches.jsonl": matches,
        "state/results.jsonl": [(row[0],) for row in matches],
    }
    for name, rows in inputs.items():
        (tmp_path / name).write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
    for provider in ("apif", "fdorg"):
        (tmp_path / f"state/audit/{provider}_identity_merges.csv").write_text(
            "reason\n", encoding="utf-8"
        )
    manifest = {
        "created_utc": "2026-08-02T00:00:00+00:00",
        "files": {
            name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
            for name in audit["ARCHIVE_FILES"]
        },
    }
    (tmp_path / "feed_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path


def test_hash_mismatch_rejects_changed_archive(archive, audit):
    with (archive / "state/teams.jsonl").open("a", encoding="utf-8") as stream:
        stream.write("\n")
    with pytest.raises(ValueError, match=r"归档 SHA256 不一致：state/teams\.jsonl"):
        audit["_read_audit"](archive)


def test_utc_season_boundary_and_archive_cutoff(archive, audit):
    _, _, windows, roster, _, _, historical, _ = audit["_read_audit"](archive)
    assert windows["日职"] == ["2024", "2025", "2026T", "2627"]
    assert roster["日职", "2026T"][1] == {"apif:previous_season"}
    assert roster["日职", "2627"][1] == {"apif:current_season"}
    assert set(historical["日职", 1]) == {"apif:previous_season", "apif:current_season"}


def test_alias_evidence_keeps_unverified_and_ambiguous_names_separate(audit):
    teams = {
        1: ("Town", []),
        2: ("Town FC", []),
        3: ("城市", ["Town"]),
        4: ("城镇", ["Town"]),
    }
    sources = {(league, i): {"fd"} for league, i in [("英冠", 1), ("英超", 2), ("英超", 3)]}
    alias_index = audit["_alias_index"]
    assert alias_index(("英超", "英冠"), teams, sources, [], {}) == {1: 3}
    sources["英超", 4] = {"fd"}
    ambiguous = alias_index(("英超", "英冠"), teams, sources, [], {})
    assert ambiguous == {}
    assert audit["_canonical_roster"]({1: {"fd:lower"}, 2: {"fd:upper"}}, ambiguous) == {
        1: {1: {"fd:lower"}},
        2: {2: {"fd:upper"}},
    }
