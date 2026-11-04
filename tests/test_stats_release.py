"""GitHub Release storage is immutable and never leaks authentication redirects."""

import hashlib
import json
import zipfile
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests
import responses

FIXTURE = Path(__file__).parents[1] / "data/fixtures/t32/release_errors.json"


def test_empty_checksums_do_not_create_or_upload_any_file(tmp_path):
    from zucai.stats_release import upload_checksums

    release = Mock()
    assert upload_checksums(release, "stats-raw-202611", tmp_path, "empty", []) is None
    assert list(tmp_path.iterdir()) == []
    release.upload.assert_not_called()


def test_empty_raw_directory_does_not_create_or_upload_any_asset(tmp_path):
    from zucai.stats_release import publish_raw

    raw, out = tmp_path / "raw", tmp_path / "out"
    (raw / "source/day").mkdir(parents=True)
    release = Mock()
    assert publish_raw(raw, out, release=release, tag="stats-raw-202611", run_id="empty") is None
    assert not out.exists() or list(out.iterdir()) == []
    release.upload.assert_not_called()


@pytest.mark.parametrize("case", json.loads(FIXTURE.read_text())["cases"])
@responses.activate
def test_upload_failure_reports_status_and_bounded_redacted_body(tmp_path, case):
    from zucai.cloud_apis import failure_reason
    from zucai.stats_release import ReleaseStore

    token = json.loads(FIXTURE.read_text())["token"]
    path = tmp_path / "rows.zst"
    path.write_bytes(FIXTURE.read_bytes())
    root = "https://api.github.com/repos/owner/repo/releases"
    upload = "https://uploads.github.com/repos/owner/repo/releases/1/assets"
    responses.get(root + "/tags/stats-raw-202611", json={"id": 1, "upload_url": upload})
    responses.get(root + "/1/assets", json=[])
    responses.post(upload, status=case["status"], body=case["body"])
    with pytest.raises(requests.HTTPError) as caught:
        ReleaseStore("owner/repo", token).upload("stats-raw-202611", path)
    expected = case["body"].replace(token, "[REDACTED]")[:200]
    expected = expected.replace("\r", "\\r").replace("\n", "\\n")
    reason = failure_reason(caught.value)
    assert reason == f"Release HTTP {case['status']}: {expected}"
    assert token not in reason and token not in str(caught.value)
    assert reason == str(caught.value)


def test_raw_zip_preserves_original_and_sidecar(tmp_path):
    from zucai.stats_release import publish_raw

    raw = tmp_path / "raw"
    (raw / "source/day").mkdir(parents=True)
    (raw / "source/day/original.json").write_bytes(b'{"raw":3}')
    (raw / "source/day/original.json.meta.json").write_bytes(b'{"sha256":"original"}')
    release = Mock()
    release.upload.side_effect = lambda tag, path: dict(
        tag=tag,
        name=path.name,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        size=path.stat().st_size,
    )
    descriptor = publish_raw(
        raw, tmp_path / "out", release=release, tag="stats-raw-202611", run_id="mock-1"
    )
    with zipfile.ZipFile(tmp_path / "out" / descriptor["name"]) as archive:
        assert archive.namelist() == [
            "source/day/original.json",
            "source/day/original.json.meta.json",
        ]
        assert archive.read("source/day/original.json") == b'{"raw":3}'
    assert descriptor["checksum_asset"]["name"] == "SHA256SUMS-raw-mock-1"


@responses.activate
def test_release_hash_reuse_and_collision(tmp_path):
    from zucai.stats_release import ReleaseStore

    path = tmp_path / "row.zst"
    path.write_bytes(b"original")
    sha = hashlib.sha256(b"original").hexdigest()
    url = "https://api.github.com/repos/owner/repo/releases/tags/stats-raw-202611"
    responses.get(
        url,
        json={
            "id": 1,
            "upload_url": "https://uploads.github.com/repos/owner/repo/releases/1/assets{?name,label}",
        },
    )
    responses.get(
        "https://api.github.com/repos/owner/repo/releases/1/assets",
        json=[{"id": 2, "name": "row.zst", "size": 8, "digest": "sha256:" + sha}],
    )
    store = ReleaseStore("owner/repo", "private-token")
    assert store.upload("stats-raw-202611", path)["sha256"] == sha
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="collision|冲突"):
        store.upload("stats-raw-202611", path)


@responses.activate
def test_signed_download_has_no_token(tmp_path):
    from zucai.stats_release import ReleaseStore

    responses.get(
        "https://api.github.com/repos/owner/repo/releases/tags/stats-raw-202611", json={"id": 1}
    )
    responses.get(
        "https://api.github.com/repos/owner/repo/releases/1/assets",
        json=[{"id": 2, "name": "rows.zst"}],
    )
    responses.get(
        "https://api.github.com/repos/owner/repo/releases/assets/2",
        status=302,
        headers={"Location": "https://release-assets.githubusercontent.com/signed"},
    )
    responses.get("https://release-assets.githubusercontent.com/signed", body=b"payload")
    store = ReleaseStore("https://github.com/owner/repo", "private-token")
    assert (
        store.download("stats-raw-202611", "rows.zst", tmp_path / "rows").read_bytes() == b"payload"
    )
    assert responses.calls[-1].request.headers.get("Authorization") is None
    assert responses.calls[0].request.headers["Authorization"] == "Bearer private-token"


@pytest.mark.parametrize(
    "repository",
    [
        "https://evil.test/owner/repo",
        "owner/../repo",
        "owner/repo/more",
        "https://u:p@github.com/owner/repo",
    ],
)
def test_reject_repository(repository):
    from zucai.stats_release import ReleaseStore

    with pytest.raises(ValueError):
        ReleaseStore(repository)


def test_reject_unsafe_tag_name_and_symlink(tmp_path):
    from zucai.stats_release import ReleaseStore, publish_raw

    store = ReleaseStore("owner/repo")
    with pytest.raises(ValueError):
        store.download("stats-raw-202613", "asset", tmp_path / "a")
    with pytest.raises(ValueError):
        store.download("stats-raw-202611", "../asset", tmp_path / "a")
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "link").symlink_to(tmp_path / "other")
    with pytest.raises(ValueError, match="符号|symlink"):
        publish_raw(raw, tmp_path / "out", release=Mock(), tag="stats-raw-202611", run_id="test")


def test_raw_output_cannot_recurse_into_source(tmp_path):
    from zucai.stats_release import publish_raw

    raw = tmp_path / "raw"
    raw.mkdir()
    with pytest.raises(ValueError, match="原件|路径"):
        publish_raw(raw, raw / "out", release=Mock(), tag="stats-raw-202611", run_id="safe")


@responses.activate
def test_create_and_upload_are_verified_with_api_digest(tmp_path):
    from zucai.stats_release import ReleaseStore

    path = tmp_path / "rows.zst"
    path.write_bytes(b"exact")
    sha = hashlib.sha256(b"exact").hexdigest()
    root = "https://api.github.com/repos/owner/repo/releases"
    responses.get(root + "/tags/stats-raw-202611", status=404)
    responses.post(
        root,
        json={
            "id": 1,
            "upload_url": "https://uploads.github.com/repos/owner/repo/releases/1/assets{?name,label}",
        },
        status=201,
    )
    responses.get(root + "/1/assets", json=[])
    responses.post(
        "https://uploads.github.com/repos/owner/repo/releases/1/assets",
        json={"id": 2, "name": "rows.zst", "size": 5, "digest": "sha256:" + sha},
        status=201,
    )
    result = ReleaseStore("owner/repo", "secret").upload("stats-raw-202611", path)
    assert result == dict(tag="stats-raw-202611", name="rows.zst", sha256=sha, size=5)
