"""External, hash-checked capture receipts; never put expanded snapshots in git."""

import csv
import gzip
import json
import re
import shutil
from pathlib import Path

from zucai.sources.base import _atomic_write
from zucai.stats_release import sha256, upload_verified

RECEIPTS = "state/injuries_captures"


def references(directory):
    path = Path(directory) / "state/audit/injuries_ingest_audit.csv"
    result = {}
    if not path.exists():
        return result
    with path.open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            detail = json.loads(row.get("detail") or "{}")
            if detail.get("receipt_version") != "t38-injury-capture-v3":
                continue
            name, digest = detail.get("receipt_path"), detail.get("receipt_sha256")
            size = detail.get("receipt_bytes")
            if (
                not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or name != f"{RECEIPTS}/{digest}.json.gz"
                or type(size) is not int
                or size <= 0
            ):
                raise ValueError("伤停压缩回执描述无效")
            if name in result and result[name] != (digest, size):
                raise ValueError("伤停压缩回执描述冲突")
            result[name] = (digest, size)
    return result


def verify_bytes(path, digest, size):
    if not path.is_file() or path.stat().st_size != size or sha256(path) != digest:
        raise ValueError("伤停压缩回执哈希或大小不符")
    try:
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            doc = json.load(stream)
        if not isinstance(doc, dict) or doc.get("receipt_version") != "t38-injury-capture-v3":
            raise ValueError("伤停压缩回执格式无效")
    except (OSError, EOFError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("伤停压缩回执损坏") from exc


def check_published(directory, block):
    """Index-only exports need the descriptors, not downloaded capture bodies."""
    assets = {
        a["relative_path"]: (a["sha256"], a["size"])
        for a in (block or {}).get("injury_receipts", [])
    }
    for name, value in references(directory).items():
        if assets.get(name) != value:
            raise ValueError("伤停压缩回执尚未发布")


def publish_receipts(directory, previous, release, tag):
    from zucai.injuries_storage import capture_receipt_paths

    capture_receipt_paths(directory)
    old = {a["relative_path"]: a for a in (previous or {}).get("injury_receipts", [])}
    refs = references(directory)
    if not old.keys() <= refs.keys():
        raise ValueError("伤停历史回执不能丢失")
    result = []
    for name, (digest, size) in sorted(refs.items()):
        path = Path(directory) / name
        verify_bytes(path, digest, size)
        if name in old:
            asset = old[name]
            if (asset["sha256"], asset["size"]) != (digest, size):
                raise ValueError("伤停历史回执不能改写")
        else:
            # Content-addressed basename is already a valid immutable asset name.
            asset = dict(upload_verified(release, tag, path), relative_path=name)
        result.append(asset)
    return result


def prepare_receipts(directory, block, stage, release):
    from zucai.injuries_storage import capture_receipt_paths

    prepared = []
    check_published(directory, block)
    capture_stage = stage / "capture-validation"
    for asset in block.get("injury_receipts", []):
        path = Path(directory) / asset["relative_path"]
        staged = capture_stage / asset["relative_path"]
        staged.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            verify_bytes(path, asset["sha256"], asset["size"])
            shutil.copyfile(path, staged)
            continue
        release.download(asset["tag"], asset["name"], staged)
        verify_bytes(staged, asset["sha256"], asset["size"])
        prepared.append((path, staged))
    audit_name = "state/audit/injuries_ingest_audit.csv"
    if (Path(directory) / audit_name).exists():
        target = capture_stage / audit_name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(directory) / audit_name, target)
        capture_receipt_paths(capture_stage)
    return prepared


def install_receipts(prepared):
    for path, staged in prepared:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, staged.read_bytes())
