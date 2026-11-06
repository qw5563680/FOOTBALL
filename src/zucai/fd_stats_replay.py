"""Explicit, offline replay of hash-pinned FD CSV originals and their sidecars."""

import hashlib
import json
import re
from pathlib import Path

from zucai.sources.apif_stats import instant
from zucai.stats_storage import ingest_fd, load_raw


def _sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def replay_fd(database, plan, *, leagues_path=None, aliases_path=None):
    """Prevalidate the entire plan; ingest each original using existing atomic receipts.

    Paths are explicit (relative to the plan, or absolute). No discovery, downloads,
    cursor updates or timestamp rewrites occur. Each file remains one transaction.
    """
    database, plan = Path(database), Path(plan)
    if not database.is_file():
        raise ValueError("离线重放需要已有且完整恢复的数据库")
    document = json.loads(plan.read_text(encoding="utf-8"))
    files = document.get("files") if isinstance(document, dict) else None
    if not isinstance(files, list) or not files:
        raise ValueError("重放清单需要非空 files 列表")
    prepared, seen = [], set()
    for item in files:
        fields = (
            "path",
            "div",
            "season",
            "sha256",
            "sidecar_sha256",
            "captured_utc",
            "raw_location",
        )
        if not isinstance(item, dict) or any(
            not isinstance(item.get(key), str) or not item[key].strip() for key in fields
        ):
            raise ValueError("重放清单缺少路径、范围、原件/sidecar哈希、真实采集时间或归档位置")
        if not re.fullmatch(r"[0-9]{4}", item["season"]):
            raise ValueError("重放清单供应商赛季无效")
        path = Path(item["path"])
        if not path.is_absolute():
            path = plan.parent / path
        sidecar = path.with_suffix(path.suffix + ".meta.json")
        if _sha(path) != item["sha256"] or _sha(sidecar) != item["sidecar_sha256"]:
            raise ValueError("重放清单的原件或 sidecar SHA256 不符")
        _, metadata = load_raw(
            path,
            endpoint="csv",
            parameters={"div": item["div"], "season": item["season"]},
            location=item["raw_location"],
        )
        if instant(metadata["captured_utc"]) != instant(item["captured_utc"]):
            raise ValueError("重放清单与原始 sidecar 的采集时间不符")
        key = (item["div"], item["season"], item["sha256"])
        if key in seen:
            raise ValueError("重放清单包含重复原件")
        seen.add(key)
        prepared.append((path, item))
    results = []
    for path, item in prepared:
        result = ingest_fd(
            database,
            path,
            div=item["div"],
            season=item["season"],
            leagues_path=leagues_path,
            aliases_path=aliases_path,
            raw_location=item["raw_location"],
            daily=False,
        )
        results.append(
            dict(div=item["div"], season=item["season"], sha256=item["sha256"], **result)
        )
    return dict(
        operation="replay_fd",
        requests=0,
        files=results,
        inserted=sum(row["inserted"] for row in results),
        rejected=sum(row["rejected"] for row in results),
    )
