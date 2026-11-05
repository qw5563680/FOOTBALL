"""Immutable daily render receipts; a previous issue-comment version is explicit evidence."""

import hashlib
import json
import re

from zucai.forecast_archive import _path
from zucai.scoreboard.score import instant


def source_hashes(archive):
    return {
        name: hashlib.sha256(_path(archive, name).read_bytes()).hexdigest()
        for name in ("feed_manifest.json", "forecast_manifest.json", "scoreboard/summary.json")
        if _path(archive, name).exists()
    }


def rendered_versions(root, today):
    """Read only frozen render pairs; notification sidecars are separate evidence."""
    directory = _path(root, today.isoformat())
    if not directory.exists():
        return
    for path in sorted(directory.glob("*.json")):
        if path.name.endswith(".notification.json"):
            continue
        path = _path(root, f"{today}/{path.name}")
        value = json.loads(path.read_bytes())
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != 1
            or value.get("date") != today.isoformat()
            or value.get("version_id") != path.stem
            or not isinstance(value.get("pending"), dict)
        ):
            raise ValueError("日报历史快照无效")
        markdown = _path(root, f"{today}/{path.stem}.md")
        if (
            not markdown.is_file()
            or hashlib.sha256(markdown.read_bytes()).hexdigest() != value["markdown_sha256"]
        ):
            raise ValueError("日报历史 Markdown 哈希不符")
        yield value


def notification_path(root, version):
    return _path(root, f"{version['date']}/{version['version_id']}.notification.json")


def read_notification(root, version):
    """Accept only a successful, hash-bound receipt for this exact rendered version."""
    path = notification_path(root, version)
    if not path.exists():
        return None
    receipt = json.loads(path.read_bytes())
    if (
        not isinstance(receipt, dict)
        or receipt.get("schema_version") != 1
        or receipt.get("status") != "published"
        or any(
            receipt.get(key) != version.get(key)
            for key in ("date", "run_id", "version_id", "markdown_sha256")
        )
        or not isinstance(receipt.get("repository"), str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", receipt["repository"])
        or any(
            type(receipt.get(key)) is not int or receipt[key] <= 0
            for key in ("issue_number", "comment_id")
        )
    ):
        raise ValueError("日报通知回执与冻结版本不符")
    try:
        instant(receipt["notified_utc"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("日报通知回执时间无效") from None
    return receipt


def version_context(archive, out, today, now, run_id, inputs, *, require_notified=False):
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", run_id)
        or run_id in {".", ".."}
        or run_id.endswith(".notification")
    ):
        raise ValueError("日报 run ID 格式无效")
    versions, notified = {}, {}
    for root in sorted({archive / "daily", out}):
        for value in rendered_versions(root, today):
            identity = value["version_id"], value["generated_utc"], value["markdown_sha256"]
            if identity in versions and versions[identity] != value:
                raise ValueError("日报历史版本冲突")
            versions[identity] = value
            if require_notified and (receipt := read_notification(root, value)):
                at = instant(receipt["notified_utc"])
                repository = inputs.get("repository")
                if at <= now and (repository is None or receipt["repository"] == repository):
                    notified[identity] = max(notified.get(identity, at), at)
    if any(instant(value["generated_utc"]) > now for value in versions.values()):
        raise ValueError("不可倒退已有日报版本")
    valid = [v for v in versions.values() if instant(v["generated_utc"]) <= now]
    same = next(
        (
            v
            for v in valid
            if v["run_id"] == run_id
            and instant(v["generated_utc"]) == now
            and v["inputs"] == inputs
        ),
        None,
    )
    candidates = [
        value
        for identity, value in versions.items()
        if value is not same and (not require_notified or identity in notified)
    ]
    if same:
        candidates = [v for v in candidates if v["update_number"] < same["update_number"]]
    previous = max(
        candidates,
        key=lambda v: (
            notified[v["version_id"], v["generated_utc"], v["markdown_sha256"]]
            if require_notified
            else instant(v["generated_utc"]),
            v["update_number"],
        ),
        default=None,
    )
    update = same["update_number"] if same else (previous["update_number"] + 1 if previous else 1)
    name = same["version_id"] if same else run_id
    if not same and any(v["version_id"] == name for v in valid):
        fingerprint = json.dumps([now.isoformat(), inputs], sort_keys=True).encode()
        name += "-" + hashlib.sha256(fingerprint).hexdigest()[:12]
    return previous, update, name
