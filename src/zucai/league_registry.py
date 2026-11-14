"""Reviewed competition routes; catalogue discovery never grants import permission."""

import copy
import json
import re
from datetime import date
from pathlib import Path

REGISTRY_PATH = Path(__file__).resolve().parents[2] / "data/league_map.json"
PROVIDERS = {"apif", "fdorg", "fd"}
MODES = {"import", "identity_only", "index_only", "nations"}
STATUSES = {"verified", "candidate"}


def _require(condition, message):
    if not condition:
        raise ValueError(f"Invalid league registry: {message}")


def _keys(value, required, optional=()):
    _require(isinstance(value, dict), "expected object")
    _require(set(required) <= value.keys() <= set(required) | set(optional), "object fields")


def _strings(values, *, empty=False):
    return (
        isinstance(values, list)
        and (empty or bool(values))
        and all(isinstance(v, str) and v for v in values)
        and len(values) == len(set(values))
    )


def _evidence(value, status):
    _require(isinstance(value, list), "evidence must be a list")
    _require(status != "verified" or bool(value), "verified entry needs evidence")
    for item in value:
        _require(isinstance(item, dict) and item.get("kind") and item.get("claim"), "evidence")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, f"duplicate JSON key {key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f"Invalid league registry: non-JSON constant {value}")


def _provider_entry(provider, code, entry):
    _require(isinstance(code, str), "provider code")
    pattern = r"[1-9][0-9]*" if provider == "apif" else r"[A-Z][A-Z0-9]*"
    _require(re.fullmatch(pattern, code), f"invalid {provider} code {code}")
    _keys(
        entry,
        ("status", "mode", "groups", "relevant", "daily", "backfill", "config", "evidence"),
        ("verified_scope", "access_status"),
    )
    status, mode, config = entry["status"], entry["mode"], entry["config"]
    _require(isinstance(status, str) and status in STATUSES, "status")
    _require(isinstance(mode, str) and mode in MODES, "mode")
    _require(provider == "apif" or mode == "import", "provider mode")
    _require(_strings(entry["groups"], empty=True), "groups")
    _require(set(entry["groups"]) <= {"A", "B", "C"}, "unknown scope group")
    for field in ("relevant", "daily", "backfill"):
        _require(type(entry[field]) is bool, f"{field} must be boolean")
    _require(bool(entry["groups"]) == entry["relevant"], "groups and relevance disagree")
    _require(isinstance(config, dict) and isinstance(config.get("league"), str), "config league")
    _require(bool(config["league"].strip()), "empty config league")
    if "api_id" in config:
        _require(type(config["api_id"]) is int and config["api_id"] > 0, "config api_id")
    if "catalogue_seasons" in config:
        years = config["catalogue_seasons"]
        _require(isinstance(years, list), "catalogue_seasons")
        _require(
            all(type(year) is int and 1900 <= year <= 9999 for year in years)
            and len(years) == len(set(years)),
            "catalogue_seasons values",
        )
    for flag in ("identity_only", "index_only"):
        _require(type(config.get(flag, False)) is bool, f"config {flag}")
        _require(config.get(flag, False) == (mode == flag), f"config contradicts {mode}")
    if mode != "import" or status == "candidate":
        _require(not entry["daily"] and not entry["backfill"], "non-import route scheduled")
    if mode == "nations":
        _require(provider == "apif" and code == "5", "special nations route is APIF 5")
    if provider == "apif":
        seasons = config.get("seasons_available")
        _require(isinstance(seasons, list), "APIF seasons_available")
        _require(
            all(type(year) is int and 1900 <= year <= 9999 for year in seasons)
            and len(seasons) == len(set(seasons)),
            "APIF season values",
        )
    elif provider == "fdorg":
        year = config.get("earliest_season")
        _require(type(year) is int and 1900 <= year <= 9999, "FDORG earliest_season")
    else:
        _require(config.get("div_code") == code, "FD div_code")
        if status == "verified":
            _require(config.get("status") == "verified", "FD verification status")
    if "access_status" in entry:
        _require(entry["access_status"] in {"verified", "unverified"}, "access_status")
    if "verified_scope" in entry:
        _require(isinstance(entry["verified_scope"], str) and entry["verified_scope"], "scope")
    _evidence(entry["evidence"], status)


def _catalogue_proposal(value, *, official, row, registry):
    common = ("status", "review_status", "searched_names", "reason", "entries")
    extra = (
        (
            "proposed_status",
            "owner_decision_required",
            "master_context",
            "observation_window",
            "evidence",
        )
        if official
        else ()
    )
    _keys(value, (*common, *extra))
    statuses = {"candidate", "not_found"} | (set() if official else {"existing_verified"})
    _require(isinstance(value["status"], str) and value["status"] in statuses, "proposal status")
    expected_review = "existing_verified" if value["status"] == "existing_verified" else "pending"
    _require(value["review_status"] == expected_review, "proposal review status")
    _require(_strings(value["searched_names"]), "proposal searched names")
    _require(isinstance(value["reason"], str) and value["reason"], "proposal reason")
    entries = value["entries"]
    _require(isinstance(entries, list), "proposal entries")
    _require(bool(entries) == (value["status"] != "not_found"), "proposal entries/status")
    seen = set()
    for entry in entries:
        if official:
            _keys(
                entry,
                (
                    "league_id",
                    "league_abbr",
                    "league_name",
                    "observed_match_count",
                    "first_match_date",
                    "last_match_date",
                    "evidence",
                ),
            )
            code = entry["league_id"]
            _require(type(code) is int and code > 0, "official league_id")
            for key in ("league_abbr", "league_name"):
                _require(isinstance(entry[key], str) and entry[key], "official name")
            count = entry["observed_match_count"]
            _require(type(count) is int and count >= 0, "official observed count")
            _date_range(entry["first_match_date"], entry["last_match_date"], empty=count == 0)
        else:
            _keys(entry, ("code", "name", "country", "type", "catalogue_seasons", "evidence"))
            code = entry["code"]
            _require(
                isinstance(code, str) and code in registry["providers"]["apif"], "proposal API code"
            )
            for key in ("name", "country"):
                _require(isinstance(entry[key], str) and entry[key], "proposal API name/country")
            _require(
                isinstance(entry["type"], str) and entry["type"] in {"League", "Cup"},
                "proposal API type",
            )
            seasons = entry["catalogue_seasons"]
            _require(isinstance(seasons, list), "proposal seasons")
            _require(
                all(type(year) is int and 1900 <= year <= 9999 for year in seasons),
                "proposal season values",
            )
            _require(len(set(seasons)) == len(seasons), "proposal duplicate season")
        _require(code not in seen, "proposal duplicate code")
        seen.add(code)
        _evidence(entry["evidence"], "verified")
    if not official:
        if value["status"] == "existing_verified":
            _require(row["status"] == "verified", "unapproved existing proposal")
        return
    _require(
        value["proposed_status"] in {"verified", "candidate", "not_found"},
        "official proposed status",
    )
    _require(type(value["owner_decision_required"]) is bool, "official decision flag")
    if value["proposed_status"] == "verified":
        _require(
            len(entries) == 1 and not value["owner_decision_required"],
            "ambiguous official approval proposal",
        )
    _require((value["proposed_status"] == "not_found") == (not entries), "official missing status")
    context = value["master_context"]
    _keys(
        context,
        (
            "source_names",
            "jc_match_count",
            "jc_first_sale_day",
            "jc_last_sale_day",
            "sfc_match_count",
            "sfc_first_issue",
            "sfc_last_issue",
        ),
    )
    _require(context["source_names"] == row["source_names"], "proposal master source names")
    for key in ("jc_match_count", "sfc_match_count"):
        _require(type(context[key]) is int and context[key] >= 0, "master match count")
    _date_range(
        context["jc_first_sale_day"],
        context["jc_last_sale_day"],
        empty=context["jc_match_count"] == 0,
    )
    for key in ("sfc_first_issue", "sfc_last_issue"):
        _require(
            context[key] is None or isinstance(context[key], str) and bool(context[key]),
            "master issue",
        )
    window = value["observation_window"]
    _keys(window, ("start_beijing", "end_beijing", "count_basis"))
    _date_range(window["start_beijing"], window["end_beijing"], empty=False)
    _require(isinstance(window["count_basis"], str) and window["count_basis"], "count basis")
    _evidence(value["evidence"], "verified")


def _date_range(start, end, *, empty):
    if empty:
        _require(start is None and end is None, "empty observation dates")
        return
    _require(isinstance(start, str) and isinstance(end, str), "observation dates")
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        raise ValueError("Invalid league registry: observation date format") from None
    _require(
        first.isoformat() == start and last.isoformat() == end and first <= last,
        "observation date range",
    )


def _competition(row, registry):
    _keys(
        row,
        (
            "master_row_id",
            "master_name",
            "project_name",
            "source_names",
            "source_names_raw",
            "master_fields",
            "status",
            "mappings",
            "missing_reason",
        ),
        ("official_catalogue", "api_catalogue_proposal"),
    )
    _require(isinstance(row["master_row_id"], str), "master_row_id")
    _require(re.fullmatch(r"master-[0-9]{3}", row["master_row_id"]), "master_row_id format")
    fields = row["master_fields"]
    _keys(fields, registry["master_table"]["columns"])
    _require(all(isinstance(value, str) for value in fields.values()), "master fields are strings")
    for field, source in (
        ("master_name", "赛事"),
        ("project_name", "项目内赛事名"),
        ("source_names_raw", "来源出现过的名称"),
    ):
        _require(isinstance(row[field], str) and row[field] == fields.get(source), field)
    _require(_strings(row["source_names"]), "source_names")
    _require(row["source_names"] == row["source_names_raw"].split("、"), "lossy source names")
    _require(isinstance(row["status"], str) and row["status"] in STATUSES, "competition status")
    _require(isinstance(row["mappings"], list), "mappings")
    seen = set()
    for mapping in row["mappings"]:
        _keys(mapping, ("provider", "code", "status", "evidence"))
        provider, code, status = mapping["provider"], mapping["code"], mapping["status"]
        _require(isinstance(provider, str) and provider in PROVIDERS, "mapping provider")
        _require(isinstance(status, str) and status in STATUSES, "mapping status")
        _require(code is None or isinstance(code, str), "mapping code")
        _require((provider, code) not in seen, "duplicate competition mapping")
        seen.add((provider, code))
        if code is None:
            _require(status == "candidate", "unknown code cannot be verified")
        else:
            entry = registry["providers"][provider].get(code)
            _require(entry is not None, "mapping references unknown provider code")
            _require(status != "verified" or entry["status"] == "verified", "candidate mapping")
        _evidence(mapping["evidence"], status)
    approved = any(mapping["status"] == "verified" for mapping in row["mappings"])
    _require((row["status"] == "verified") == approved, "competition/mapping status mismatch")
    _require(isinstance(row["missing_reason"], str), "missing_reason")
    _require(approved or row["missing_reason"], "candidate needs missing_reason")
    for field, official in (("official_catalogue", True), ("api_catalogue_proposal", False)):
        if field in row:
            _catalogue_proposal(row[field], official=official, row=row, registry=registry)


def load_registry(path=REGISTRY_PATH) -> dict:
    """Load a fresh ordered registry, rejecting malformed or contradictory approvals."""
    with Path(path).open(encoding="utf-8-sig") as stream:
        registry = json.load(
            stream, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    _keys(
        registry,
        (
            "version",
            "master_table",
            "download_policy",
            "scope_policy",
            "supporting_scope",
            "providers",
            "competitions",
        ),
    )
    _require(type(registry["version"]) is int and registry["version"] == 1, "version")
    master = registry["master_table"]
    _keys(master, ("path", "sha256", "row_count", "columns"))
    _require(isinstance(master["path"], str) and master["path"], "master path")
    _require(
        isinstance(master["sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", master["sha256"]),
        "master sha256",
    )
    _require(type(master["row_count"]) is int and master["row_count"] > 0, "master row_count")
    _require(_strings(master["columns"]), "master columns")
    _require(
        {"赛事", "项目内赛事名", "来源出现过的名称"} <= set(master["columns"]), "master columns"
    )
    policy = registry["download_policy"]
    _keys(policy, ("scope", "daily_reserve", "max_requests"))
    _require(
        isinstance(policy["scope"], str) and policy["scope"] in {"related", "all"}, "download scope"
    )
    for field in ("daily_reserve", "max_requests"):
        _require(type(policy[field]) is int and policy[field] > 0, field)
    _keys(registry["scope_policy"], ("A", "B", "C", "unrelated"))
    _require(
        all(isinstance(v, str) and v for v in registry["scope_policy"].values()), "scope policy"
    )
    _keys(registry["providers"], PROVIDERS)
    for provider, entries in registry["providers"].items():
        _require(isinstance(entries, dict), "provider entries")
        for code, entry in entries.items():
            _provider_entry(provider, code, entry)
        api_ids = [
            entry["config"]["api_id"] for entry in entries.values() if "api_id" in entry["config"]
        ]
        _require(len(api_ids) == len(set(api_ids)), "duplicate provider api_id")
    rows = registry["competitions"]
    _require(isinstance(rows, list) and len(rows) == master["row_count"], "master row count")
    for row in rows:
        _competition(row, registry)
    ids = [row["master_row_id"] for row in rows]
    _require(len(ids) == len(set(ids)), "duplicate master_row_id")
    _require(ids == [f"master-{i:03}" for i in range(1, len(rows) + 1)], "master row order")
    supporting = registry["supporting_scope"]
    _keys(
        supporting,
        ("countries", "national_teams", "participant_country_gaps"),
        ("playoff_leagues",),
    )
    for entries in supporting.values():
        _require(isinstance(entries, list), "supporting scope entries")
        for entry in entries:
            _require(
                isinstance(entry, dict) and entry.get("status") in STATUSES, "supporting status"
            )
            refs = entry.get("master_row_ids")
            _require(_strings(refs, empty=True) and set(refs) <= set(ids), "supporting references")
    return registry


def provider_entries(provider, *, path=REGISTRY_PATH, modes=None, purpose=None) -> dict:
    """Approved routes in source order. Ordinary schedules exclude the special nations route."""
    _require(provider in PROVIDERS, "unknown provider")
    modes = MODES if modes is None else {modes} if isinstance(modes, str) else set(modes)
    _require(modes <= MODES, "unknown mode")
    _require(purpose in {None, "daily", "backfill"}, "unknown purpose")
    registry = load_registry(path)
    all_scope = registry["download_policy"]["scope"] == "all"
    return {
        code: entry
        for code, entry in registry["providers"][provider].items()
        if entry["status"] == "verified"
        and entry["mode"] in modes
        and (entry["relevant"] or all_scope)
        and (purpose is None or (entry["mode"] == "import" and entry[purpose]))
    }


def provider_config(provider, *, path=REGISTRY_PATH) -> dict:
    """Return authoritative approved configs, including legacy identity/index/nations modes."""
    return {
        code: copy.deepcopy(row["config"])
        for code, row in provider_entries(provider, path=path).items()
    }


def provider_ids(provider, mode="import", *, path=REGISTRY_PATH) -> frozenset[int]:
    """API-Football's numeric IDs; other providers use daily_codes/provider_entries."""
    _require(provider == "apif", "numeric IDs are only defined for apif")
    return frozenset(int(code) for code in provider_entries(provider, path=path, modes=mode))


def daily_codes(provider, *, path=REGISTRY_PATH) -> list[str]:
    return list(provider_entries(provider, path=path, purpose="daily"))
