"""Read-only Actions catalogue evidence; published metadata never grants access."""

import hashlib
import html
import json
import os
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import requests

from zucai.league_registry import REGISTRY_PATH, load_registry
from zucai.sources.api_common import CredentialError, LocalValidationError
from zucai.sources.api_football import API as APIF_API
from zucai.sources.api_football import ApiFootball
from zucai.sources.apif_quota import QuotaExceeded
from zucai.sources.base import RawStore, _atomic_write
from zucai.sources.football_data import LEAGUES_PATH as FD_LEAGUES_PATH
from zucai.sources.football_data import verified_league
from zucai.sources.football_data_org import API as FDORG_API
from zucai.sources.football_data_org import FootballDataOrg, _rate_headers, safe_json_for_archive

RUN_ID = r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}"
# A fixed, clock-free sanity window, not inferred season boundaries. The lower
# bound matches catalogue season years; 2100 excludes implausible remote dates.
APIF_DATE_MIN_YEAR = 1900
APIF_DATE_MAX_YEAR = 2100


class CatalogueError(LocalValidationError):
    """A fixed, credential-free diagnostic authored by this module."""


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CatalogueError("catalogue JSON contains duplicate keys")
        result[key] = value
    return result


def _invalid_constant(_):
    raise CatalogueError("catalogue JSON contains a non-finite number")


def _document(raw):
    try:
        doc = json.loads(raw, object_pairs_hook=_object_pairs, parse_constant=_invalid_constant)
        pending = [doc]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                for key in item:
                    if re.sub(r"[^a-z0-9]", "", key.lower()) in {
                        "apikey",
                        "token",
                        "accesstoken",
                        "authorization",
                        "password",
                        "secret",
                        "xauthtoken",
                        "xapisportskey",
                        "xrapidapikey",
                    }:
                        raise CatalogueError("catalogue contains a credential field")
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
            elif isinstance(item, str):
                item.encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, CatalogueError):
            raise
        raise CatalogueError("catalogue is not valid UTF-8 JSON") from None
    if not isinstance(doc, dict):
        raise CatalogueError("catalogue root must be an object")
    return doc


def _text(value):
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise CatalogueError("catalogue name, country or type is missing or invalid")
    return value


def _identifier(value):
    if type(value) is int and value > 0:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value):
        return value
    raise CatalogueError("catalogue league ID is missing or invalid")


def _year(value):
    if type(value) is not int or not 1900 <= value <= 9999:
        raise CatalogueError("catalogue season year must be a four-digit integer")
    return value


def _day(value):
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise CatalogueError("catalogue season date is invalid") from None
    if parsed.isoformat() != value:
        raise CatalogueError("catalogue season date must use YYYY-MM-DD")
    return value


def _season(year, start, end, fixtures=None, results=None):
    first, last = _day(start), _day(end)
    if first and last and first > last:
        raise CatalogueError("catalogue season date order is invalid")
    return dict(
        year=_year(year),
        start_date=first,
        end_date=last,
        fixtures=fixtures,
        results=results,
        access="unverified",
    )


def _flag(value):
    if value is not None and type(value) is not bool:
        raise CatalogueError("catalogue coverage flag must be boolean or null")
    return value


def _apif_season(identity, year, source, fixtures, results):
    dates, reasons = {}, []
    for field in ("start", "end"):
        if field not in source or (
            source[field] is not None and not isinstance(source[field], str)
        ):
            raise CatalogueError("API-Football catalogue season date field is missing or invalid")
        try:
            dates[field] = _day(source[field])
        except CatalogueError as exc:
            dates[field] = None
            reasons.append(f"{field}: {exc}")
        if dates[field] and not APIF_DATE_MIN_YEAR <= int(dates[field][:4]) <= APIF_DATE_MAX_YEAR:
            reasons.append(
                f"{field}: catalogue season date year is outside "
                f"{APIF_DATE_MIN_YEAR}..{APIF_DATE_MAX_YEAR}"
            )
    if dates["start"] and dates["end"] and dates["start"] > dates["end"]:
        reasons.append("catalogue season date order is invalid")
    result = _season(
        year,
        None if reasons else dates["start"],
        None if reasons else dates["end"],
        fixtures,
        results,
    )
    if reasons:
        result["warnings"] = [
            dict(
                code="invalid_season_dates",
                league_id=identity,
                season_year=year,
                original_start=source["start"],
                original_end=source["end"],
                reason="; ".join(reasons),
            )
        ]
    return result


def parse_apif_catalogue(raw, *, raw_path=None) -> dict:
    """Parse one complete /leagues response without credentials, I/O or a clock."""
    doc = _document(raw)
    rows, paging = doc.get("response"), doc.get("paging")
    if doc.get("get") != "leagues" or doc.get("parameters") not in ({}, []):
        raise CatalogueError("API-Football catalogue endpoint or parameters mismatch")
    if doc.get("errors") not in ({}, []):
        raise CatalogueError("API-Football catalogue reports API errors")
    if not isinstance(rows, list) or type(doc.get("results")) is not int:
        raise CatalogueError("API-Football catalogue results or response is invalid")
    if doc["results"] != len(rows):
        raise CatalogueError("API-Football catalogue results count mismatch")
    if (
        not isinstance(paging, dict)
        or type(paging.get("current")) is not int
        or type(paging.get("total")) is not int
        or paging["current"] != 1
        or paging["total"] != 1
    ):
        raise CatalogueError("API-Football catalogue pagination is incomplete")
    leagues, ids = [], set()
    raw_digest = hashlib.sha256(raw.encode() if isinstance(raw, str) else raw).hexdigest()
    for row_index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise CatalogueError("API-Football catalogue league row is invalid")
        league, country, seasons = row.get("league"), row.get("country"), row.get("seasons")
        if (
            not isinstance(league, dict)
            or not isinstance(country, dict)
            or not isinstance(seasons, list)
        ):
            raise CatalogueError("API-Football catalogue league fields are missing")
        identity = _identifier(league.get("id"))
        if identity in ids:
            raise CatalogueError("API-Football catalogue contains duplicate league IDs")
        ids.add(identity)
        normalized, by_year = [], {}
        for season_index, season in enumerate(seasons):
            if not isinstance(season, dict):
                raise CatalogueError("API-Football catalogue season is invalid")
            year = _year(season.get("year"))
            coverage = season.get("coverage")
            if coverage is None:
                coverage = {}
            if not isinstance(coverage, dict):
                raise CatalogueError("API-Football catalogue coverage is invalid")
            fixtures = coverage.get("fixtures")
            # These nested flags describe technical details, not fixtures/results availability.
            if isinstance(fixtures, dict):
                for flag in fixtures.values():
                    _flag(flag)
                fixtures = None
            parsed = _apif_season(
                identity,
                year,
                season,
                _flag(fixtures),
                _flag(coverage.get("results")),
            )
            by_year.setdefault(year, []).append((season_index, season, parsed))
        # Validate every original before isolating conflicts. A duplicate year
        # must not hide missing fields or invalid types in either record.
        for year, records in sorted(by_year.items()):
            if len(records) == 1:
                normalized.append(records[0][2])
                continue
            warning = dict(
                code="duplicate_season_year",
                league_id=identity,
                season_year=year,
                original_records=[source for _, source, _ in records],
                raw_sha256=raw_digest,
                record_pointers=[
                    f"/response/{row_index}/seasons/{index}" for index, _, _ in records
                ],
                reason="multiple catalogue records share this year; unavailable for backfill",
            )
            if raw_path is not None:
                warning["raw_path"] = raw_path
            conflict = _season(year, None, None)
            conflict.update(
                status="conflict",
                access="conflict",
                warnings=[warning]
                + [w for _, _, parsed in records for w in parsed.get("warnings", [])],
            )
            normalized.append(conflict)
        leagues.append(
            dict(
                id=identity,
                name=_text(league.get("name")),
                country=_text(country.get("name")),
                type=_text(league.get("type")),
                seasons=sorted(normalized, key=lambda s: s["year"]),
            )
        )
    leagues.sort(key=lambda row: int(row["id"]))
    return dict(
        version=1,
        provider="apif",
        leagues=leagues,
        raw_season_count=sum(len(row["seasons"]) for row in rows),
        conflicted_season_count=sum(
            season.get("status") == "conflict" for league in leagues for season in league["seasons"]
        ),
        warnings=[
            warning
            for league in leagues
            for season in league["seasons"]
            for warning in season.get("warnings", [])
        ],
    )


def parse_fdorg_catalogue(raw) -> dict:
    """The collection lists currentSeason only; a season count is not historical years."""
    doc = _document(raw)
    rows, count = doc.get("competitions"), doc.get("count")
    if doc.get("errorCode") is not None or doc.get("errors") not in (None, [], {}):
        raise CatalogueError("football-data.org catalogue reports API errors")
    if not isinstance(rows, list) or type(count) is not int or count != len(rows):
        raise CatalogueError("football-data.org catalogue competition count mismatch")
    leagues, ids, codes = [], set(), set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("area"), dict):
            raise CatalogueError("football-data.org catalogue competition row is invalid")
        identity, code = _identifier(row.get("id")), row.get("code")
        if identity in ids:
            raise CatalogueError("football-data.org catalogue contains duplicate competition IDs")
        ids.add(identity)
        if code is not None:
            _text(code)
            if code in codes:
                raise CatalogueError(
                    "football-data.org catalogue contains duplicate competition codes"
                )
            codes.add(code)
        count_seasons = row.get("numberOfAvailableSeasons")
        if count_seasons is not None and (type(count_seasons) is not int or count_seasons < 0):
            raise CatalogueError("football-data.org available season count is invalid")
        current, seasons = row.get("currentSeason"), []
        if current is not None:
            if not isinstance(current, dict):
                raise CatalogueError("football-data.org current season is invalid")
            start, end = _day(current.get("startDate")), _day(current.get("endDate"))
            if start is None or end is None or count_seasons == 0:
                raise CatalogueError("football-data.org current season dates or count are missing")
            seasons.append(_season(int(start[:4]), start, end))
        leagues.append(
            dict(
                id=identity,
                code=code,
                name=_text(row.get("name")),
                country=_text(row["area"].get("name")),
                type=_text(row.get("type")),
                seasons=seasons,
                number_of_available_seasons=count_seasons,
            )
        )
    return dict(
        version=1, provider="fdorg", leagues=sorted(leagues, key=lambda row: int(row["id"]))
    )


def _json_bytes(document):
    return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _write_new(path, content):
    with path.open("xb") as stream:
        stream.write(content)


def _credentials():
    values = (
        os.environ.get("API_FOOTBALL_KEY", "").strip(),
        os.environ.get("FOOTBALL_DATA_ORG_TOKEN", ""),
    )
    return [value for value in values if re.fullmatch(r"[!-~]+", value)]


class _CatalogueRawStore(RawStore):
    """The provider transports check their own key; also reject cross-provider echoes."""

    def save_response(self, source, name, response, ext, *, captured_at=None):
        if any(not safe_json_for_archive(response.content, token) for token in _credentials()):
            raise CatalogueError("catalogue response could not be safely archived")
        return super().save_response(source, name, response, ext, captured_at=captured_at)


def _safe_failure(exc):
    if isinstance(exc, CatalogueError):
        return str(exc)
    if isinstance(exc, CredentialError):
        return "missing or invalid environment credentials"
    message = str(exc)
    status = re.search(r"HTTP\s+(\d{3})\b", message)
    if status:
        return "HTTP " + status[1]
    if "请设置有效" in message:
        return "missing or invalid environment credentials"
    if "配额" in message or isinstance(exc, QuotaExceeded):
        return "quota insufficient or quota state invalid"
    if (
        isinstance(exc, (requests.RequestException, ConnectionError, TimeoutError))
        or "网络" in message
    ):
        return "network request failed"
    # Never publish transport exception details, headers, URLs or arbitrary upstream wording.
    return "catalogue response, archive or configuration validation failed"


def _fetch_fdorg(client):
    """One GET only: no pagination, redirects or the match adapter's 429 retry."""
    token = os.environ.get("FOOTBALL_DATA_ORG_TOKEN", "")
    if not token or not re.fullmatch(r"[\x21-\x7e]+", token):
        raise CredentialError("FOOTBALL_DATA_ORG_TOKEN", missing=not token)
    url = FDORG_API + "/competitions"
    client.limiter.wait()
    client.request_count += 1
    try:
        response = client.session.get(
            url,
            headers={"X-Auth-Token": token},
            allow_redirects=False,
            timeout=30,
        )
    except (requests.RequestException, OSError):
        raise CatalogueError("football-data.org catalogue network request failed") from None
    captured = datetime.now(UTC)
    headers = _rate_headers(response.headers, token)
    if response.url != url or token in url or not safe_json_for_archive(response.content, token):
        raise CatalogueError("football-data.org catalogue response could not be safely archived")
    path = client.store.save_response(
        client.name, "competitions", response, "json", captured_at=captured
    )
    metadata = client.store.load_metadata(path)
    metadata["rate_headers"] = headers
    _atomic_write(path.with_suffix(path.suffix + ".meta.json"), _json_bytes(metadata))
    return path


def _archive_response(root, folder, provider, source, store):
    raw, metadata = source.read_bytes(), store.load_metadata(source)
    digest = hashlib.sha256(raw).hexdigest()
    expected_url = {"apif": APIF_API + "/leagues", "fdorg": FDORG_API + "/competitions"}[provider]
    if metadata.get("sha256") != digest or metadata.get("url") != expected_url:
        raise CatalogueError("catalogue source archive hash or endpoint mismatch")
    try:
        instant = datetime.fromisoformat(metadata["fetched_at_utc"].replace("Z", "+00:00"))
        if instant.utcoffset() != timedelta(0):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        raise CatalogueError("catalogue source capture time must be actual UTC") from None
    raw_path, meta_path = folder / f"{provider}-raw.json", folder / f"{provider}-meta.json"
    _write_new(raw_path, raw)
    _write_new(meta_path, _json_bytes(metadata))
    return (
        raw,
        metadata,
        dict(
            raw_sha256=digest,
            raw_path=raw_path.relative_to(root).as_posix(),
            meta_path=meta_path.relative_to(root).as_posix(),
            fetched_at_utc=metadata["fetched_at_utc"],
            url=expected_url,
            http_status=metadata["status_code"],
        ),
    )


def _observe_provider(root, folder, provider):
    report = dict(status="error", requests=0, errors=[], warnings=[])
    client, source, error = None, None, None
    try:
        store = _CatalogueRawStore(root / "raw")
        if provider == "apif":
            client = ApiFootball(
                store=store, quota_path=root / "state/api_football_quota.json", leagues={}
            )
        else:
            client = FootballDataOrg(store=store, competitions={})
        try:
            source = (
                client._request(APIF_API + "/leagues", name="leagues")
                if provider == "apif"
                else _fetch_fdorg(client)
            )
        except Exception as exc:
            error = _safe_failure(exc)
            if provider == "apif" and client.pages:
                source = client.pages[-1]
        if source is not None:
            raw, metadata, provenance = _archive_response(root, folder, provider, source, store)
            report.update(provenance)
            if metadata["status_code"] != 200:
                error = f"HTTP {metadata['status_code']}"
            if error is None:
                parsed = (
                    parse_apif_catalogue(raw, raw_path=provenance["raw_path"])
                    if provider == "apif"
                    else parse_fdorg_catalogue(raw)
                )
                parsed["provenance"] = provenance
                destination = folder / f"{provider}.json"
                payload = _json_bytes(parsed)
                _write_new(destination, payload)
                report.update(
                    status="complete",
                    warnings=parsed.get("warnings", []),
                    parsed_path=destination.relative_to(root).as_posix(),
                    parsed_sha256=hashlib.sha256(payload).hexdigest(),
                    league_count=len(parsed["leagues"]),
                    season_count=sum(len(row["seasons"]) for row in parsed["leagues"]),
                )
                if provider == "apif":
                    report.update(
                        raw_season_count=parsed["raw_season_count"],
                        conflicted_season_count=parsed["conflicted_season_count"],
                    )
        if error is not None:
            report["errors"].append(error)
    except Exception as exc:
        report["errors"].append(_safe_failure(exc))
    finally:
        if client is not None:
            report["requests"] = client.request_count
            client.session.close()
    return report


def _static_fd(root, folder):
    """Record only configured, verified DIV identities; no unobserved season range."""
    raw = FD_LEAGUES_PATH.read_bytes()
    config = _document(raw)
    registry = load_registry(REGISTRY_PATH)
    registry_digest = hashlib.sha256(REGISTRY_PATH.read_bytes()).hexdigest()
    leagues = []
    for code, row in sorted(config.items()):
        if not isinstance(row, dict) or row.get("div_code") != code:
            raise CatalogueError("football-data.co.uk DIV configuration is invalid")
        enabled = row.get("status") == "verified" and code != "D2"
        if enabled:
            verified_league(config, code)
        approved = registry["providers"]["fd"].get(code, {})
        years, season_evidence = [], []
        if enabled and approved.get("status") == "verified":
            years = approved["config"].get("catalogue_seasons", [])
            if not isinstance(years, list) or len(years) != len(set(years)):
                raise CatalogueError("football-data.co.uk catalogue seasons are invalid")
            years = sorted(_year(year) for year in years)
            if years:
                season_evidence = approved["evidence"]
        leagues.append(
            dict(
                id=code,
                code=code,
                name=_text(row.get("publisher_league") or row.get("league")),
                country=row.get("publisher_country"),
                type="League",
                seasons=[_season(year, None, None) for year in years],
                season_evidence=season_evidence,
                enabled=enabled,
                status=row.get("status"),
                reason="verified DIV configuration"
                if enabled
                else "DIV identity is unverified; import disabled",
                evidence_file=row.get("evidence_file"),
                verified_utc=row.get("verified_utc"),
            )
        )
    digest = hashlib.sha256(raw).hexdigest()
    parsed = dict(
        version=1,
        provider="fd",
        leagues=leagues,
        provenance=dict(
            source_path="data/football_data_co_uk/leagues.json",
            raw_sha256=digest,
            registry_path="data/league_map.json",
            registry_sha256=registry_digest,
        ),
    )
    payload = _json_bytes(parsed)
    path = folder / "fd.json"
    _write_new(path, payload)
    return dict(
        status="complete",
        requests=0,
        errors=[],
        raw_sha256=digest,
        parsed_path=path.relative_to(root).as_posix(),
        parsed_sha256=hashlib.sha256(payload).hexdigest(),
        league_count=len(leagues),
        season_count=sum(len(row["seasons"]) for row in leagues),
        registry_sha256=registry_digest,
    )


def _previous_pointers(root):
    latest = root / "state/catalogue/latest.json"
    if not latest.exists():
        return {}, []
    pointers, errors = {}, []
    try:
        doc = _document(latest.read_bytes())
        if (
            type(doc.get("version")) is not int
            or doc["version"] != 1
            or not isinstance(doc.get("providers"), dict)
        ):
            raise CatalogueError("previous catalogue index is invalid")
        for provider, entry in doc["providers"].items():
            try:
                if provider not in {"apif", "fdorg", "fd", "jc"} or not isinstance(entry, dict):
                    raise ValueError
                run_id = entry.get("run_id", "")
                if not isinstance(run_id, str) or not re.fullmatch(RUN_ID, run_id):
                    raise ValueError
                expected = f"state/catalogue/{run_id}/{provider}.json"
                if entry.get("path") != expected:
                    raise ValueError
                if (root / expected).is_symlink() or (root / expected).parent.is_symlink():
                    raise ValueError
                path = (root / expected).resolve()
                if not path.is_relative_to((root / "state/catalogue").resolve()):
                    raise ValueError
                payload = path.read_bytes()
                if hashlib.sha256(payload).hexdigest() != entry.get("sha256"):
                    raise ValueError
                parsed = _document(payload)
                if provider != "jc":
                    if (
                        type(parsed.get("version")) is not int
                        or parsed["version"] != 1
                        or parsed.get("provider") != provider
                        or not isinstance(entry.get("raw_sha256"), str)
                        or not re.fullmatch(r"[a-f0-9]{64}", entry["raw_sha256"])
                    ):
                        raise ValueError
                    provenance = parsed.get("provenance", {})
                    if (
                        not isinstance(provenance, dict)
                        or provenance.get("raw_sha256") != entry["raw_sha256"]
                    ):
                        raise ValueError
                if provider in {"apif", "fdorg"}:
                    raw_path = path.with_name(f"{provider}-raw.json")
                    meta_path = path.with_name(f"{provider}-meta.json")
                    if raw_path.is_symlink() or meta_path.is_symlink():
                        raise ValueError
                    raw = raw_path.read_bytes()
                    metadata = _document(meta_path.read_bytes())
                    if (
                        hashlib.sha256(raw).hexdigest() != entry["raw_sha256"]
                        or metadata.get("sha256") != entry["raw_sha256"]
                        or metadata.get("status_code") != 200
                        or provenance.get("raw_path")
                        != f"state/catalogue/{run_id}/{provider}-raw.json"
                        or provenance.get("meta_path")
                        != f"state/catalogue/{run_id}/{provider}-meta.json"
                        or provenance.get("fetched_at_utc") != metadata.get("fetched_at_utc")
                        or provenance.get("url") != metadata.get("url")
                    ):
                        raise ValueError
                    replayed = (
                        parse_apif_catalogue(raw, raw_path=provenance["raw_path"])
                        if provider == "apif"
                        else parse_fdorg_catalogue(raw)
                    )
                    if parsed.get("leagues") != replayed["leagues"]:
                        raise ValueError
                pointers[provider] = entry
            except (OSError, TypeError, ValueError, KeyError):
                errors.append("previous catalogue provider pointer failed validation")
    except (OSError, ValueError):
        errors.append("previous catalogue index failed validation")
    return pointers, errors


def collect_catalogue(database: Path, run_id: str) -> dict:
    """Observe catalogues only. The database path locates state; it is never opened."""
    from zucai.cloud_probe import require_probe_actions

    require_probe_actions()
    if os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
        raise CatalogueError("API catalogue probes require a github-hosted runner")
    if (
        not isinstance(run_id, str)
        or not re.fullmatch(RUN_ID, run_id)
        or any(token in run_id for token in _credentials())
    ):
        raise CatalogueError("catalogue run_id must be one safe filename")
    root = Path(database).parent
    folder = root / "state/catalogue" / run_id
    if any(
        path.is_symlink()
        for path in (root / "state", folder.parent, folder, folder.parent / "latest.json")
    ):
        raise CatalogueError("catalogue evidence paths must not be symbolic links")
    if folder.exists():
        raise CatalogueError(
            "catalogue run already exists; immutable evidence cannot be overwritten"
        )
    pointers, errors = _previous_pointers(root)
    folder.mkdir(parents=True, exist_ok=False)
    report = dict(
        version=1,
        collection="catalogue",
        run_id=run_id,
        status="complete",
        requests=0,
        status_requests=0,
        request_budget=2,
        errors=errors,
        warnings=[],
        providers={},
        outputs=[],
        access_note=(
            "Non-conflicting API seasons have access=unverified; metadata is not account "
            "entitlement. Conflicting years are unavailable for backfill."
        ),
    )
    for provider in ("apif", "fdorg", "fd"):
        if provider == "fd":
            try:
                observed = _static_fd(root, folder)
            except Exception as exc:
                observed = dict(status="error", requests=0, errors=[_safe_failure(exc)])
        else:
            observed = _observe_provider(root, folder, provider)
        report["providers"][provider] = observed
        report["requests"] += observed["requests"]
        report["errors"].extend(f"{provider}: {error}" for error in observed["errors"])
        report["warnings"].extend(
            dict(warning, provider=provider) for warning in observed.get("warnings", [])
        )
        if observed["status"] == "complete":
            pointers[provider] = dict(
                path=observed["parsed_path"],
                sha256=observed["parsed_sha256"],
                raw_sha256=observed["raw_sha256"],
                run_id=run_id,
            )
    if report["errors"]:
        report["status"] = "partial"
    latest = folder.parent / "latest.json"
    _atomic_write(latest, _json_bytes(dict(version=1, providers=pointers)))
    summary = folder / "summary.json"
    report["outputs"] = sorted(
        path.relative_to(root).as_posix() for path in [*folder.iterdir(), latest, summary]
    )
    _write_new(summary, _json_bytes(report))
    return report


def write_summary(report):
    if not os.environ.get("GITHUB_STEP_SUMMARY"):
        return

    def cell(value):
        return html.escape(str(value)).replace("|", "&#124;").replace("\n", " ").replace("\r", " ")

    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
        stream.write(f"### Read-only catalogue probe: {cell(report['status'])}\n\n")
        stream.write(f"Requests: {report['requests']} / 2; database imports: 0.\n\n")
        stream.write(cell(report["access_note"]) + "\n\n")
        stream.write("| Provider | Status | Leagues | Listed seasons | Requests |\n")
        stream.write("| --- | --- | ---: | ---: | ---: |\n")
        for provider, result in report["providers"].items():
            values = (
                provider,
                result["status"],
                result.get("league_count", "—"),
                result.get("season_count", "—"),
                result["requests"],
            )
            stream.write("| " + " | ".join(map(cell, values)) + " |\n")
        stream.write(f"\nEvidence: state/catalogue/{cell(report['run_id'])}/summary.json\n")
        for error in report["errors"]:
            stream.write("\n- " + cell(error) + "\n")
        for warning in report.get("warnings", []):
            detail = (
                f"Warning: {warning['provider']} league {warning['league_id']}, "
                f"season {warning['season_year']}: {warning['reason']}"
            )
            if warning["code"] == "invalid_season_dates":
                detail += f" (start={warning['original_start']}, end={warning['original_end']})"
            elif warning["code"] == "duplicate_season_year":
                detail += (
                    f" ({len(warning['original_records'])} original records; "
                    f"raw={warning.get('raw_path', '')}; SHA256={warning['raw_sha256']})"
                )
            stream.write("\n- " + cell(detail) + "\n")
