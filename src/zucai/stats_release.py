"""Immutable monthly GitHub Release assets for source originals and row snapshots."""

import hashlib
import json
import os
import re
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote, urlparse

import requests

from zucai.sources.base import _atomic_write

TAG = r"stats-raw-[0-9]{4}(?:0[1-9]|1[0-2])"
NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]*"


def validate_tag(tag):
    if not isinstance(tag, str) or not re.fullmatch(TAG, tag):
        raise ValueError("Release tag 必须为 stats-raw-YYYYMM")


def validate_name(name):
    if not isinstance(name, str) or not re.fullmatch(NAME, name):
        raise ValueError("Release asset 文件名无效")


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def descriptor(tag, path):
    path = Path(path)
    validate_tag(tag)
    validate_name(path.name)
    return dict(tag=tag, name=path.name, sha256=sha256(path), size=path.stat().st_size)


def upload_verified(release, tag, path):
    want = descriptor(tag, path)
    got = release.upload(tag, Path(path))
    if any(got.get(key) != value for key, value in want.items()):
        raise ValueError("Release 上传回执哈希、大小或文件名不符")
    return want


def upload_checksums(release, tag, out, run_id, assets, *, kind="rows"):
    validate_tag(tag)
    validate_name(run_id)
    if not assets:
        return None
    path = Path(out) / (f"SHA256SUMS-raw-{run_id}" if kind == "raw" else f"SHA256SUMS-{run_id}")
    _atomic_write(path, "".join(f"{a['sha256']}  {a['name']}\n" for a in assets).encode())
    return upload_verified(release, tag, path)


class ReleaseHttpError(requests.HTTPError):
    """Bounded Release diagnostic without request headers, URLs or credentials."""

    def __init__(self, status_code, body, *, token=None):
        if token:
            # Redact before truncation, including tokens crossing the 200-character boundary.
            for secret in {token, quote(token, safe=""), json.dumps(token)[1:-1]}:
                body = body.replace(secret, "[REDACTED]")
        excerpt = body[:200].replace("\r", "\\r").replace("\n", "\\n")
        self.summary = f"Release HTTP {status_code}: {excerpt}"
        super().__init__(self.summary)


class ReleaseStore:
    """Only github.com repositories and GitHub's API/upload hosts receive credentials."""

    def __init__(self, repository=None, token=None, session=None):
        repository = repository or os.environ.get("GITHUB_REPOSITORY", "")
        if repository.startswith("https://"):
            parsed = urlparse(repository)
            if (
                parsed.netloc != "github.com"
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("Release repository 须为 github.com owner/repo")
            repository = parsed.path.removeprefix("/").removesuffix("/")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*", repository):
            raise ValueError("Release repository 须为 github.com owner/repo")
        self.repository = repository
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN")
        self.session = session or requests.Session()
        self.base = f"https://api.github.com/repos/{repository}"

    def _request(self, method, url, **kwargs):
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise ValueError("Release URL 无效")
        headers = {"Accept": "application/vnd.github+json", **kwargs.pop("headers", {})}
        if parsed.hostname in {"api.github.com", "uploads.github.com"} and self.token:
            headers["Authorization"] = "Bearer " + self.token
        else:
            headers.pop("Authorization", None)
        # Request-level auth/header overrides also suppress a caller-supplied session token.
        headers.setdefault("Authorization", None)
        response = self.session.request(
            method, url, headers=headers, auth=(), allow_redirects=False, timeout=120, **kwargs
        )
        return response

    def _raise_for_status(self, response):
        if 400 <= response.status_code < 600:
            raise ReleaseHttpError(response.status_code, response.text, token=self.token)

    def _release(self, tag, *, create=False):
        validate_tag(tag)
        response = self._request("GET", f"{self.base}/releases/tags/{quote(tag, safe='')}")
        if response.status_code == 404 and create:
            response = self._request(
                "POST", f"{self.base}/releases", json={"tag_name": tag, "name": tag}
            )
            if response.status_code == 422:  # Another writer may have created the month.
                response = self._request("GET", f"{self.base}/releases/tags/{tag}")
        self._raise_for_status(response)
        return response.json()

    def _assets(self, release):
        assets, page = [], 1
        while True:
            response = self._request(
                "GET",
                f"{self.base}/releases/{int(release['id'])}/assets",
                params={"per_page": 100, "page": page},
            )
            self._raise_for_status(response)
            batch = response.json()
            assets.extend(batch)
            if len(batch) < 100:
                return assets
            page += 1

    def _download_asset(self, asset, dest):
        url = f"{self.base}/releases/assets/{int(asset['id'])}"
        for _ in range(6):
            response = self._request(
                "GET", url, headers={"Accept": "application/octet-stream"}, stream=True
            )
            if response.status_code in {301, 302, 303, 307, 308}:
                url = response.headers["Location"]
                response.close()
                continue
            self._raise_for_status(response)
            dest = Path(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as output:
                for block in response.iter_content(1024 * 1024):
                    output.write(block)
            response.close()
            return dest
        raise ValueError("Release 下载重定向过多")

    def upload(self, tag: str, path: Path) -> dict:
        path = Path(path)
        if path.is_symlink() or not path.is_file():
            raise ValueError("Release 上传需要普通文件")
        want = descriptor(tag, path)
        release = self._release(tag, create=True)
        existing = next((a for a in self._assets(release) if a["name"] == path.name), None)
        if existing is not None:
            digest = existing.get("digest")
            if digest is None:
                with TemporaryDirectory(prefix="stats-release-") as temporary:
                    digest = "sha256:" + sha256(
                        self._download_asset(existing, Path(temporary) / "asset")
                    )
            if digest != "sha256:" + want["sha256"] or existing.get("size") != want["size"]:
                raise ValueError("Release asset collision：同名文件哈希冲突，禁止覆盖")
            return want
        url = release["upload_url"].split("{", 1)[0]
        if urlparse(url).netloc != "uploads.github.com" or not url.startswith("https://"):
            raise ValueError("Release 上传地址无效")
        with path.open("rb") as stream:
            response = self._request(
                "POST",
                url,
                params={"name": path.name},
                data=stream,
                headers={"Content-Type": "application/octet-stream"},
            )
        self._raise_for_status(response)
        asset = response.json()
        if asset.get("name") != want["name"] or asset.get("size") != want["size"]:
            raise ValueError("Release 上传大小或文件名不符")
        if asset.get("digest") is not None:
            if asset["digest"] != "sha256:" + want["sha256"]:
                raise ValueError("Release 上传哈希不符")
        else:
            with TemporaryDirectory(prefix="stats-upload-verify-") as temporary:
                if sha256(self._download_asset(asset, Path(temporary) / "asset")) != want["sha256"]:
                    raise ValueError("Release 上传哈希不符")
        return want

    def download(self, tag: str, name: str, dest: Path) -> Path:
        validate_tag(tag)
        validate_name(name)
        release = self._release(tag)
        asset = next((a for a in self._assets(release) if a["name"] == name), None)
        if asset is None:
            raise ValueError(f"Release asset 缺失：{tag}/{name}")
        return self._download_asset(asset, dest)


def publish_raw(raw_root, out, *, release, tag, run_id):
    """ZIP originals and sidecars; an empty source produces no archive or checksum."""
    validate_tag(tag)
    validate_name(run_id)
    root, out = Path(raw_root), Path(out)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("原件目录无效")
    if out.resolve().is_relative_to(root.resolve()):
        raise ValueError("归档输出路径不能位于原件目录内")
    files = []
    for source in sorted(root.rglob("*")):
        if source.is_symlink() or not source.resolve().is_relative_to(root.resolve()):
            raise ValueError("原件不允许符号链接或越界路径")
        if source.is_file():
            relative = source.relative_to(root).as_posix()
            if "\\" in relative:
                raise ValueError("原件路径不允许反斜杠")
            files.append((source, relative))
    if not files:
        return None
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"stats-raw-{run_id}.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source, relative in files:
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            with source.open("rb") as incoming, archive.open(info, "w") as outgoing:
                while block := incoming.read(1024 * 1024):
                    outgoing.write(block)
    asset = upload_verified(release, tag, path)
    asset["checksum_asset"] = upload_checksums(release, tag, out, run_id, [asset], kind="raw")
    return asset
