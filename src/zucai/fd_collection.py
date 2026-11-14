"""Optional successful FD run evidence; never a response capture timestamp."""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from zucai.sources.base import _atomic_write

FD_COLLECTION_FILE = "state/fd_collection.json"
_STARTED = "last_successful_run_started_utc"
_UTC_TIMESTAMP = (
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)"
)


def _utc_timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(_UTC_TIMESTAMP, value):
        raise ValueError("FD 成功采集时间须为 UTC ISO 时间")
    return datetime.fromisoformat(value)


def read_fd_success(path: Path, *, latest: datetime | str) -> datetime:
    """Require the exact versioned schema and a UTC start no later than latest."""
    try:
        if path.is_symlink():
            raise ValueError("FD 成功采集状态不允许符号链接")
        saved = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(saved, dict)
            or set(saved) != {"version", _STARTED}
            or type(saved["version"]) is not int
            or saved["version"] != 1
        ):
            raise ValueError("FD 成功采集状态字段或版本无效")
        started = _utc_timestamp(saved[_STARTED])
        limit = _utc_timestamp(latest) if isinstance(latest, str) else latest
        if not isinstance(limit, datetime) or limit.utcoffset() != timedelta(0) or started > limit:
            raise ValueError("FD 成功采集时间晚于本次运行或归档创建时间")
        return started
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("FD 成功采集状态校验失败") from exc


def save_fd_success(path: Path, started: datetime) -> None:
    """Write only after the whole FD batch succeeds, retaining its actual run start."""
    saved = {"version": 1, _STARTED: started.isoformat()}
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, (json.dumps(saved, sort_keys=True) + "\n").encode())
