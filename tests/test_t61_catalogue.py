"""Offline catalogue contracts: metadata is evidence, never account entitlement."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import requests
import responses

FIXTURES = Path(__file__).resolve().parents[1] / "data/fixtures/t61"


def test_apif_metadata_does_not_grant_season_access():
    from zucai.cloud_catalogue import parse_apif_catalogue

    raw = (FIXTURES / "catalogue-apif-synthetic.json").read_bytes()
    parsed = parse_apif_catalogue(raw)
    assert parsed["version"] == 1 and parsed["provider"] == "apif"
    leagues = {row["id"]: row for row in parsed["leagues"]}
    assert set(leagues) == {"3", "39"}
    assert leagues["39"]["name"] == "Synthetic Premier League"
    assert leagues["39"]["country"] == "Synthetic England"
    assert {season["year"] for season in leagues["39"]["seasons"]} == {2024, 2025}
    for season in leagues["39"]["seasons"]:
        assert season["access"] == "unverified"
        assert season["fixtures"] is None and season["results"] is None
    assert leagues["3"]["seasons"][0]["start_date"] is None


def test_fdorg_season_count_is_not_an_enumeration_or_entitlement():
    from zucai.cloud_catalogue import parse_fdorg_catalogue

    raw = (FIXTURES / "catalogue-fdorg-synthetic.json").read_bytes()
    parsed = parse_fdorg_catalogue(raw)
    assert parsed["version"] == 1 and parsed["provider"] == "fdorg"
    leagues = {row["code"]: row for row in parsed["leagues"]}
    assert leagues["PL"]["id"] == "2021"
    assert leagues["PL"]["number_of_available_seasons"] == 35
    assert leagues["PL"]["seasons"] == [
        {
            "year": 2025,
            "start_date": "2025-08-01",
            "end_date": "2026-05-31",
            "fixtures": None,
            "results": None,
            "access": "unverified",
        }
    ]
    assert leagues["CL"]["seasons"] == []


def _document(provider):
    return json.loads((FIXTURES / f"catalogue-{provider}-synthetic.json").read_bytes())


def _parse(provider, document):
    from zucai import cloud_catalogue

    return getattr(cloud_catalogue, f"parse_{provider}_catalogue")(
        json.dumps(document).encode("utf-8")
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("get", "fixtures"),
        ("parameters", {"id": 39}),
        ("results", 1),
        ("results", True),
        ("response", {}),
        ("errors", {"access": "Synthetic refusal"}),
        ("paging", {"current": 1, "total": 2}),
        ("paging", {"current": 2, "total": 2}),
        ("paging", {"current": True, "total": 1}),
        ("paging", {}),
    ],
)
def test_apif_rejects_partial_or_misdirected_catalogue(field, value):
    doc = _document("apif")
    doc[field] = value
    with pytest.raises(ValueError):
        _parse("apif", doc)


@pytest.mark.parametrize("field", ["get", "errors", "results", "paging", "response"])
def test_apif_required_envelope_fields(field):
    doc = _document("apif")
    doc.pop(field)
    with pytest.raises(ValueError):
        _parse("apif", doc)


@pytest.mark.parametrize("year", [True, "2025", 0, 1899, 10000])
def test_apif_rejects_invalid_year(year):
    doc = _document("apif")
    doc["response"][0]["seasons"][0]["year"] = year
    with pytest.raises(ValueError):
        _parse("apif", doc)


@pytest.mark.parametrize("provider", ["apif", "fdorg"])
def test_duplicate_provider_ids_rejected(provider):
    doc = _document(provider)
    if provider == "apif":
        doc["response"][1]["league"]["id"] = doc["response"][0]["league"]["id"]
    else:
        doc["competitions"][1]["id"] = doc["competitions"][0]["id"]
    with pytest.raises(ValueError):
        _parse(provider, doc)


@pytest.mark.parametrize("mutation", ["league", "country", "seasons", "season", "duplicate-year"])
def test_apif_malformed_rows_do_not_silently_disappear(mutation):
    doc = _document("apif")
    row = doc["response"][0]
    if mutation in {"league", "country", "seasons"}:
        row.pop(mutation)
    elif mutation == "season":
        row["seasons"][0] = None
    else:
        row["seasons"][1]["year"] = row["seasons"][0]["year"]
    with pytest.raises(ValueError):
        _parse("apif", doc)


@pytest.mark.parametrize(
    "mutation",
    [
        "count",
        "bool-count",
        "no-count",
        "missing-name",
        "bad-row",
        "bad-season",
        "negative-seasons",
        "bad-id",
        "duplicate-code",
    ],
)
def test_fdorg_counts_valid_competitions_and_rejects_malformed_rows(mutation):
    doc = _document("fdorg")
    row = doc["competitions"][0]
    if mutation == "count":
        doc["count"] = 3
    elif mutation == "bool-count":
        doc["count"] = True
    elif mutation == "no-count":
        doc.pop("count")
    elif mutation == "missing-name":
        row.pop("name")
    elif mutation == "bad-row":
        doc["competitions"][0] = None
    elif mutation == "bad-season":
        row["currentSeason"]["startDate"] = "unknown"
    elif mutation == "negative-seasons":
        row["numberOfAvailableSeasons"] = -1
    elif mutation == "bad-id":
        row["id"] = True
    else:
        doc["competitions"][1]["code"] = row["code"]
    with pytest.raises(ValueError):
        _parse("fdorg", doc)


@pytest.mark.parametrize("provider", ["apif", "fdorg"])
def test_parsers_reject_credential_fields_without_runtime_secrets(provider, monkeypatch):
    monkeypatch.delenv("API_FOOTBALL_KEY", raising=False)
    monkeypatch.delenv("FOOTBALL_DATA_ORG_TOKEN", raising=False)
    cases = json.loads((FIXTURES / "catalogue-errors-synthetic.json").read_bytes())
    for key in cases["unsafe_keys"]:
        doc = _document(provider)
        doc["nested"] = [{key: "synthetic-only"}]
        with pytest.raises(ValueError):
            _parse(provider, doc)


@pytest.mark.parametrize("provider", ["apif", "fdorg"])
def test_parsers_reject_non_json_and_duplicate_fields(provider):
    from zucai import cloud_catalogue

    parser = getattr(cloud_catalogue, f"parse_{provider}_catalogue")
    cases = json.loads((FIXTURES / "catalogue-errors-synthetic.json").read_bytes())
    with pytest.raises(ValueError):
        parser(cases["invalid_json"].encode())
    raw = (FIXTURES / f"catalogue-{provider}-synthetic.json").read_bytes()
    with pytest.raises(ValueError):
        parser(b'{"count":2,"count":2,' + raw[1:])


APIF_URL = "https://v3.football.api-sports.io/leagues"
FDORG_URL = "https://api.football-data.org/v4/competitions"
ERRORS = json.loads((FIXTURES / "catalogue-errors-synthetic.json").read_bytes())


@pytest.fixture
def actions(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "github-hosted")
    monkeypatch.setenv("API_FOOTBALL_KEY", "synthetic-apif-key")
    monkeypatch.setenv("FOOTBALL_DATA_ORG_TOKEN", "synthetic-fdorg-token")
    monkeypatch.delenv("ZUCAI_APIF_DAILY_CAP", raising=False)
    monkeypatch.setattr("zucai.sources.base.RateLimiter.wait", lambda self: None)


def _responses(http, *, apif=None, fdorg=None, apif_status=200, fdorg_status=200):
    http.get(
        APIF_URL,
        body=(FIXTURES / "catalogue-apif-synthetic.json").read_bytes() if apif is None else apif,
        status=apif_status,
        headers={"x-ratelimit-requests-limit": "7500", "x-ratelimit-requests-remaining": "7498"},
    )
    http.get(
        FDORG_URL,
        body=(FIXTURES / "catalogue-fdorg-synthetic.json").read_bytes() if fdorg is None else fdorg,
        status=fdorg_status,
        headers={
            "X-Requests-Available-Minute": "9",
            "X-RequestCounter-Reset": "60",
            "Authorization": "synthetic-fdorg-token",
        },
    )


@pytest.mark.parametrize("event", [None, "schedule", "push", "workflow_dispatch"])
def test_catalogue_requires_real_manual_actions_guard(tmp_path, monkeypatch, event):
    from zucai.cloud_catalogue import collect_catalogue

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    if event is None:
        monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    else:
        monkeypatch.setenv("GITHUB_EVENT_NAME", event)
        if event != "workflow_dispatch":
            monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with pytest.raises(ValueError, match="workflow_dispatch"):
        collect_catalogue(tmp_path / "data/zucai.db", "guarded")
    assert not list(tmp_path.rglob("*"))


def test_collect_exactly_two_requests_and_hash_linked_immutable_evidence(
    tmp_path, actions, monkeypatch
):
    from zucai.cloud_catalogue import collect_catalogue

    database = tmp_path / "data/zucai.db"
    database.parent.mkdir()
    database.write_bytes(b"Database sentinel: must never be opened or mutated.")
    original = database.read_bytes()
    transport = requests.Session.get
    calls = []

    def get(session, url, **kwargs):
        calls.append((url, kwargs))
        return transport(session, url, **kwargs)

    monkeypatch.setattr(requests.Session, "get", get)
    before = datetime.now(UTC)
    with responses.RequestsMock() as http:
        _responses(http)
        report = collect_catalogue(database, "synthetic-success")
        assert [call.request.url for call in http.calls] == [APIF_URL, FDORG_URL]
    after = datetime.now(UTC)
    assert all(
        kwargs["timeout"] == 30 and kwargs["allow_redirects"] is False for _, kwargs in calls
    )
    assert database.read_bytes() == original
    assert report["status"] == "complete" and report["collection"] == "catalogue"
    assert report["requests"] == 2 and report["status_requests"] == 0 and report["errors"] == []
    folder = database.parent / "state/catalogue/synthetic-success"
    assert {path.name for path in folder.iterdir()} == {
        "apif-raw.json",
        "apif-meta.json",
        "apif.json",
        "fdorg-raw.json",
        "fdorg-meta.json",
        "fdorg.json",
        "fd.json",
        "summary.json",
    }
    assert json.loads((folder / "summary.json").read_bytes()) == report
    latest = json.loads((folder.parent / "latest.json").read_bytes())
    for provider in ("apif", "fdorg"):
        raw = (folder / f"{provider}-raw.json").read_bytes()
        assert raw == (FIXTURES / f"catalogue-{provider}-synthetic.json").read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        meta = json.loads((folder / f"{provider}-meta.json").read_bytes())
        assert meta["sha256"] == digest and meta["status_code"] == 200
        captured = datetime.fromisoformat(meta["fetched_at_utc"].replace("Z", "+00:00"))
        assert before <= captured <= after
        assert meta["url"] == {"apif": APIF_URL, "fdorg": FDORG_URL}[provider]
        assert report["providers"][provider]["raw_sha256"] == digest
        pointer = latest["providers"][provider]
        assert pointer["raw_sha256"] == digest
        assert pointer["path"] == f"state/catalogue/synthetic-success/{provider}.json"
        assert (
            pointer["sha256"]
            == hashlib.sha256((database.parent / pointer["path"]).read_bytes()).hexdigest()
        )
    fd = json.loads((folder / "fd.json").read_bytes())
    divisions = {row["id"]: row for row in fd["leagues"]}
    assert divisions["E0"]["enabled"] is True
    assert divisions["D2"]["enabled"] is False and divisions["D2"]["status"] == "unverified"
    assert all(row["seasons"] == [] for row in divisions.values())
    for name in report["outputs"]:
        assert (database.parent / name).is_file()
    quota = json.loads((database.parent / "state/api_football_quota.json").read_bytes())
    assert quota["used"] == 1 and quota["remaining"] == 7498
    for path in database.parent.rglob("*"):
        if path.is_file():
            assert b"synthetic-apif-key" not in path.read_bytes()
            assert b"synthetic-fdorg-token" not in path.read_bytes()


@pytest.mark.parametrize("provider", ["apif", "fdorg"])
@pytest.mark.parametrize("status", ERRORS["http_statuses"])
def test_http_error_preserves_safe_evidence_and_other_provider(tmp_path, actions, provider, status):
    from zucai.cloud_catalogue import collect_catalogue

    database = tmp_path / "data/zucai.db"
    kwargs = {
        provider: json.dumps(ERRORS[f"{provider}_error"]).encode(),
        f"{provider}_status": status,
    }
    with responses.RequestsMock() as http:
        _responses(http, **kwargs)
        report = collect_catalogue(database, "synthetic-http")
        assert len(http.calls) == 2
    folder = database.parent / "state/catalogue/synthetic-http"
    assert report["status"] == "partial" and report["requests"] == 2
    assert (folder / f"{provider}-raw.json").is_file()
    assert not (folder / f"{provider}.json").exists()
    assert str(status) in str(report["errors"])
    latest = json.loads((folder.parent / "latest.json").read_bytes())
    assert provider not in latest["providers"]
    assert ({"apif", "fdorg"} - {provider}) <= latest["providers"].keys()
    assert not database.exists()


@pytest.mark.parametrize("provider", ["apif", "fdorg"])
@pytest.mark.parametrize("case", ["unsafe", "unsafe_escaped", "non-json"])
def test_unsafe_bodies_never_enter_any_archive_or_summary(tmp_path, actions, provider, case):
    from zucai.cloud_catalogue import collect_catalogue

    body = ERRORS["invalid_json"] if case == "non-json" else ERRORS[f"{provider}_{case}"]
    database = tmp_path / "data/zucai.db"
    with responses.RequestsMock() as http:
        _responses(http, **{provider: body.encode()})
        report = collect_catalogue(database, "synthetic-unsafe")
        assert len(http.calls) == 2
    assert report["status"] == "partial"
    folder = database.parent / "state/catalogue/synthetic-unsafe"
    assert not (folder / f"{provider}-raw.json").exists()
    assert not (folder / f"{provider}.json").exists()
    for path in database.parent.rglob("*"):
        if path.is_file():
            contents = path.read_bytes()
            assert b"synthetic-apif-key" not in contents
            assert b"synthetic-fdorg-token" not in contents
    assert "synthetic-apif-key" not in str(report) and "synthetic-fdorg-token" not in str(report)


def test_partial_pagination_is_archived_but_never_replaces_valid_pointer(tmp_path, actions):
    from zucai.cloud_catalogue import collect_catalogue

    database = tmp_path / "data/zucai.db"
    with responses.RequestsMock() as http:
        _responses(http)
        collect_catalogue(database, "first")
    folder = database.parent / "state/catalogue"
    before = {p.name: p.read_bytes() for p in (folder / "first").iterdir()}
    pointer_before = json.loads((folder / "latest.json").read_bytes())["providers"]["apif"]
    doc = _document("apif")
    doc["paging"]["total"] = 2
    with responses.RequestsMock() as http:
        _responses(http, apif=json.dumps(doc).encode())
        report = collect_catalogue(database, "second")
        assert len(http.calls) == 2
    assert report["status"] == "partial"
    latest = json.loads((folder / "latest.json").read_bytes())
    assert latest["providers"]["apif"] == pointer_before
    assert latest["providers"]["fdorg"]["run_id"] == "second"
    assert before == {p.name: p.read_bytes() for p in (folder / "first").iterdir()}
    assert (folder / "second/apif-raw.json").exists() and not (folder / "second/apif.json").exists()
    with pytest.raises(ValueError):
        collect_catalogue(database, "first")
    assert before == {p.name: p.read_bytes() for p in (folder / "first").iterdir()}


@pytest.mark.parametrize("provider", ["apif", "fdorg"])
def test_missing_credentials_do_not_block_other_provider(tmp_path, actions, monkeypatch, provider):
    from zucai.cloud_catalogue import collect_catalogue

    monkeypatch.delenv({"apif": "API_FOOTBALL_KEY", "fdorg": "FOOTBALL_DATA_ORG_TOKEN"}[provider])
    database = tmp_path / "data/zucai.db"
    with responses.RequestsMock(assert_all_requests_are_fired=False) as http:
        _responses(http)
        report = collect_catalogue(database, "synthetic-missing")
        assert len(http.calls) == 1
    assert report["status"] == "partial" and report["requests"] == 1
    assert report["providers"][provider]["requests"] == 0


def test_catalogue_run_id_cannot_escape_evidence_folder(tmp_path, actions):
    from zucai.cloud_catalogue import collect_catalogue

    for name in ("../escape", "/absolute", "", ".", "..", "folder/name"):
        with pytest.raises(ValueError):
            collect_catalogue(tmp_path / "data/zucai.db", name)
    assert not list(tmp_path.rglob("*"))


def test_summary_discloses_unknown_entitlement_and_complete_evidence(
    tmp_path, actions, monkeypatch
):
    from zucai.cloud_catalogue import collect_catalogue, write_summary

    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    with responses.RequestsMock() as http:
        _responses(http)
        report = collect_catalogue(tmp_path / "data/zucai.db", "summary")
    write_summary(report)
    text = summary.read_text()
    assert "catalogue" in text and "unverified" in text and "2" in text
    assert "state/catalogue/summary/summary.json" in text
    assert "synthetic-apif-key" not in text and "synthetic-fdorg-token" not in text


@pytest.mark.parametrize("runner", [None, "self-hosted", "unknown"])
def test_hosted_only_guard_rejects_other_runner_environments(
    tmp_path, actions, monkeypatch, runner
):
    from zucai.cloud_catalogue import collect_catalogue

    if runner is None:
        monkeypatch.delenv("RUNNER_ENVIRONMENT", raising=False)
    else:
        monkeypatch.setenv("RUNNER_ENVIRONMENT", runner)
    with pytest.raises(ValueError, match="github-hosted"):
        collect_catalogue(tmp_path / "data/zucai.db", "hosted-only")
    assert not list(tmp_path.rglob("*"))


@pytest.mark.parametrize("provider", ["apif", "fdorg"])
def test_cross_provider_credential_echo_is_blocked_before_original_archive(
    tmp_path, actions, provider
):
    from zucai.cloud_catalogue import collect_catalogue

    other = "fdorg" if provider == "apif" else "apif"
    database = tmp_path / "data/zucai.db"
    with responses.RequestsMock() as http:
        _responses(http, **{provider: ERRORS[f"{other}_unsafe_escaped"].encode()})
        report = collect_catalogue(database, "cross-echo")
    assert report["status"] == "partial"
    for path in database.parent.rglob("*"):
        if path.is_file():
            decoded = path.read_text().replace("\\u0073", "s")
            assert "synthetic-apif-key" not in decoded
            assert "synthetic-fdorg-token" not in decoded
    assert not (database.parent / f"state/catalogue/cross-echo/{provider}-raw.json").exists()


def test_fd_catalogue_retains_only_explicit_reviewed_season_years(tmp_path, actions, monkeypatch):
    from zucai import cloud_catalogue
    from zucai.league_registry import REGISTRY_PATH

    doc = json.loads(REGISTRY_PATH.read_bytes())
    fixture = json.loads((FIXTURES / "catalogue-fd-seasons-synthetic.json").read_bytes())
    doc["providers"]["fd"]["E0"]["config"]["catalogue_seasons"] = fixture["catalogue_seasons"]
    doc["providers"]["fd"]["E0"]["evidence"].append(fixture["evidence"])
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps(doc))
    monkeypatch.setattr(cloud_catalogue, "REGISTRY_PATH", registry, raising=False)
    database = tmp_path / "data/zucai.db"
    with responses.RequestsMock() as http:
        _responses(http)
        report = cloud_catalogue.collect_catalogue(database, "fd-seasons")
    assert report["status"] == "complete"
    fd = json.loads((database.parent / "state/catalogue/fd-seasons/fd.json").read_bytes())
    divisions = {row["id"]: row for row in fd["leagues"]}
    assert [season["year"] for season in divisions["E0"]["seasons"]] == [2020, 2025]
    assert all(season["access"] == "unverified" for season in divisions["E0"]["seasons"])
    assert all(
        season["start_date"] is None and season["end_date"] is None
        for season in divisions["E0"]["seasons"]
    )
    assert divisions["E1"]["seasons"] == [] and divisions["D2"]["seasons"] == []
    assert fd["provenance"]["registry_sha256"] == hashlib.sha256(registry.read_bytes()).hexdigest()
    assert fixture["evidence"] in divisions["E0"]["season_evidence"]


def test_invalid_run_names_match_archive_validator_and_never_expose_keys(tmp_path, actions):
    from zucai.cloud_catalogue import collect_catalogue

    for run_id in ("has.dot", "r" * 101, "synthetic-apif-key", "synthetic-fdorg-token"):
        with pytest.raises(ValueError):
            collect_catalogue(tmp_path / "data/zucai.db", run_id)
    assert not list(tmp_path.rglob("*"))


def test_probe_rejects_catalogue_directory_symlink_before_requests(tmp_path, actions):
    from zucai.cloud_catalogue import collect_catalogue

    root = tmp_path / "data"
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "state").mkdir(parents=True)
    (root / "state/catalogue").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        collect_catalogue(root / "zucai.db", "symlink")
    assert not list(outside.iterdir())


def test_exhausted_apif_budget_still_collects_fdorg_once(tmp_path, actions, monkeypatch):
    from zucai.cloud_catalogue import collect_catalogue

    database = tmp_path / "data/zucai.db"
    monkeypatch.setenv("ZUCAI_APIF_DAILY_CAP", "1")
    with responses.RequestsMock() as http:
        _responses(http)
        collect_catalogue(database, "budget-first")
    with responses.RequestsMock(assert_all_requests_are_fired=False) as http:
        _responses(http)
        report = collect_catalogue(database, "budget-second")
        assert [call.request.url for call in http.calls] == [FDORG_URL]
    assert report["status"] == "partial" and report["requests"] == 1
    assert report["providers"]["apif"]["requests"] == 0
    assert report["providers"]["fdorg"]["status"] == "complete"
    latest = json.loads((database.parent / "state/catalogue/latest.json").read_bytes())
    assert latest["providers"]["apif"]["run_id"] == "budget-first"
    assert latest["providers"]["fdorg"]["run_id"] == "budget-second"


@pytest.mark.parametrize("provider", ["apif", "fdorg"])
def test_transport_failure_counts_attempt_and_redacts_exception(tmp_path, actions, provider):
    from zucai.cloud_catalogue import collect_catalogue

    url = {"apif": APIF_URL, "fdorg": FDORG_URL}[provider]
    other = "fdorg" if provider == "apif" else "apif"
    other_url = {"apif": APIF_URL, "fdorg": FDORG_URL}[other]
    with responses.RequestsMock() as http:
        http.get(url, body=requests.ConnectionError("synthetic-apif-key synthetic-fdorg-token"))
        http.get(other_url, body=(FIXTURES / f"catalogue-{other}-synthetic.json").read_bytes())
        report = collect_catalogue(tmp_path / "data/zucai.db", "transport")
        assert len(http.calls) == 2
    assert report["status"] == "partial" and report["requests"] == 2
    assert report["providers"][other]["status"] == "complete"
    assert "synthetic-apif-key" not in str(report) and "synthetic-fdorg-token" not in str(report)


@pytest.mark.parametrize("corruption", ["raw", "metadata", "parsed-provider"])
def test_old_pointer_with_broken_evidence_is_not_preserved(tmp_path, actions, corruption):
    from zucai.cloud_catalogue import collect_catalogue

    database = tmp_path / "data/zucai.db"
    with responses.RequestsMock() as http:
        _responses(http)
        collect_catalogue(database, "old-evidence")
    root = database.parent
    folder = root / "state/catalogue/old-evidence"
    if corruption == "raw":
        (folder / "apif-raw.json").write_bytes(b"modified")
    elif corruption == "metadata":
        meta = json.loads((folder / "apif-meta.json").read_bytes())
        meta["sha256"] = "0" * 64
        (folder / "apif-meta.json").write_text(json.dumps(meta))
    else:
        parsed = json.loads((folder / "apif.json").read_bytes())
        parsed["provider"] = "fdorg"
        (folder / "apif.json").write_text(json.dumps(parsed))
        latest = json.loads((folder.parent / "latest.json").read_bytes())
        latest["providers"]["apif"]["sha256"] = hashlib.sha256(
            (folder / "apif.json").read_bytes()
        ).hexdigest()
        (folder.parent / "latest.json").write_text(json.dumps(latest))
    with responses.RequestsMock() as http:
        _responses(http, apif=json.dumps(ERRORS["apif_error"]).encode(), apif_status=403)
        report = collect_catalogue(database, "new-evidence")
    latest = json.loads((root / "state/catalogue/latest.json").read_bytes())
    assert "apif" not in latest["providers"]
    assert report["status"] == "partial"


@pytest.mark.parametrize("mutation", ["missing-raw-hash", "invalid-provenance"])
def test_malformed_previous_pointer_cannot_abort_other_provider_collection(
    tmp_path, actions, mutation
):
    from zucai.cloud_catalogue import collect_catalogue

    database = tmp_path / "data/zucai.db"
    with responses.RequestsMock() as http:
        _responses(http)
        collect_catalogue(database, "prior-malformed")
    folder = database.parent / "state/catalogue/prior-malformed"
    latest = json.loads((folder.parent / "latest.json").read_bytes())
    parsed = json.loads((folder / "apif.json").read_bytes())
    if mutation == "missing-raw-hash":
        latest["providers"]["apif"].pop("raw_sha256")
        parsed["provenance"].pop("raw_sha256")
    else:
        parsed["provenance"] = []
    (folder / "apif.json").write_text(json.dumps(parsed))
    latest["providers"]["apif"]["sha256"] = hashlib.sha256(
        (folder / "apif.json").read_bytes()
    ).hexdigest()
    (folder.parent / "latest.json").write_text(json.dumps(latest))
    with responses.RequestsMock() as http:
        _responses(http)
        report = collect_catalogue(database, "after-malformed")
        assert len(http.calls) == 2
    assert report["status"] == "partial"
    assert all(
        report["providers"][provider]["status"] == "complete" for provider in ("apif", "fdorg")
    )
