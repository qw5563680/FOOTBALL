"""Verified append-only publication proof and explicit Git infrastructure failures."""

import hashlib
import json
import re
import subprocess
from pathlib import Path

from zucai.report.render import _timestamp

HISTORY_FILES = tuple(f"state/publication/{kind}_history.json" for kind in ("forecast", "sfc"))


class PublicationHistoryError(ValueError):
    """Required publication proof cannot be inspected; outputs must not be published."""


def git_read(root, *args, input=None):
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            input=input,
            capture_output=True,
            check=True,
            timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        # Never echo Git stderr/config: a remote URL may contain authentication.
        raise PublicationHistoryError(
            f"数据分支历史不可用：Git {args[0]} 命令失败（{type(exc).__name__}）"
        ) from exc


def check_history(root, published_ref):
    if not re.fullmatch(r"refs/remotes/[^/]+/data/football-feeds", published_ref):
        raise ValueError("发布时间必须来自远程 data/football-feeds 分支")
    if not (root / ".git").exists():
        raise PublicationHistoryError("数据分支历史不可用：非仓库或 .git 缺失")
    if Path(git_read(root, "rev-parse", "--show-toplevel").decode().strip()).resolve() != root:
        raise PublicationHistoryError("数据分支历史不可用：目录不是仓库顶层")
    if git_read(root, "rev-parse", "--is-shallow-repository").strip() != b"false":
        raise PublicationHistoryError("数据分支历史不可用：浅克隆")
    for args, reason in (
        (("show-ref", "--verify", published_ref), "published_ref 缺失或不可读"),
        (
            ("config", "--get", "remote." + published_ref.split("/")[2] + ".url"),
            "远端配置缺失或不可读",
        ),
    ):
        try:
            if not git_read(root, *args).strip():
                raise PublicationHistoryError(reason)
        except PublicationHistoryError as exc:
            raise PublicationHistoryError("数据分支历史不可用：" + reason) from exc


def precedes_calculation(published, generated):
    published, generated = _timestamp(published), _timestamp(generated)
    same_git_second = published.microsecond == 0 and published == generated.replace(microsecond=0)
    return published < generated and not same_git_second


def history_path(archive, name):
    path = Path(archive) / name
    if not path.resolve().is_relative_to(Path(archive).resolve()) or any(
        p.is_symlink() for p in (path, path.parent, path.parent.parent)
    ):
        raise ValueError("发布时间状态路径不允许符号链接或超出归档")
    return path


def read_history(archive, name):
    path = history_path(archive, name)
    if not path.exists():
        return {}
    doc = json.loads(path.read_bytes())
    if doc.get("schema_version") != 1 or not isinstance(doc.get("entries"), dict):
        raise ValueError("发布时间历史状态格式无效：" + name)
    return doc["entries"]


def encode_history(entries):
    return (
        json.dumps(
            {"schema_version": 1, "entries": entries}, ensure_ascii=False, sort_keys=True, indent=2
        )
        + "\n"
    ).encode()


def resolve_history(archive, kind, candidates, derive, *, pending=None):
    """Read-only resolution; queue new entries for the caller's publication transaction."""
    name = f"state/publication/{kind}_history.json"
    saved = read_history(archive, name) if candidates else {}
    resolved, missing = {}, {}
    for path, (sha, generated) in candidates.items():
        entry = saved.get(path)
        try:
            valid = (
                entry["method"] == "historical_first_containing_commit"
                and entry["candidate_path"] == path
                and entry["candidate_sha256"] == sha
                and re.fullmatch(r"[0-9a-f]{40,64}", entry["commit"])
                and re.fullmatch(r"refs/remotes/[^/]+/data/football-feeds", entry["ref"])
                and hashlib.sha256(history_path(archive, path).read_bytes()).hexdigest() == sha
                and not precedes_calculation(entry["published_utc"], generated)
            )
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            valid = False
        if valid:
            resolved[path] = entry
        else:
            missing[path] = sha
    if not missing:
        return resolved
    inferred = derive(missing)  # Unavailable infrastructure raises even for invalid saved slots.
    for path, entry in inferred.items():
        if path in saved or precedes_calculation(entry["published_utc"], candidates[path][1]):
            continue  # First saved entry is immutable, including invalid entries.
        resolved[path] = entry
        if pending is not None:
            pending.setdefault(name, {})[path] = entry
    return resolved


def publication_updates(archive, pending):
    """Prepare bytes, without writing, for the existing all-or-rollback output transaction."""
    updates = {}
    for name, additions in pending.items():
        saved = read_history(archive, name)
        merged = {**additions, **saved}
        if merged != saved:
            updates[history_path(archive, name)] = encode_history(merged)
    manifest_path = Path(archive) / "feed_manifest.json"
    if updates and manifest_path.exists():
        manifest = json.loads(manifest_path.read_bytes())
        changed = False
        for path, raw in updates.items():
            name = path.relative_to(archive).as_posix()
            if name in manifest["files"]:
                manifest["files"][name] = hashlib.sha256(raw).hexdigest()
                changed = True
        if changed:
            updates[manifest_path] = (
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
            ).encode()
    return updates
