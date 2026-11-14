"""Apply a pinned candidate review to an archive checkout without collecting or predicting.

The caller publishes with the shared feed writer only after this command succeeds.
The original T58 decision rules are reused unchanged on a disposable restored DB.
"""

import argparse
import copy
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from zucai.cloud_feeds import (
    CANDIDATE_REVIEW_DIR,
    CANDIDATE_REVIEW_RUN_ID,
    MANIFEST,
    archive_info,
    digest,
    restore_feeds,
)
from zucai.forecast_archive import _path
from zucai.jc_candidate_review import QUEUE, review_candidates
from zucai.sources.base import _atomic_write

LOG = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]


def _encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def dispatch_plan(options, root=ROOT):
    """Accept only the exact committed bytes of a plan in the reviewed code checkout."""
    payload = json.loads(options)
    if not isinstance(payload, dict) or set(payload) != {"plan"}:
        raise ValueError("candidate-review options 只能包含 plan")
    name = payload["plan"]
    if not isinstance(name, str) or not re.fullmatch(
        r"data/jc_candidate_reviews/[A-Za-z0-9][A-Za-z0-9_-]*\.json", name
    ):
        raise ValueError("plan 必须是 data/jc_candidate_reviews/ 下的 JSON 计划")
    root = Path(root).resolve()
    path = _path(root, name)
    committed = subprocess.run(
        ["git", "-C", str(root), "show", f"HEAD:{name}"], capture_output=True, check=False
    )
    if committed.returncode or not path.is_file() or path.read_bytes() != committed.stdout:
        raise ValueError("核验计划须已提交，且工作副本字节须与所选代码提交一致")
    return path


def _replace_review(archive, directory, contents):
    """Write manifest last and restore every changed byte if a local write fails."""
    paths = {key: _path(archive, key) for key in contents}
    backups = {key: path.read_bytes() if path.exists() else None for key, path in paths.items()}
    directory.mkdir(parents=True, exist_ok=False)
    try:
        for key, raw in contents.items():
            _atomic_write(paths[key], raw)
        archive_info(archive)
    except BaseException:
        for key, original in backups.items():
            if original is None:
                paths[key].unlink(missing_ok=True)
            else:
                paths[key].write_bytes(original)
        shutil.rmtree(directory)
        raise


def apply_to_archive(archive, plan_path, run_id):
    """Prepare queue + immutable audit + manifest; leave every other archive file alone."""
    if not isinstance(run_id, str) or not re.fullmatch(CANDIDATE_REVIEW_RUN_ID, run_id):
        raise ValueError("候选核验 run ID 无效")
    archive = Path(archive).resolve(strict=True)
    plan_bytes = Path(plan_path).read_bytes()
    meta = archive_info(archive)
    if QUEUE not in meta["files"]:
        raise ValueError("归档清单中缺少竞彩待核队列")
    manifest_path = _path(archive, MANIFEST)
    original_manifest = manifest_path.read_bytes()
    queue = _path(archive, QUEUE)
    original_queue = queue.read_bytes()
    prefix = f"{CANDIDATE_REVIEW_DIR}/{run_id}"
    destination = _path(archive, prefix)
    if destination.exists():
        raise FileExistsError("核验运行目录已存在，不覆盖旧审计")

    with TemporaryDirectory(prefix="t59-candidate-review-") as temporary:
        work = Path(temporary)
        database = work / "feeds.db"
        restore_feeds(archive, database)
        before_db = digest(database)
        selected_plan = work / "selected-plan.json"
        selected_plan.write_bytes(plan_bytes)
        audit = work / prefix
        report = review_candidates(database, selected_plan, audit)
        if digest(database) != before_db:
            raise ValueError("候选处置意外改变数据库，拒绝发布")
        queue_bytes = (work / QUEUE).read_bytes()
        operation = dict(
            version=1,
            operation="candidate-review",
            run_id=run_id,
            code_commit=os.environ.get("GITHUB_SHA"),
            actor=os.environ.get("GITHUB_ACTOR") or "local",
            archive_manifest_before_sha256=hashlib.sha256(original_manifest).hexdigest(),
            inventory_sha256=report["inventory_sha256"],
            queue_before_sha256=report["queue_before_sha256"],
            queue_after_sha256=hashlib.sha256(queue_bytes).hexdigest(),
            plan_sha256=report["plan_sha256"],
            recorded_utc=report["recorded_utc"],
            recorded_beijing=report["recorded_beijing"],
            pending_before=report["pending_before"],
            pending_after=report["pending_after"],
            requests=0,
            audit=f"{prefix}/review.json",
            database_sha256_unchanged=before_db,
        )
        contents = {
            f"{prefix}/{name}": (audit / name).read_bytes()
            for name in ("plan.json", "identity_inventory.json", "review.json")
        }
        contents[f"{prefix}/operation.json"] = _encoded(operation)
        contents[QUEUE] = queue_bytes
        updated = copy.deepcopy(meta)
        updated["files"].update(
            {key: hashlib.sha256(raw).hexdigest() for key, raw in contents.items()}
        )
        updated.update(
            run=run_id,
            operation="candidate-review",
            event_name=os.environ.get("GITHUB_EVENT_NAME") or "local",
            created_utc=report["recorded_utc"],
        )
        updated.setdefault("collection", {})["jc_candidate_review"] = {
            "status": "complete",
            **operation,
        }
        contents[MANIFEST] = _encoded(updated)
        # The shared Actions lock is the production writer lock; also fail closed
        # if the selected local checkout changed while its DB was restored/reviewed.
        if (
            manifest_path.read_bytes() != original_manifest
            or queue.read_bytes() != original_queue
            or archive_info(archive) != meta
        ):
            raise ValueError("核验期间归档已变化，须重新 inspect 并制定计划")
        _replace_review(archive, destination, contents)
    return operation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    plans = parser.add_mutually_exclusive_group(required=True)
    plans.add_argument("--plan", type=Path, help="离线等价操作：明确指定核验计划")
    plans.add_argument("--dispatch-options", help="Actions：只接受已提交的 options.plan")
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if Path.cwd().resolve().is_relative_to(args.archive.resolve()):
        raise ValueError("工作目录须在归档之外，防止失败诊断改动归档")
    plan = dispatch_plan(args.dispatch_options) if args.dispatch_options is not None else args.plan
    # Workflow runs in a fresh WORK_DIR. Keep selection/failure evidence outside
    # the archive, so a stale plan cannot leave an uncommitted archive mutation.
    if Path("summary.md").exists() or Path("approved-plan.json").exists():
        raise FileExistsError("操作证据目录须为空，请在新工作目录运行")
    Path("approved-plan.json").write_bytes(plan.read_bytes())
    summary = ""
    try:
        result = apply_to_archive(args.archive, Path("approved-plan.json"), args.run_id)
        summary = (
            "### 竞彩候选核验处置（等待发布步骤）\n\n"
            f"待核 {result['pending_before']} → {result['pending_after']}；体育请求 0。\n\n"
            f"核验时间：{result['recorded_beijing']}（北京时间）；{result['recorded_utc']}（UTC）。\n\n"
            f"审计：`{result['audit']}`；模型、比赛、冻结预测、旧报告与账本未修改。\n"
        )
        LOG.info("候选处置准备完成：%s → %s", result["pending_before"], result["pending_after"])
    except Exception as exc:
        summary = f"### 竞彩候选核验处置失败，未发布\n\n{exc}\n"
        raise
    finally:
        Path("summary.md").write_text(summary, encoding="utf-8")
        if target := os.environ.get("GITHUB_STEP_SUMMARY"):
            with Path(target).open("a", encoding="utf-8") as stream:
                stream.write(summary)


if __name__ == "__main__":
    main()
