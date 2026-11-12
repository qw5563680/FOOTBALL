#!/usr/bin/env python3
"""T57 offline CLI acceptance against a fixed, locally available data-branch snapshot."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

COMMIT = "b492f4d7c501fd8cdbd2694d760f2f13892f796c"
REF = "refs/remotes/origin/data/football-feeds"
HISTORY = "state/publication/forecast_history.json"
# Freeze only the execution clock, not any archived timestamps or program logic.
CLOCK = """
import sys
from datetime import datetime
import zucai.scoreboard.command as board
import zucai.daily_digest as daily
now = datetime.fromisoformat(sys.argv.pop(1))
class FixedClock(datetime):
    @classmethod
    def now(cls, tz=None): return now.astimezone(tz)
board.datetime = daily.datetime = FixedClock
kind = sys.argv.pop(1)
if kind == 'scoreboard':
    board.main()
else:
    raise SystemExit(daily.main(sys.argv[1:]))
"""


def hashes(root):
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".git" not in p.parts
    }


def git(root, *args):
    return subprocess.check_output(
        ["git", "-C", str(root), *args], stderr=subprocess.PIPE, text=True
    ).strip()


def dump(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--archive", type=Path, required=True)
    p.add_argument("--baseline-code", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    archive, output = args.archive.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    assert git(archive, "rev-parse", "HEAD") == COMMIT
    initial = hashes(archive)
    summary = json.loads((archive / "scoreboard/summary.json").read_bytes())
    at = summary["as_of_utc"]
    # All comparisons use the same clock/run id/input bytes in separate copies.
    clock = "2026-11-13T00:00:00+00:00"
    source = Path(__file__).resolve().parents[1]
    records = []

    def copy(name, origin=archive):
        dest = output / "work" / name
        shutil.copytree(origin, dest)
        return dest

    def execute(root, kind, label, code=source, ok=True, reason=None):
        before = hashes(root)
        argv = (
            ["run", "--archive", str(root), "--as-of", at]
            if kind == "scoreboard"
            else [
                "--archive",
                str(root),
                "--out",
                str(root / "daily"),
                "--run-id",
                "t57-acceptance",
            ]
        )
        env = {**os.environ, "PYTHONPATH": str(code / "src"), "GITHUB_STEP_SUMMARY": ""}
        env.pop("GITHUB_REPOSITORY", None)
        proc = subprocess.run(
            [sys.executable, "-c", CLOCK, clock, kind, *argv],
            capture_output=True,
            text=True,
            env=env,
        )
        log = output / "logs" / (label + ".log")
        log.parent.mkdir(exist_ok=True)
        log.write_text(proc.stdout + proc.stderr)
        after = hashes(root)
        row = dict(
            case=label,
            command=kind,
            exit_code=proc.returncode,
            unchanged=before == after,
            changed_files=[
                k for k in sorted(before.keys() | after.keys()) if before.get(k) != after.get(k)
            ],
            log=str(log.relative_to(output)),
        )
        records.append(row)
        dump(output / "command-results.json", records)
        assert (proc.returncode == 0) == ok, (label, proc.stderr[-1800:])
        if reason:
            assert reason in proc.stderr, (label, proc.stderr)
        if not ok:
            assert before == after, row
        return after

    def fail(root, case):
        if case == "missing_git":
            shutil.rmtree(root / ".git")
        elif case == "shallow":
            (root / ".git/shallow").write_text(COMMIT + "\n")
        else:
            git(root, "update-ref", "-d", REF)

    for case, reason in [
        ("missing_git", "非仓库"),
        ("shallow", "浅克隆"),
        ("missing_ref", "published_ref"),
    ]:
        for kind in ("scoreboard", "digest"):
            root = copy("empty-" + case + "-" + kind)
            fail(root, case)
            execute(root, kind, "empty-" + case + "-" + kind, ok=False, reason=reason)
            shutil.rmtree(root)

    baseline, current = copy("baseline"), copy("current")
    for kind in ("scoreboard", "digest"):
        execute(baseline, kind, "baseline-" + kind, code=args.baseline_code)
        execute(current, kind, "normal-" + kind)
    base_hash, new_hash = hashes(baseline), hashes(current)
    added = set(new_hash) - set(base_hash)
    assert added == {HISTORY, "state/publication/sfc_history.json"}, added
    assert all(new_hash[k] == v for k, v in base_hash.items())
    unchanged = {k: v for k, v in initial.items() if not k.startswith(("scoreboard/", "daily/"))}
    assert all(new_hash[k] == v for k, v in unchanged.items())
    # T55 may append newly eligible scores; all original score bytes must survive.
    assert (
        (current / "scoreboard/scored.jsonl")
        .read_bytes()
        .startswith((archive / "scoreboard/scored.jsonl").read_bytes())
    )
    from zucai.parlay.paper import LEDGER

    for name in ("scoreboard/ledger.jsonl", LEDGER):
        assert new_hash[name] == initial[name]
    dump(output / "original-sha256.json", initial)
    dump(output / "normal-sha256.json", new_hash)
    dump(output / "baseline-sha256.json", base_hash)
    for label, root in (("baseline", baseline), ("normal", current)):
        names = [
            "scoreboard/summary.json",
            "scoreboard/scoreboard.md",
            "daily/latest.md",
            *sorted(added),
        ]
        for name in names:
            if not (root / name).exists():
                continue
            target = output / "outputs" / label / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / name, target)
    for kind in ("scoreboard", "digest"):
        execute(baseline, kind, "baseline-repeat-" + kind, code=args.baseline_code)
        execute(current, kind, "repeat-" + kind)
    repeated = hashes(current)
    assert all(repeated[k] == v for k, v in hashes(baseline).items())
    assert all(repeated[k] == new_hash[k] for k in added)
    new_hash = repeated
    for case, reason in [
        ("missing_git", "非仓库"),
        ("shallow", "浅克隆"),
        ("missing_ref", "published_ref"),
    ]:
        root = copy("cached-" + case, current)
        fail(root, case)
        for kind in ("scoreboard", "digest"):
            execute(root, kind, "cached-" + case + "-" + kind)
            assert hashes(root) == new_hash
        doc = json.loads((root / HISTORY).read_bytes())
        name = next(iter(doc["entries"]))
        doc["entries"][name]["candidate_sha256"] = "0" * 64
        dump(root / HISTORY, doc)
        for kind in ("scoreboard", "digest"):
            execute(root, kind, "tampered-" + case + "-" + kind, ok=False, reason=reason)
        shutil.rmtree(root)

    # Same T55 isolated per-batch unknown injection in old and new code.
    unknown_roots = []
    for label, code in [("old", args.baseline_code), ("new", source)]:
        root = copy("unknown-" + label)
        manifest = json.loads((root / "forecast_manifest.json").read_bytes())
        for run in manifest["runs"].values():
            for entry in run["forecasts"].values():
                if entry["batch_id"] == "41509202754-1-2026-11-03":
                    entry["publication_required"] = True
                    for prefix in ("", "experimental_", "parlay_"):
                        entry.pop(prefix + "publication_evidence", None)
                        entry.pop(prefix + "published_utc", None)
        dump(root / "forecast_manifest.json", manifest)
        for kind in ("scoreboard", "digest"):
            execute(root, kind, "unknown-" + label + "-" + kind, code=code)
        unknown_roots.append(root)
    old, new = map(hashes, unknown_roots)
    assert all(new[k] == v for k, v in old.items())
    unknown_summary = json.loads((unknown_roots[1] / "scoreboard/summary.json").read_bytes())

    from datetime import date

    from zucai.cloud_feeds import export_feeds, restore_feeds
    from zucai.forecast_archive import verify_forecasts
    from zucai.forecast_publication import publication_times
    from zucai.sfc_publication import resolve_publications
    from zucai.sfc_reports import read_reports

    db = output / "work/restored/data.db"
    restore_feeds(current, db)
    meta = export_feeds(
        db, current, date.fromisoformat(summary["as_of_utc"][:10]), "t57-offline-roundtrip"
    )
    assert added.issubset(meta["files"])
    restore_feeds(current, db)
    for name in added:
        assert (db.parent / name).read_bytes() == (current / name).read_bytes()
    # Restore state into a no-Git full archive copy: real lookup must still match.
    restored = copy("restored-view", current)
    shutil.rmtree(restored / ".git")
    for name in added:
        shutil.copyfile(db.parent / name, restored / name)
    fm = verify_forecasts(current)
    assert publication_times(restored, fm) == publication_times(current, fm)
    assert resolve_publications(
        restored, read_reports(restored, include_unconfirmed=True)
    ) == resolve_publications(current, read_reports(current, include_unconfirmed=True))
    # Raw state tampering must be caught by the feeds manifest before DB replacement.
    target_hash = hashlib.sha256(db.read_bytes()).hexdigest()
    (current / HISTORY).write_bytes((current / HISTORY).read_bytes() + b" ")
    try:
        restore_feeds(current, db)
    except ValueError as exc:
        assert "校验" in str(exc)
    else:
        raise AssertionError("corrupt state was accepted")
    assert hashlib.sha256(db.read_bytes()).hexdigest() == target_hash
    assert hashes(archive) == initial
    cache_counts = {key: len(json.loads((restored / key).read_bytes())["entries"]) for key in added}
    normal_summary = json.loads((baseline / "scoreboard/summary.json").read_bytes())
    result = dict(
        archive_commit=COMMIT,
        archive_files=len(initial),
        clock=clock,
        cutoff=at,
        commands=len(records),
        failure_commands=sum(r["exit_code"] != 0 for r in records),
        history_entries=cache_counts,
        normal_probability_n=normal_summary["probability"]["total"]["n"],
        normal_outputs_byte_identical=True,
        cached_fault_outputs_byte_identical=True,
        repeat_history_byte_identical=True,
        repeat_outputs_match_baseline=True,
        originals_unchanged=True,
        restore_manifest_verified=True,
        tamper_restore_rejected=True,
        unknown_probability_n=unknown_summary["probability"]["total"]["n"],
        unknown_audit=unknown_summary["audit"]["excluded"],
    )
    dump(output / "summary.json", result)
    # Keep result files and log evidence, not bulky or credential-bearing .git directories.
    shutil.rmtree(output / "work")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
