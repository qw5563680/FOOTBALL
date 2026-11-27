"""The T98 backup reuses a current root capture and reads schedule attributes only."""

from dataclasses import asdict
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
import responses

from zucai.sources import base
from zucai.sources.base import RawStore
from zucai.sources.c500 import URL, C500JcRoot, C500Sfc, parse_jc_fixtures
from zucai.sources.sfc import _collect_500_bridge

RAW = (Path(__file__).resolve().parents[1] / "data/fixtures/c500_root_20261102.html").read_bytes()


def test_archived_root_yields_only_live_jc_fixture_attributes():
    fixtures = parse_jc_fixtures(RAW)
    assert [row.match_id for row in fixtures] == [f"jc:{i}" for i in range(2042438, 2042447)]
    assert asdict(fixtures[0]) == {
        "match_id": "jc:2042438",
        "provider_fixture_id": "1489072",
        "jc_num": "周一001",
        "sales_date": date(2026, 11, 2),
        "league": "芬兰超级联赛",
        "home": "国际图尔",
        "away": "赫尔辛基火花",
        "home_id": "1327",
        "away_id": "2687",
        "kickoff_utc": datetime(2026, 11, 2, 16, tzinfo=UTC),
        "problem": None,
    }
    # Statistics and price markup can change without affecting a schedule-only parse.
    altered = RAW.replace(b"data-homesxname=", b"data-had='987.123' data-homesxname=")
    assert parse_jc_fixtures(altered) == fixtures


def test_missing_schedule_fields_are_retained_for_audit_under_the_official_id():
    altered = RAW.replace(b'data-homeid="1327"', b'data-homeid=""').replace(
        b'data-matchtime="00:00"', b'data-matchtime="missing"'
    )
    # Attribute separators in the captured page are part of the actual sample format.
    assert altered != RAW
    row = parse_jc_fixtures(altered)[0]
    assert row.match_id == "jc:2042438"
    assert row.home_id == "" and row.kickoff_utc is None


@pytest.mark.parametrize("raw", [b"", b"<html><title>non-schedule site</title></html>"])
def test_non_schedule_response_fails_as_bad_root_evidence(raw):
    with pytest.raises(ValueError, match="500竞彩根页"):
        parse_jc_fixtures(raw)


@responses.activate
def test_sfc_bridge_and_jc_fallback_use_one_capture(tmp_path):
    responses.get(URL, body=RAW, status=200)
    source = C500Sfc(store=RawStore(tmp_path / "raw"))
    root = C500JcRoot()
    try:
        assert not root.attempted
        bridges, observed = _collect_500_bridge(source, jc_root=root)
        captured, meta = root.snapshot()
        assert root.attempted and len(responses.calls) == 1
        assert responses.calls[0].request.url == URL
        assert captured == RAW
        assert meta["raw_sha256"] == sha256(RAW).hexdigest()
        assert root.fixtures() == parse_jc_fixtures(RAW)
        assert datetime.fromisoformat(meta["fetched_at_utc"]) == observed
        assert all(item["provenance"]["raw_sha256"] == meta["raw_sha256"] for item in bridges)
    finally:
        source.session.close()


@responses.activate
def test_standalone_failure_is_cached_and_is_not_retried():
    responses.get(URL, status=503)
    root = C500JcRoot()
    for _ in range(2):
        with pytest.raises(requests.HTTPError, match="503"):
            root.snapshot()
    assert root.attempted and len(responses.calls) == 1


@responses.activate
def test_standalone_root_respects_the_shared_limiter_and_creates_no_market_raw_file(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    clock = [0.0]
    monkeypatch.setattr(
        base,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock[0],
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        ),
    )
    responses.get(URL, status=200, body=RAW)
    root = C500JcRoot()
    root.limiter.wait()  # previous scheduled request to the same 500 host
    captured, meta = root.snapshot()
    assert captured == RAW and meta["raw_sha256"] == sha256(RAW).hexdigest()
    assert len(root.fixtures()) == 9 and len(responses.calls) == 1
    assert clock[0] == 1.0
    assert not tuple(tmp_path.iterdir())


@responses.activate
def test_standalone_root_does_not_follow_redirects():
    responses.get(URL, status=302, headers={"Location": URL + "?unverified=1"})
    root = C500JcRoot()
    with pytest.raises(ValueError, match="重定向"):
        root.snapshot()
    assert len(responses.calls) == 1 and responses.calls[0].request.url == URL
