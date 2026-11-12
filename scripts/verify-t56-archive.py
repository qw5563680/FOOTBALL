"""Compare T56 display with a fixed local Git baseline, without archive writes/network."""

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

ARCHIVE_COMMITS = {
    "fixed": "baac74d6975d01fa1a49fb9a1981d32102e20497",
    "nov6": "8bdacb05e1e4ec7d0e62c3e6e8477d49777ae6f8",
}


def raw(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n"


def hashes(root):
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def verify_git_snapshot(root, commit, repository):
    """Check every extracted file against the locally available immutable Git tree."""
    tree = subprocess.check_output(["git", "ls-tree", "-rz", "--full-tree", commit], cwd=repository)
    expected = {}
    for record in tree.split(b"\0"):
        if not record:
            continue
        identity, name = record.split(b"\t", 1)
        mode, kind, oid = identity.split()
        assert kind == b"blob" and mode in {b"100644", b"100755"}
        expected[name.decode()] = oid.decode()
    actual = {}
    for path in root.rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            actual[path.relative_to(root).as_posix()] = hashlib.sha1(
                f"blob {len(content)}\0".encode() + content
            ).hexdigest()
    assert actual == expected, f"Archive differs from fixed Git tree {commit}"


def worker(args):
    from zucai.daily_digest import generate
    from zucai.digest_display import _match, _text
    from zucai.parlay.paper import read_ledger, render, summarize
    from zucai.report.render import _csv_bytes, load_batch
    from zucai.report.sfc import _csv

    out = args.output
    out.mkdir(parents=True)
    ledger = read_ledger(args.archive)
    summary = summarize(ledger, {}, "2026-11-11T18:00:00+00:00")
    (out / "f-summary.json").write_bytes(raw(summary))
    (out / "f-summary.md").write_text(render(summary))
    generate(
        args.archive,
        out / "daily-fixed",
        now=datetime.fromisoformat("2026-11-11T18:00:00+00:00"),
        run_id="t56-offline-fixed",
    )
    path = generate(
        args.nov6_archive,
        out / "daily-nov6",
        now=datetime.fromisoformat("2026-11-05T16:05:00+00:00"),
        run_id="t56-offline-nov6",
    )
    text = path.read_text()
    table = text.split("### 今日 2026-11-06", 1)[1].split("### 明日", 1)[0]
    line = next(line for line in table.splitlines() if line.startswith("|") and "AC米兰" in line)
    (out / "nov6-single-table.md").write_text(table)
    (out / "ac-milan-row.txt").write_text(line + "\n")
    batch = load_batch(args.nov6_archive / "forecasts/2026-11-06/42024862563-1/forecast.json")
    display = []
    for row in batch["predictions"]:
        if row["status"] != "predicted":
            continue
        rendered = next(
            line for line in table.splitlines() if line.startswith("|") and _match(row) in line
        )
        display.append(
            dict(
                match_id=row["match_id"],
                model_name_inline=_text(row["model_name"]) in rendered,
                model_hash_in_note=row["model_version"] not in rendered
                and row["model_version"] in table,
                top_five_inline=all(
                    f"{score['score']} {score['probability']:.2%}" in rendered
                    for score in row["top_scores"][:5]
                ),
                expected_p2="可用" if row.get("p2_ready") is True else "实验",
                displayed_p2=rendered.split(" | ")[5].split("；", 1)[0],
            )
        )
    (out / "nov6-display.json").write_bytes(raw(display))
    (out / "single-report.csv").write_bytes(_csv_bytes(batch))
    sfc = json.loads((args.nov6_archive / "sfc/26159/sfc.json").read_bytes())
    (out / "sfc-report.csv").write_bytes(_csv(sfc))


def run(args):
    if args.output.exists():
        raise ValueError("Evidence output must not exist")
    if any(args.output.is_relative_to(root) for root in (args.archive, args.nov6_archive)):
        raise ValueError("Evidence output must be outside both read-only archives")
    args.output.mkdir(parents=True)
    root = Path(__file__).resolve().parents[1]
    for label, archive in (("fixed", args.archive), ("nov6", args.nov6_archive)):
        verify_git_snapshot(archive, ARCHIVE_COMMITS[label], root)
    before = {"fixed": hashes(args.archive), "nov6": hashes(args.nov6_archive)}
    (args.output / "archive-before.json").write_bytes(raw(before))
    commit = subprocess.check_output(
        ["git", "rev-parse", args.baseline_ref + "^{commit}"], cwd=root, text=True
    ).strip()
    with TemporaryDirectory(prefix="t56-baseline-") as directory:
        baseline = Path(directory)
        archive = subprocess.check_output(["git", "archive", commit, "src", "data"], cwd=root)
        with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
            stream.extractall(baseline, filter="data")
        for label, source in (("baseline", baseline), ("current", root)):
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--worker",
                "--archive",
                str(args.archive),
                "--nov6-archive",
                str(args.nov6_archive),
                "--output",
                str(args.output / label),
            ]
            process = subprocess.run(
                command,
                env={**os.environ, "PYTHONPATH": str(source / "src")},
                text=True,
                capture_output=True,
                cwd=root,
            )
            (args.output / f"{label}.log").write_text(process.stdout + process.stderr)
            process.check_returncode()
    after = {"fixed": hashes(args.archive), "nov6": hashes(args.nov6_archive)}
    (args.output / "archive-after.json").write_bytes(raw(after))
    assert before == after
    base = args.output / "baseline"
    current = args.output / "current"
    original = json.loads((base / "f-summary.json").read_bytes())
    summary = json.loads((current / "f-summary.json").read_bytes())
    dedup = summary.pop("deduplication")
    assert raw(original) == raw(summary)
    assert summary["totals"]["groups"] == 21
    assert dedup["unique_combinations"] == 14
    assert dedup["first_occurrence"]["covered_groups"] == 8
    pair = next(
        row
        for row in dedup["repeated_combinations"]
        if {row["single"]["match_id"], row["double"]["match_id"]} == {"jc:2042625", "jc:2042626"}
    )
    assert pair["occurrences"] == 3 and pair["repeat_count"] == 2
    assert len(pair["batch_ids"]) == 3
    base_line = (base / "ac-milan-row.txt").read_text().rstrip("\n")
    line = (current / "ac-milan-row.txt").read_text().rstrip("\n")
    table = (current / "nov6-single-table.md").read_text()
    # Owner decision on PR114: keep scores inline; half-row length is not a gate.
    assert "默认实力：客队 费伦茨" in line
    assert table.count("默认实力说明：") == 1
    assert "weakest_quartile_half_shrinkage" in table.replace("\\_", "_")
    assert "96.29% / 3.07% / 0.64%" in line
    assert "4:0 14.70%" in line and "6:0 8.61%" in line
    assert "比分 S" not in table and "| S1 |" not in table
    display = json.loads((current / "nov6-display.json").read_bytes())
    assert display and all(
        row["model_name_inline"]
        and row["model_hash_in_note"]
        and row["top_five_inline"]
        and row["displayed_p2"] == row["expected_p2"]
        for row in display
    )
    csv_hashes = {}
    for name in ("single-report.csv", "sfc-report.csv"):
        assert (base / name).read_bytes() == (current / name).read_bytes()
        csv_hashes[name] = hashlib.sha256((current / name).read_bytes()).hexdigest()
    daily = (current / "daily-fixed/2026-11-12.md").read_text()
    assert "21 组 / 14 种不同组合" in daily
    repeated_line = (
        "单选 热那亚 VS AC米兰（jc:2042626）客胜 × 双选 蒙扎 VS 亚特兰大（jc:2042625）平+客胜"
    )
    assert repeated_line in daily and repeated_line in (current / "f-summary.md").read_text()
    most_line = next(line for line in daily.splitlines() if line.startswith("被重复押注最多"))
    for row in dedup["most_repeated_matches"]:
        assert f"{row['home']} VS {row['away']}（{row['match_id']}）" in most_line
    report = dict(
        baseline_code=commit,
        archive_commits=ARCHIVE_COMMITS,
        git_tree_bytes_verified=True,
        archives_unchanged={key: len(value) for key, value in before.items()},
        original_summary_fields_byte_equal=True,
        original_totals=summary["totals"],
        deduplication=dedup,
        repeated_pair=pair,
        repeated_pair_display=repeated_line,
        nov6_ac_milan=dict(
            baseline_characters=len(base_line),
            current_characters=len(line),
            footnote_count=table.count("默认实力说明："),
            historical_replay_utc="2026-11-05T16:05:00+00:00",
            half_row_length_required=False,
            owner_decision="https://github.com/qw5563680/FOOTBALL/pull/114#issuecomment-6829284391",
        ),
        nov6_display_checks=display,
        csv_bytes_equal=csv_hashes,
        network_requests=0,
        production_written=False,
    )
    (args.output / "summary.json").write_bytes(raw(report))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=lambda value: Path(value).resolve(), required=True)
    parser.add_argument("--nov6-archive", type=lambda value: Path(value).resolve(), required=True)
    parser.add_argument("--output", type=lambda value: Path(value).resolve(), required=True)
    parser.add_argument("--baseline-ref", default="c1ace6fc5d2131fb0d4bdf2421420c6bd9e3c543")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    options = parser.parse_args()
    worker(options) if options.worker else run(options)
