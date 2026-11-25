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


@responses.activate
def test_fifty_downloads_share_one_paginated_release_inventory(tmp_path):
    from zucai.stats_release import ReleaseStore

    root = "https://api.github.com/repos/owner/repo/releases"
    responses.get(root + "/tags/stats-raw-202611", json={"id": 1})
    inventory = [dict(id=n, name=f"shard-{n}.zst") for n in range(1, 151)]
    for page, assets in enumerate((inventory[:100], inventory[100:]), start=1):
        responses.get(
            root + "/1/assets",
            match=[responses.matchers.query_param_matcher({"per_page": 100, "page": page})],
            json=assets,
        )
    selected = [*range(1, 26), *range(101, 126)]
    for n in selected:
        responses.get(f"{root}/assets/{n}", body=f"payload {n}")

    store = ReleaseStore("owner/repo", "private-token")
    for n in selected:
        dest = tmp_path / f"shard-{n}.zst"
        store.download("stats-raw-202611", dest.name, dest)
        assert dest.read_text() == f"payload {n}"

    assert sum("/tags/" in c.request.path_url for c in responses.calls) == 1
    listings = [c.request.path_url for c in responses.calls if "/1/assets?" in c.request.path_url]
    assert listings == [
        "/repos/owner/repo/releases/1/assets?per_page=100&page=1",
        "/repos/owner/repo/releases/1/assets?per_page=100&page=2",
    ]


@responses.activate
def test_verified_upload_updates_inventory_used_for_downloads_and_collisions(tmp_path):
    from zucai.stats_release import ReleaseStore

    root = "https://api.github.com/repos/owner/repo/releases"
    upload = "https://uploads.github.com/repos/owner/repo/releases/1/assets"
    path = tmp_path / "rows.zst"
    path.write_bytes(b"payload")
    sha = hashlib.sha256(b"payload").hexdigest()
    responses.get(root + "/tags/stats-raw-202611", json={"id": 1, "upload_url": upload})
    responses.get(root + "/1/assets", json=[])
    responses.post(
        upload, json={"id": 2, "name": "rows.zst", "size": 7, "digest": "sha256:" + sha}, status=201
    )
    responses.get(root + "/assets/2", body=b"payload")

    store = ReleaseStore("owner/repo", "private-token")
    assert store.upload("stats-raw-202611", path)["sha256"] == sha
    downloaded = store.download("stats-raw-202611", "rows.zst", tmp_path / "downloaded")
    assert downloaded.read_bytes() == b"payload"
    assert store.upload("stats-raw-202611", path)["sha256"] == sha
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="collision"):
        store.upload("stats-raw-202611", path)
    assert [c.request.method for c in responses.calls] == ["GET", "GET", "POST", "GET"]


@responses.activate
def test_upload_waits_for_rate_reset_and_resends_the_complete_file(tmp_path, monkeypatch):
    from zucai.stats_release import ReleaseStore

    clock = [1_800_000_000]
    waits, sent = [], []

    def sleep(seconds):
        waits.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr("zucai.github_api.time.time", lambda: clock[0])
    monkeypatch.setattr("zucai.github_api.time.sleep", sleep)
    monkeypatch.setenv("ZUCAI_GITHUB_DEADLINE", str(clock[0] + 60))
    token = "offline-release-token-placeholder"
    path = tmp_path / "rows.zst"
    path.write_bytes(b"payload")
    sha = hashlib.sha256(b"payload").hexdigest()
    upload = _release_with([[]])

    def limited(request):
        body = request.body
        sent.append(body if isinstance(body, bytes) else body.read())
        return (
            403,
            {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1800000005"},
            json.dumps({"message": f"API rate limit exceeded for installation {token}"}),
        )

    def uploaded(request):
        body = request.body
        sent.append(body if isinstance(body, bytes) else body.read())
        return (
            201,
            {},
            json.dumps({"id": 2, "name": path.name, "size": 7, "digest": "sha256:" + sha}),
        )

    responses.add_callback(responses.POST, upload, callback=limited)
    responses.add_callback(responses.POST, upload, callback=uploaded)
    result = ReleaseStore("owner/repo", token).upload("stats-raw-202611", path)
    assert result["sha256"] == sha
    assert sent == [b"payload", b"payload"]
    assert waits == [6]
    assert [c.request.method for c in responses.calls] == ["GET", "GET", "POST", "POST"]


@pytest.mark.parametrize("transport_error", [False, True])
@responses.activate
def test_signed_download_failure_omits_cdn_credentials(tmp_path, monkeypatch, transport_error):
    from zucai.stats_release import ReleaseStore

    _quiet(monkeypatch)
    signed = "https://release-assets.githubusercontent.com/signed?token=cdn-access-placeholder"
    root = "https://api.github.com/repos/owner/repo/releases"
    responses.get(root + "/tags/stats-raw-202611", json={"id": 1})
    responses.get(root + "/1/assets", json=[{"id": 2, "name": "rows.zst"}])
    responses.get(root + "/assets/2", status=302, headers={"Location": signed})
    if transport_error:
        responses.get(signed, body=requests.ConnectionError(f"failed to open {signed}"))
        error_type = requests.ConnectionError
    else:
        responses.get(signed, status=403, body=f"signature failed for {signed}")
        error_type = requests.HTTPError
    with pytest.raises(error_type) as caught:
        ReleaseStore("owner/repo", "private-token").download(
            "stats-raw-202611", "rows.zst", tmp_path / "rows"
        )
    assert "cdn-access-placeholder" not in str(caught.value)
    assert "private-token" not in str(caught.value)
    assert all(
        c.request.headers.get("Authorization") is None
        for c in responses.calls
        if c.request.url == signed
    )


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


def _quiet(monkeypatch):
    waits = []
    monkeypatch.setattr("zucai.stats_release.time.sleep", waits.append)
    return waits


@responses.activate
def test_a_dropped_read_is_asked_again_and_an_http_answer_is_not(tmp_path, monkeypatch):
    from zucai.stats_release import RETRY_WAITS, ReleaseHttpError, ReleaseStore

    waits = _quiet(monkeypatch)
    tag = "https://api.github.com/repos/owner/repo/releases/tags/stats-raw-202611"
    responses.get(tag, body=requests.ConnectionError("reset"))
    responses.get(tag, body=requests.ReadTimeout("slow"))
    responses.get(tag, json={"id": 1})
    responses.get("https://api.github.com/repos/owner/repo/releases/1/assets", json=[])
    store = ReleaseStore("owner/repo", "private-token")
    with pytest.raises(ValueError, match="缺失"):
        store.download("stats-raw-202611", "rows.zst", tmp_path / "rows")
    assert waits == list(RETRY_WAITS[:2])
    # A fresh run sees the new server state, and an ordinary HTTP failure is final.
    waits.clear()
    responses.replace(responses.GET, tag, status=503, body="busy")
    before = len(responses.calls)
    with pytest.raises(ReleaseHttpError):
        ReleaseStore("owner/repo", "private-token").download(
            "stats-raw-202611", "rows.zst", tmp_path / "rows"
        )
    assert len(responses.calls) == before + 1 and waits == []


@responses.activate
def test_a_read_that_keeps_failing_gives_up_with_the_network_error(tmp_path, monkeypatch):
    from zucai.cloud_apis import failure_reason
    from zucai.stats_release import RETRY_WAITS, ReleaseStore

    waits = _quiet(monkeypatch)
    tag = "https://api.github.com/repos/owner/repo/releases/tags/stats-raw-202611"
    responses.get(tag, body=requests.ConnectionError("reset"))
    with pytest.raises(requests.ConnectionError) as caught:
        ReleaseStore("owner/repo", "t").download("stats-raw-202611", "rows.zst", tmp_path / "r")
    assert waits == list(RETRY_WAITS) and len(responses.calls) == len(RETRY_WAITS) + 1
    assert failure_reason(caught.value) == "网络请求失败"


def _release_with(assets):
    root = "https://api.github.com/repos/owner/repo/releases"
    upload = "https://uploads.github.com/repos/owner/repo/releases/1/assets"
    responses.get(root + "/tags/stats-raw-202611", json={"id": 1, "upload_url": upload})
    for listing in assets:
        responses.get(root + "/1/assets", json=listing)
    return upload


@responses.activate
def test_an_interrupted_upload_is_sent_again_from_the_start(tmp_path, monkeypatch):
    from zucai.stats_release import RETRY_WAITS, ReleaseStore

    waits = _quiet(monkeypatch)
    path = tmp_path / "rows.zst"
    path.write_bytes(b"payload")
    sha = hashlib.sha256(b"payload").hexdigest()
    upload = _release_with([[], []])
    responses.post(upload, body=requests.ConnectionError("reset"))
    responses.post(upload, json={"name": "rows.zst", "size": 7, "digest": "sha256:" + sha})
    got = ReleaseStore("owner/repo", "t").upload("stats-raw-202611", path)
    assert got == dict(tag="stats-raw-202611", name="rows.zst", sha256=sha, size=7)
    posts = [c for c in responses.calls if c.request.method == "POST"]
    # The second attempt sends the whole file again, not the tail of a consumed stream.
    assert len(posts) == 2 and waits == [RETRY_WAITS[0]]
    body = posts[1].request.body
    assert (body if isinstance(body, bytes) else body.read()) == b"payload"


@responses.activate
def test_an_upload_that_arrived_before_the_drop_is_recognised_not_resent(tmp_path, monkeypatch):
    from zucai.stats_release import ReleaseStore

    _quiet(monkeypatch)
    path = tmp_path / "rows.zst"
    path.write_bytes(b"payload")
    sha = hashlib.sha256(b"payload").hexdigest()
    arrived = [{"id": 2, "name": "rows.zst", "size": 7, "digest": "sha256:" + sha}]
    upload = _release_with([[], arrived])
    responses.post(upload, body=requests.ReadTimeout("no receipt"))
    assert ReleaseStore("owner/repo", "t").upload("stats-raw-202611", path)["sha256"] == sha
    assert sum(c.request.method == "POST" for c in responses.calls) == 1


@responses.activate
def test_a_half_arrived_upload_stays_a_refusal_and_nothing_is_deleted(tmp_path, monkeypatch):
    from zucai.stats_release import ReleaseStore

    _quiet(monkeypatch)
    path = tmp_path / "rows.zst"
    path.write_bytes(b"payload")
    broken = [{"id": 2, "name": "rows.zst", "size": 3, "digest": "sha256:" + "0" * 64}]
    upload = _release_with([[], broken])
    responses.post(upload, body=requests.ConnectionError("reset"))
    with pytest.raises(ValueError, match="collision"):
        ReleaseStore("owner/repo", "t").upload("stats-raw-202611", path)
    assert {c.request.method for c in responses.calls} == {"GET", "POST"}


@responses.activate
def test_an_upload_that_keeps_failing_gives_up_with_the_network_error(tmp_path, monkeypatch):
    from zucai.cloud_apis import failure_reason
    from zucai.stats_release import RETRY_WAITS, ReleaseStore

    waits = _quiet(monkeypatch)
    path = tmp_path / "rows.zst"
    path.write_bytes(b"payload")
    upload = _release_with([[]])
    responses.post(upload, body=requests.ConnectionError("reset"))
    with pytest.raises(requests.ConnectionError) as caught:
        ReleaseStore("owner/repo", "t").upload("stats-raw-202611", path)
    assert waits == list(RETRY_WAITS) and failure_reason(caught.value) == "网络请求失败"
    assert sum(c.request.method == "POST" for c in responses.calls) == len(RETRY_WAITS) + 1


@responses.activate
def test_a_download_cut_off_half_way_starts_again(tmp_path, monkeypatch):
    from zucai.stats_release import RETRY_WAITS, ReleaseStore

    waits = _quiet(monkeypatch)
    root = "https://api.github.com/repos/owner/repo/releases"
    responses.get(root + "/tags/stats-raw-202611", json={"id": 1})
    responses.get(root + "/1/assets", json=[{"id": 2, "name": "rows.zst"}])
    responses.get(root + "/assets/2", body=requests.exceptions.ChunkedEncodingError("cut"))
    responses.get(root + "/assets/2", body=b"payload")
    store = ReleaseStore("owner/repo", "t")
    assert store.download("stats-raw-202611", "rows.zst", tmp_path / "r").read_bytes() == b"payload"
    assert waits == [RETRY_WAITS[0]]
