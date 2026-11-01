"""下载、不可覆盖的原始快照和离线解析约定。"""

import json
import math
import re
import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock
from typing import Any, ClassVar, Generic, TypeVar
from uuid import uuid4

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry


class _SourceSession(requests.Session):
    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", 15)
        return super().request(method, url, **kwargs)


def make_session() -> requests.Session:
    session = _SourceSession()
    session.headers["User-Agent"] = "Mozilla/5.0 (compatible; ZucaiResearch/0.1)"
    retries = Retry(
        total=3,
        read=0,
        other=0,
        backoff_factor=1,
        allowed_methods={"GET", "HEAD"},
        # EdgeOne's 567 is a policy denial, not a transient service failure.
        status_forcelist=(500, 502, 503, 504),
        respect_retry_after_header=False,
        raise_on_status=False,
    )
    for protocol in ("http://", "https://"):
        session.mount(protocol, HTTPAdapter(max_retries=retries))
    return session


def _atomic_write(dest: Path, content: bytes) -> None:
    staged = NamedTemporaryFile(dir=dest.parent, suffix=".part", delete=False)
    tmp = Path(staged.name)
    try:
        with staged:
            staged.write(content)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)


class RawStore:
    def __init__(self, root: Path | str = "data/raw"):
        self.root = Path(root).resolve()

    def save(
        self,
        source: str,
        name: str,
        content: bytes,
        ext: str,
        *,
        captured_at: datetime | None = None,
        url: str | None = None,
        status_code: int | None = None,
    ) -> Path:
        """Persist bytes and their provenance. Manual byte imports can omit HTTP fields."""
        extension = ext.removeprefix(".")
        if not all(re.fullmatch(r"[\w-]+(?:\.[\w-]+)*", part) for part in (source, name)):
            raise ValueError("来源和快照名称必须是单个文件名")
        if not re.fullmatch(r"[a-zA-Z0-9]+", extension):
            raise ValueError("无效的文件扩展名")
        instant = captured_at if captured_at is not None else datetime.now(UTC)
        if instant.utcoffset() is None:
            raise ValueError("快照时间必须包含时区")
        utc = instant.astimezone(UTC)
        folder = (self.root / source / utc.date().isoformat()).resolve()
        if not folder.is_relative_to(self.root):
            raise ValueError("快照路径超出存档目录")
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"{name}_{utc:%H%M%S%f}Z_{uuid4().hex}.{extension}"
        metadata = {
            "url": url,
            "fetched_at_utc": utc.isoformat(timespec="microseconds").replace("+00:00", "Z"),
            "status_code": status_code,
        }
        sidecar = dest.with_suffix(dest.suffix + ".meta.json")
        _atomic_write(sidecar, json.dumps(metadata, ensure_ascii=False).encode("utf-8"))
        try:
            _atomic_write(dest, content)
        except BaseException:
            sidecar.unlink(missing_ok=True)
            raise
        return dest

    def save_response(
        self,
        source: str,
        name: str,
        response: requests.Response,
        ext: str,
        *,
        captured_at: datetime | None = None,
    ) -> Path:
        return self.save(
            source,
            name,
            response.content,
            ext,
            captured_at=captured_at,
            url=response.url,
            status_code=response.status_code,
        )

    def load(self, path: Path | str) -> bytes:
        snapshot = Path(path)
        resolved = (self.root / snapshot).resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("读取路径超出存档目录")
        return resolved.read_bytes()

    def load_metadata(self, path: Path | str) -> dict[str, str | int | None]:
        snapshot = Path(path)
        return json.loads(self.load(snapshot.with_suffix(snapshot.suffix + ".meta.json")))


class RateLimiter:
    """Space the start of consecutive requests sharing this limiter."""

    def __init__(self, min_interval_s: float):
        if not math.isfinite(min_interval_s) or min_interval_s < 0:
            raise ValueError("请求间隔必须是非负有限值")
        self.min_interval_s = min_interval_s
        self._last_start: float | None = None
        self._lock = Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if self._last_start is not None:
                remaining = self.min_interval_s - (now - self._last_start)
                if remaining > 0:
                    time.sleep(remaining)
            self._last_start = time.monotonic()


Record = TypeVar("Record")


class Source(ABC, Generic[Record]):
    name: ClassVar[str]

    @abstractmethod
    def fetch_raw(self, **kwargs: Any) -> Path:
        """Archive the unmodified response and HTTP metadata with RawStore.save_response()."""

    @abstractmethod
    def parse(self, raw: bytes) -> list[Record]:
        """Derive records from bytes only, without network access or a wall clock."""
