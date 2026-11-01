import hashlib
import json
import traceback
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import requests
import responses
from requests.structures import CaseInsensitiveDict

from zucai.sources.base import RateLimiter, RawStore

FIXTURES = Path(__file__).resolve().parents[1] / "data/fixtures/t09"
SEASON = {"competition": "PL", "season": 2024}
WINDOW = {"competition": "PL,PD", "date_from": "2024-09-01", "date_to": "2024-09-04"}
SEASON_URL = "https://api.football-data.org/v4/competitions/PL/matches?season=2024"
WINDOW_URL = (
    "https://api.football-data.org/v4/matches?dateFrom=2024-09-01&dateTo=2024-09-04"
    "&competitions=PL,PD"
)


def fixture(name):
    return (FIXTURES / f"synthetic_{name}.json").read_bytes()


def changed(name, path, value):
    payload = json.loads(fixture(name))
    parent = payload
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value
    return json.dumps(payload).encode()


def feed(**kwargs):
    from zucai.sources.football_data_org import FootballDataOrg

    source = FootballDataOrg(**kwargs)
    source.limiter = RateLimiter(0)
    return source


class FixtureTransport:
    def __init__(self, *replies):
        self.replies = iter(replies)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        status, body, headers = reply
        response = requests.Response()
        response.status_code = status
        response.url = url
        response._content = body
        response.headers = CaseInsensitiveDict(headers)
        return response


@pytest.fixture
def dummy_key(monkeypatch):
    value = "offline-" + uuid4().hex
    monkeypatch.setenv("FOOTBALL_DATA_ORG_TOKEN", value)
    return value


def snapshots(root):
    return [p for p in root.rglob("*.json") if not p.name.endswith(".meta.json")]


def test_regular_time_results_identity_and_auditable_rejections_without_key(monkeypatch):
    monkeypatch.delenv("FOOTBALL_DATA_ORG_TOKEN", raising=False)
    source = feed()
    raw = fixture("season")
    games = {g.provider_match_id: g for g in source.parse(raw)}
    assert set(games) == {910001, 910002, 910003, 910004, 910007}
    regular = games[910001]
    assert (regular.provider, regular.competition_code, regular.league) == ("fdorg", "PL", "英超")
    assert (regular.home_provider_id, regular.home_raw) == (91001, "Example North")
    assert (regular.away_provider_id, regular.away_raw) == (91002, "Example South")
    assert regular.kickoff_utc == datetime(2024, 8, 31, 14, 30, tzinfo=UTC)
    assert (regular.status, regular.full, regular.half, regular.regular_time_basis) == (
        "FINISHED",
        (2, 1),
        (1, 0),
        "regular",
    )
    assert (games[910002].full, games[910002].half, games[910002].regular_time_basis) == (
        (1, 1),
        (0, 1),
        "derived",
    )
    assert (games[910003].full, games[910003].half) == ((2, 2), None)
    postponed = games[910004]
    assert (postponed.status, postponed.full, postponed.half, postponed.regular_time_basis) == (
        "POSTPONED",
        None,
        None,
        "missing",
    )
    assert [r["provider_match_id"] for r in source.rejected] == [910005, 910006]
    assert [r["row_number"] for r in source.rejected] == [5, 6]
    assert "AWARDED" in source.rejected[0]["reason"]
    assert "regularTime" in source.rejected[1]["reason"]
    assert all(r["raw_sha256"] == hashlib.sha256(raw).hexdigest() for r in source.rejected)


def test_window_preserves_raw_states_but_never_returns_a_partial_result():
    source = feed()
    source.parse(fixture("season"))
    games = source.parse(fixture("window"), **WINDOW)
    assert [g.status for g in games] == [
        "SCHEDULED",
        "TIMED",
        "IN_PLAY",
        "PAUSED",
        "SUSPENDED",
        "CANCELLED",
    ]
    assert {g.league for g in games} == {"英超", "西甲"}
    assert {(g.full, g.half, g.regular_time_basis) for g in games} == {(None, None, "missing")}
    assert source.rejected == []


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ((), None),
        (("id",), True),
        (("homeTeam",), []),
        (("homeTeam", "id"), "91001"),
        (("awayTeam", "id"), -1),
        (("awayTeam", "id"), 91001),
        (("homeTeam", "name"), ""),
        (("status",), ["sensitive-input"]),
        (("utcDate",), "sensitive-input"),
        (("utcDate",), "2024-08-31T14:30:00"),
        (("score", "duration"), {}),
        (("score", "fullTime", "home"), True),
        (("score", "halfTime", "away"), None),
        (("score", "halfTime", "home"), 3),
        (("competition", "code"), []),
    ],
)
def test_malformed_individual_rows_are_isolated_and_not_echoed(path, value):
    raw = changed("season", ("matches", 0, *path), value)
    source = feed()
    games = source.parse(raw, **SEASON)
    assert {g.provider_match_id for g in games} == {910002, 910003, 910004, 910007}
    assert len(source.rejected) == 3 and source.rejected[0]["row_number"] == 1
    assert "sensitive-input" not in str(source.rejected)


def test_known_offset_is_normalized_and_duplicate_provider_ids_are_all_refused():
    source = feed()
    raw = changed("season", ("matches", 0, "utcDate"), "2024-08-31T16:30:00+02:00")
    when = source.parse(raw)[0].kickoff_utc
    assert when.tzinfo is UTC and when.hour == 14
    duplicate = changed("season", ("matches", 1, "id"), 910001)
    assert 910001 not in {g.provider_match_id for g in source.parse(duplicate)}
    assert [r["row_number"] for r in source.rejected if r["provider_match_id"] == 910001] == [1, 2]


def test_unscoped_parse_isolates_unmapped_cup_competitions():
    raw = changed("window", ("matches", 0, "competition", "code"), "CL")
    source = feed()
    assert len(source.parse(raw)) == 5 and source.rejected[0]["provider_match_id"] == 920001
    assert "赛事" in source.rejected[0]["reason"]


@pytest.mark.parametrize(
    ("row", "match_id", "live_status"),
    [(1, 910002, "EXTRA_TIME"), (2, 910003, "PENALTY_SHOOTOUT")],
)
def test_live_extra_time_states_retained_without_a_result(row, match_id, live_status):
    raw = changed("season", ("matches", row, "status"), live_status)
    games = {g.provider_match_id: g for g in feed().parse(raw, **SEASON)}
    assert match_id in games
    game = games[match_id]
    assert (game.status, game.full, game.half, game.regular_time_basis) == (
        live_status,
        None,
        None,
        "missing",
    )


@pytest.mark.parametrize(
    ("name", "scope", "path", "value"),
    [
        ("season", SEASON, ("competition", "code"), "PD"),
        ("season", SEASON, ("matches", 0, "competition", "code"), "PD"),
        ("season", SEASON, ("filters", "season"), "2023"),
        ("season", SEASON, ("matches", 0, "season", "startDate"), "2023-08-12"),
        ("window", WINDOW, ("filters", "dateTo"), "2024-09-03"),
        ("window", WINDOW, ("matches", 0, "competition", "code"), "SA"),
        ("window", WINDOW, ("matches", 0, "utcDate"), "2024-09-04T00:00:00Z"),
        ("window", WINDOW, ("matches", 0, "utcDate"), "2024-08-31T23:59:59Z"),
        ("season", SEASON, ("resultSet", "count"), 8),
        ("season", {**SEASON, "status": "FINISHED"}, ("filters", "status"), "FINISHED"),
    ],
)
def test_wrong_response_scope_fails_live_and_replay_but_safe_raw_is_retained(
    name, scope, path, value, tmp_path, dummy_key
):
    raw = changed(name, path, value)
    source = feed(store=RawStore(tmp_path), session=FixtureTransport((200, raw, {})))
    with pytest.raises(ValueError):
        source.parse(raw, **scope)
    with pytest.raises(ValueError):
        source.fetch_raw(**scope)
    assert [p.read_bytes() for p in snapshots(tmp_path)] == [raw]


@pytest.mark.parametrize(
    "scope",
    [
        {"competition": "CL", "season": "2024"},
        {"competition": "PL/../CL", "season": "2024"},
        {"competition": "PL,PD", "season": "2024"},
        {"competition": "PL", "season": "2024&token=unsafe"},
        {"competition": "PL"},
        {"competition": "PL", "date_from": "2024-09-01"},
        {**WINDOW, "date_to": "2024-09-01"},
        {**WINDOW, "date_from": "2024-02-30"},
        {**WINDOW, "season": 2024},
        {**SEASON, "status": "invalid"},
    ],
)
def test_invalid_request_scope_is_refused_before_any_http(scope, dummy_key):
    transport = FixtureTransport()
    source = feed(session=transport)
    with pytest.raises(ValueError):
        source.fetch_raw(**scope)
    assert transport.calls == [] and source.request_count == 0


def test_credentials_are_required_only_for_fetch_and_never_persisted(monkeypatch, tmp_path):
    monkeypatch.delenv("FOOTBALL_DATA_ORG_TOKEN", raising=False)
    transport = FixtureTransport()
    source = feed(store=RawStore(tmp_path), session=transport)
    assert source.request_url(**SEASON, status="FINISHED") == SEASON_URL + "&status=FINISHED"
    assert source.request_url(**WINDOW) == WINDOW_URL
    with pytest.raises(ValueError, match="FOOTBALL_DATA_ORG_TOKEN"):
        source.fetch_raw(**SEASON)
    assert source.request_count == 0 and transport.calls == [] and snapshots(tmp_path) == []


def test_429_archived_then_one_retry_with_reset_and_per_request_auth(tmp_path, dummy_key):
    raw = fixture("window")
    transport = FixtureTransport(
        (
            429,
            fixture("http_error"),
            {"X-RequestCounter-Reset": "7", "X-RequestsAvailable": "0", "Other": dummy_key},
        ),
        (200, raw, {"x-requests-available-minute": "8", "X-RequestCounter-Reset": "bad"}),
    )
    waits = []
    source = feed(store=RawStore(tmp_path), session=transport, sleep=waits.append)
    path = source.fetch_raw(**WINDOW)
    assert waits == [7] and source.request_count == 2 and source.remaining_quota == 8
    assert [url for url, _ in transport.calls] == [WINDOW_URL, WINDOW_URL]
    for _, flags in transport.calls:
        assert flags["headers"]["X-Auth-Token"] == dummy_key and flags["allow_redirects"] is False
    assert path.is_relative_to(tmp_path / "football-data-org") and path.read_bytes() == raw
    assert source.store.load_metadata(path)["sha256"] == hashlib.sha256(raw).hexdigest()
    metadata = [source.store.load_metadata(p) for p in snapshots(tmp_path)]
    assert sorted(m["status_code"] for m in metadata) == [200, 429]
    assert {m["url"] for m in metadata} == {source.request_url(**WINDOW)}
    assert all(dummy_key.encode() not in p.read_bytes() for p in tmp_path.rglob("*.json"))
    times = {m["status_code"]: m["fetched_at_utc"] for m in metadata}
    assert source.rate_events == [
        {
            "status_code": 429,
            "fetched_at_utc": times[429],
            "headers": {"X-RequestCounter-Reset": 7, "X-RequestsAvailable": 0},
        },
        {
            "status_code": 200,
            "fetched_at_utc": times[200],
            "headers": {"x-requests-available-minute": 8},
        },
    ]


def test_second_429_ends_command_with_archives_and_no_third_request(tmp_path, dummy_key):
    from zucai.sources.football_data_org import FdorgHttpError

    reply = (
        429,
        fixture("http_error"),
        {"X-RequestCounter-Reset": "1", "X-RequestsAvailable": "0"},
    )
    waits = []
    source = feed(
        store=RawStore(tmp_path), session=FixtureTransport(reply, reply), sleep=waits.append
    )
    with pytest.raises(FdorgHttpError) as err:
        source.fetch_raw(**SEASON)
    assert err.value.status_code == 429 and len(snapshots(tmp_path)) == 2
    assert source.request_count == 2 and waits == [1] and source.remaining_quota == 0


@pytest.mark.parametrize("status", [302, 403, 500])
def test_default_transport_never_follows_redirects_or_hides_retries(tmp_path, dummy_key, status):
    from zucai.sources.football_data_org import FdorgHttpError

    source = feed(store=RawStore(tmp_path))
    with responses.RequestsMock(assert_all_requests_are_fired=False) as http:
        http.get(
            SEASON_URL,
            body=fixture("http_error"),
            status=status,
            headers={"Location": "https://outside.invalid/?private=" + dummy_key},
        )
        http.get(SEASON_URL, body=fixture("season"))
        with pytest.raises(FdorgHttpError) as err:
            source.fetch_raw(**SEASON)
        assert len(http.calls) == 1
    assert err.value.status_code == status and source.request_count == 1
    assert source.store.load_metadata(snapshots(tmp_path)[0])["url"] == SEASON_URL
    assert all(dummy_key.encode() not in p.read_bytes() for p in tmp_path.rglob("*.json"))
    assert dummy_key not in str(err.value)


@pytest.mark.parametrize("status", [200, 429, 500])
def test_a_reflected_credential_is_refused_before_archiving_or_retrying(
    tmp_path, dummy_key, status
):
    raw = changed("http_error", ("message",), "reflected " + dummy_key)
    source = feed(store=RawStore(tmp_path), session=FixtureTransport((status, raw, {})))
    with pytest.raises(ValueError) as err:
        source.fetch_raw(**SEASON)
    assert source.request_count == 1 and list(tmp_path.rglob("*")) == []
    assert dummy_key not in "".join(traceback.format_exception(err.value))


def test_transport_exception_discards_sensitive_details(tmp_path, dummy_key):
    problem = requests.ConnectionError("reflected header " + dummy_key)
    source = feed(store=RawStore(tmp_path), session=FixtureTransport(problem))
    with pytest.raises(ValueError) as err:
        source.fetch_raw(**SEASON)
    assert source.request_count == 1 and dummy_key not in "".join(
        traceback.format_exception(err.value)
    )
    assert list(tmp_path.rglob("*")) == []


@pytest.mark.parametrize(
    ("name", "status", "marker"),
    [
        ("season", 200, b"Example North"),
        ("http_error", 403, b"message"),
    ],
)
def test_json_escape_cannot_hide_a_credential_in_a_value_or_key(
    tmp_path, dummy_key, name, status, marker
):
    escaped = "".join(f"\\u{ord(c):04x}" for c in dummy_key).encode("ascii")
    raw = fixture(name).replace(marker, escaped, 1)
    assert escaped in raw and dummy_key.encode() not in raw
    source = feed(store=RawStore(tmp_path), session=FixtureTransport((status, raw, {})))
    with pytest.raises(ValueError) as err:
        source.fetch_raw(**SEASON)
    assert not list(tmp_path.rglob("*"))
    assert dummy_key not in "".join(traceback.format_exception(err.value))


@pytest.mark.parametrize("reset", ["999999999", "300000000000", "malformed"])
def test_unusable_reset_fails_without_a_blind_wait_or_early_retry(tmp_path, dummy_key, reset):
    from zucai.sources.football_data_org import FdorgHttpError

    waits = []
    transport = FixtureTransport((429, fixture("http_error"), {"X-RequestCounter-Reset": reset}))
    source = feed(store=RawStore(tmp_path), session=transport, sleep=waits.append)
    with pytest.raises(FdorgHttpError, match="429") as err:
        source.fetch_raw(**SEASON)
    assert err.value.status_code == 429 and waits == []
    assert source.request_count == 1 and len(snapshots(tmp_path)) == 1


@pytest.mark.parametrize(("value", "expected"), [("7", 7), ("untrusted-value", None), ("-1", None)])
def test_reported_quota_accepts_numeric_minute_header_alias_only(
    tmp_path, dummy_key, value, expected
):
    source = feed(
        store=RawStore(tmp_path),
        session=FixtureTransport((200, fixture("season"), {"X-RequestsAvailable-Minute": value})),
    )
    source.fetch_raw(**SEASON)
    assert source.remaining_quota == expected


def test_an_unparseable_row_without_season_metadata_cannot_discard_the_valid_batch():
    doc = json.loads(fixture("season"))
    del doc["filters"]
    doc["matches"][0] = {"id": 910001, "utcDate": "bad"}
    source = feed()
    games = source.parse(json.dumps(doc).encode(), **SEASON)
    assert {g.provider_match_id for g in games} == {910002, 910003, 910004, 910007}
    assert source.rejected[0]["provider_match_id"] == 910001


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("competitions", "PL,SA"),
        ("status", ["FINISHED"]),
    ],
)
def test_contradictory_echoed_filters_in_a_date_response_fail_replay(field, value):
    source = feed()
    with pytest.raises(ValueError):
        source.parse(changed("window", ("filters", field), value), **WINDOW)


@pytest.mark.parametrize("echoed", ["2014,2021", [2021, 2014]])
def test_numeric_competition_filter_echo_must_cover_the_exact_requested_codes(echoed):
    source = feed()
    assert len(source.parse(changed("window", ("filters", "competitions"), echoed), **WINDOW)) == 6
    with pytest.raises(ValueError, match="competitions"):
        source.parse(changed("window", ("filters", "competitions"), "2021,2019"), **WINDOW)
