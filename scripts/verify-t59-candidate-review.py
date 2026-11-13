"""T59 offline acceptance on the fixed first-five archive; never publish or collect.

Run with PYTHONPATH=src and explicit --archive, --plan, --output. The output must
not exist. Large disposable copies stay under work/, and delivery/ contains the
portable evidence only. A copied queue gains one explicitly synthetic candidate
solely to check that a previously approved plan is rejected without mutation.
"""

import argparse
import copy
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from zucai.cloud_feeds import MANIFEST, archive_info, digest, export_feeds, restore_feeds
from zucai.daily_digest import generate
from zucai.digest_aliases import pending_name_lines
from zucai.jc_candidate_review import QUEUE, inspect_candidates
from zucai.sources.teams import normalize_team

LOG = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
# The last frozen source digest (43405379155-1) predates this review. Using
# feed_manifest.created_utc for a full render would predate that digest and
# correctly hit the existing no-backdated-version guard.
HISTORICAL_CUTOFF = "2026-11-13T10:26:22.717365+00:00"
EARLIER_FEED_CUTOFF = "2026-11-13T10:10:46.658458+00:00"
RUN_ID = "T59-offline-first-five"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def tree_hashes(root):
    return {
        p.relative_to(root).as_posix(): digest(p)
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(root).parts
    }


def save_hashes(path, hashes):
    path.write_text("".join(f"{v}  {k}\n" for k, v in sorted(hashes.items())), encoding="utf-8")


def supporting_evidence(database, inspection, plan):
    """Expose all exact owners and the leagues of otherwise opaque provider IDs."""
    names = []
    for decision in plan["decisions"]:
        keys = {normalize_team(n).casefold() for n in decision["checked_names"]}
        owners = [
            row
            for row in inspection["teams"]
            if any(normalize_team(n).casefold() in keys for n in [row["name"], *row["aliases"]])
        ]
        names.append(
            dict(
                raw_name=decision["raw_name"], terms=decision["checked_names"], exact_owners=owners
            )
        )
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        opaque = []
        for team in inspection["teams"]:
            if re.fullmatch(r"(?:apif|fdorg):team:\d+", team["name"]):
                leagues = connection.execute(
                    "SELECT DISTINCT league FROM matches WHERE home_team_id=? OR away_team_id=? "
                    "ORDER BY league",
                    (team["id"], team["id"]),
                )
                opaque.append({**team, "observed_leagues": [row[0] for row in leagues]})
        history = connection.execute(
            "SELECT count(*),max(kickoff_utc) FROM matches "
            "WHERE home_team_id=607 OR away_team_id=607"
        ).fetchone()
    return dict(
        names_search_results=names,
        opaque_identity_review=opaque,
        shonan_existing_team=[t for t in inspection["teams"] if t["id"] == 607],
        shonan_old_match_count=history[0],
        shonan_latest_kickoff_utc=history[1],
    )


def run_operation(archive, plan, work, evidence, run_id):
    work.mkdir(parents=True, exist_ok=False)
    command = [
        sys.executable,
        "-m",
        "zucai.cloud_candidate_review",
        "--archive",
        str(archive),
        "--plan",
        str(plan),
        "--run-id",
        run_id,
    ]
    env = {key: value for key, value in os.environ.items() if key != "GITHUB_STEP_SUMMARY"}
    env["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run(command, cwd=work, env=env, capture_output=True, text=True)
    evidence.mkdir(parents=True, exist_ok=False)
    (evidence / "stdout.log").write_text(result.stdout, encoding="utf-8")
    (evidence / "stderr.log").write_text(result.stderr, encoding="utf-8")
    for name in ("summary.md", "approved-plan.json"):
        if (work / name).exists():
            shutil.copyfile(work / name, evidence / name)
    write_json(evidence / "command.json", dict(argv=command, exit_code=result.returncode))
    return result


def rejected(archive, plan, work, evidence, run_id):
    before = tree_hashes(archive)
    result = run_operation(archive, plan, work, evidence, run_id)
    assert result.returncode != 0, result.stdout
    assert "身份清单或候选队列已变化" in result.stderr, result.stderr
    after = tree_hashes(archive)
    assert before == after
    save_hashes(evidence / "before.SHA256SUMS", before)
    save_hashes(evidence / "after.SHA256SUMS", after)
    return dict(
        exit_code=result.returncode, unchanged_files=len(before), archive_bytes_unchanged=True
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    source, plan_path, output = (
        args.archive.resolve(strict=True),
        args.plan.resolve(strict=True),
        args.output.resolve(),
    )
    if output.is_relative_to(source):
        raise ValueError("验收输出须在源归档之外")
    output.mkdir(parents=True, exist_ok=False)
    work, evidence = output / "work", output / "delivery"
    work.mkdir()
    evidence.mkdir()
    now = datetime.now(UTC)
    plan = json.loads(plan_path.read_bytes())
    meta = archive_info(source)
    assert digest(source / MANIFEST) == plan["source_feed_manifest_sha256"]
    original = tree_hashes(source)
    save_hashes(evidence / "source.SHA256SUMS", original)
    shutil.copyfile(plan_path, evidence / "selected-plan.json")
    before_db = work / "before/feeds.db"
    restore_feeds(source, before_db)
    before_db_hash = digest(before_db)
    inspection = inspect_candidates(before_db)
    for name in ("inventory_sha256", "queue_sha256"):
        assert inspection[name] == plan[name]
    assert inspection["counts"] == {"teams": 1024, "matches": 35713}
    write_json(evidence / "inspection.json", inspection)
    write_json(
        evidence / "supporting-evidence.json", supporting_evidence(before_db, inspection, plan)
    )
    archive = work / "archive-copy"
    shutil.copytree(source, archive, ignore=shutil.ignore_patterns(".git"))
    assert tree_hashes(archive) == original
    original_queue, original_manifest = (
        (archive / QUEUE).read_bytes(),
        (archive / MANIFEST).read_bytes(),
    )
    for name, raw in (
        ("queue-before.json", original_queue),
        ("manifest-before.json", original_manifest),
    ):
        (evidence / name).write_bytes(raw)

    bad_plan = copy.deepcopy(plan)
    bad_plan["queue_sha256"] = "0" * 64
    write_json(evidence / "bad-hash-plan.json", bad_plan)
    bad_hash = rejected(
        archive,
        evidence / "bad-hash-plan.json",
        work / "bad-hash-cli",
        evidence / "bad-hash",
        "T59-bad-hash",
    )
    added = json.loads(original_queue)
    added["T59合成测试赛事"] = {
        "T59合成新候选": {"source": "jc", "team_id": -59, "status": "pending", "evidence": []}
    }
    write_json(archive / QUEUE, added)
    new_meta = copy.deepcopy(meta)
    new_meta["files"][QUEUE] = digest(archive / QUEUE)
    write_json(archive / MANIFEST, new_meta)
    archive_info(archive)
    new_candidate = rejected(
        archive,
        plan_path,
        work / "new-candidate-cli",
        evidence / "new-candidate",
        "T59-new-candidate",
    )
    new_candidate["mutation"] = "合成新增候选，仅用于验证旧计划拒绝，不是实际比赛资料"
    (evidence / "new-candidate/queue-input.json").write_bytes((archive / QUEUE).read_bytes())
    (evidence / "new-candidate/manifest-input.json").write_bytes((archive / MANIFEST).read_bytes())
    (archive / QUEUE).write_bytes(original_queue)
    (archive / MANIFEST).write_bytes(original_manifest)
    assert tree_hashes(archive) == original
    before_lines = pending_name_lines(archive, meta, now)
    digest_before = generate(archive, evidence / "digest-before", now=now, run_id="T59-before")
    assert before_lines[0] in digest_before.read_text(encoding="utf-8")
    assert tree_hashes(archive) == original

    result = run_operation(archive, plan_path, work / "apply-cli", evidence / "apply", RUN_ID)
    assert result.returncode == 0, result.stderr
    after = tree_hashes(archive)
    changed = sorted(k for k in original if after.get(k) != original[k])
    assert changed == sorted([QUEUE, MANIFEST]), changed
    audit_prefix = f"state/audit/jc_candidate_reviews/{RUN_ID}"
    audit_files = [
        f"{audit_prefix}/{name}.json"
        for name in ("plan", "identity_inventory", "review", "operation")
    ]
    assert set(after) - set(original) == set(audit_files)
    after_meta = archive_info(archive)
    assert all(after_meta["files"][key] == after[key] for key in audit_files)
    receipt = json.loads((archive / f"{audit_prefix}/operation.json").read_bytes())
    queue = json.loads((archive / QUEUE).read_bytes())
    cleared = sorted(
        name
        for names in queue.values()
        for name, row in names.items()
        if row["status"] == "cleared"
    )
    pending = sorted(
        name
        for names in queue.values()
        for name, row in names.items()
        if row["status"] == "pending"
    )
    assert cleared == sorted(["哥伦比亚", "巴拉圭", "新加坡", "阿根廷"])
    assert pending == ["湘南海洋"]
    for key in [QUEUE, MANIFEST, *audit_files]:
        target = evidence / "prepared" / key
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(archive / key, target)
    save_hashes(evidence / "prepared.SHA256SUMS", after)

    restored = work / "restored/feeds.db"
    restore_feeds(archive, restored)
    assert digest(restored) == before_db_hash
    restored_info = inspect_candidates(restored)
    assert restored_info["inventory_sha256"] == inspection["inventory_sha256"]
    for key in [QUEUE, *audit_files]:
        assert (restored.parent / key).read_bytes() == (archive / key).read_bytes()
    exported = work / "roundtrip-archive"
    export_feeds(restored, exported, date(2026, 11, 13), "T59-roundtrip")
    archive_info(exported)
    twice = work / "restored-twice/feeds.db"
    restore_feeds(exported, twice)
    assert digest(twice) == before_db_hash
    for key in [QUEUE, *audit_files]:
        assert (twice.parent / key).read_bytes() == (archive / key).read_bytes()
    after_lines = pending_name_lines(archive, after_meta, datetime.now(UTC))
    historical_lines = pending_name_lines(archive, after_meta, HISTORICAL_CUTOFF)
    earlier_lines = pending_name_lines(archive, after_meta, EARLIER_FEED_CUTOFF)
    assert "5 个" in before_lines[0] and "1 个" in after_lines[0]
    assert "湘南海洋" in after_lines[0] and "5 个" in historical_lines[0]
    assert "5 个" in earlier_lines[0]
    digest_after = generate(
        archive, evidence / "digest-after", now=datetime.now(UTC), run_id="T59-after"
    )
    digest_history = generate(
        archive, evidence / "digest-historical", now=HISTORICAL_CUTOFF, run_id="T59-old-cutoff"
    )
    assert after_lines[0] in digest_after.read_text(encoding="utf-8")
    assert historical_lines[0] in digest_history.read_text(encoding="utf-8")
    assert tree_hashes(archive) == after
    assert tree_hashes(source) == original
    assert digest(before_db) == before_db_hash
    frozen = [
        k
        for k in original
        if k.startswith(
            ("forecasts/", "scoreboard/", "parlay/", "daily/", "sfc/", "reports/", "runs/")
        )
    ]
    assert frozen and all(after[k] == original[k] for k in frozen)
    summary = dict(
        source_commit=plan["source_commit"],
        source_feed_manifest_sha256=plan["source_feed_manifest_sha256"],
        checked_utc=now.isoformat(),
        checked_beijing=now.astimezone(ZoneInfo("Asia/Shanghai")).isoformat(),
        sports_requests=0,
        production_published=False,
        source_file_count=len(original),
        source_archive_bytes_unchanged=True,
        original_files_changed_in_copy=changed,
        unchanged_existing_files=len(original) - len(changed),
        added_audit_files=audit_files,
        unchanged_frozen_files=len(frozen),
        database_sha256_before=before_db_hash,
        database_sha256_after_restore=digest(restored),
        database_sha256_after_roundtrip=digest(twice),
        queue_and_four_audits_byte_equal_after_restore_and_export_restore=True,
        counts=inspection["counts"],
        pending_before=receipt["pending_before"],
        pending_after=receipt["pending_after"],
        cleared=cleared,
        pending=pending,
        before_digest_line=before_lines[0],
        after_digest_line=after_lines[0],
        historical_cutoff_utc=HISTORICAL_CUTOFF,
        historical_cutoff_beijing=datetime.fromisoformat(HISTORICAL_CUTOFF)
        .astimezone(ZoneInfo("Asia/Shanghai"))
        .isoformat(),
        historical_digest_line=historical_lines[0],
        earlier_feed_cutoff_utc=EARLIER_FEED_CUTOFF,
        earlier_feed_cutoff_pending_line=earlier_lines[0],
        bad_hash=bad_hash,
        newly_added_candidate=new_candidate,
        reviewer_online_acceptance="pending; this run is offline only",
    )
    write_json(evidence / "summary.json", summary)
    LOG.info("离线验收通过：5 → 1；原归档 %s 文件不变；线上验收待审查者触发", len(original))


if __name__ == "__main__":
    main()
