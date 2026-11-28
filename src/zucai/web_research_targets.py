"""Assemble research material from the independently verified feed and prediction archives."""

import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

from zucai.cloud_feeds import archive_info
from zucai.digest_context import _table, audit_rows, jc_digest_batch
from zucai.forecast_archive import _path, verify_forecasts
from zucai.forecast_publication import publication_times
from zucai.report.render import load_batch
from zucai.result_source_links import RESULT_LINK_FILE, _validate_claims
from zucai.scoreboard.score import frozen_market, instant
from zucai.sources.football_data import CONFIG_ROOT, read_json
from zucai.sources.sporttery import BEIJING
from zucai.sources.sporttery_aliases import (
    NATIONAL_TEAM_COMPETITIONS,
    approved_canonical,
    load_aliases,
)
from zucai.web_research_identity import exact_name, squad_markers

SIDES = ("home", "away")
OUTCOMES = frozenset("hda")
SOURCE_ORDER = {"jc": 0, "fd": 1, "fdorg": 2, "apif": 3, "c500": 4}
ARCHIVED_BINDING = re.compile(r"(?:(jc):team:|(apif|fdorg):([^:]+):team:)([1-9][0-9]*)")
MATCH_PROOF_FIELDS = ("league", "home_team_id", "away_team_id")


def _selling(archive, feed, now):
    sync = feed.get("collection", {}).get("jc", {})
    captured = sync.get("schedule_updated_utc")
    if sync.get("schedule_status") != "complete" or not captured or instant(captured) > now:
        return {}, None
    captured = instant(captured)
    rows = [
        row
        for row in audit_rows(archive, feed, "jc_catalog", now, all_shards=True) or ()
        if instant(row["captured_utc"]) == captured
    ]
    ids = Counter(row["match_id"] for row in rows)
    slots = Counter((row["business_date"], row["jc_num"]) for row in rows)
    return {
        row["match_id"]: row
        for row in rows
        if row["match_id"]
        and row["jc_num"]
        and ids[row["match_id"]] == slots[row["business_date"], row["jc_num"]] == 1
        and row["matchStatus"] == "Selling"
        and row["sellStatus"] == row["has_fixed_odds"] == row["retained"] == "1"
        and row["kickoff_utc"]
        and now < instant(row["kickoff_utc"])
    }, captured


def _name_matches(row, side, team, aliases):
    raw = row[side]
    if not raw.strip():
        return False
    wanted = approved_canonical(aliases, row["league"], raw) or raw
    expected = exact_name(wanted)
    return any(
        exact_name(name) == expected
        for name in [team["name"], *json.loads(team["aliases"] or "[]")]
    )


def _sale_identities(sales, matches, teams):
    aliases = load_aliases()
    identities = {}
    for match_id, row in sales.items():
        match = matches.get(match_id)
        if (
            not match
            or match["status"] != "scheduled"
            or match["source"] != "jc"
            or any(match[key] != row[key] for key in ("jc_num", "league"))
            or instant(match["kickoff_utc"]) != instant(row["kickoff_utc"])
        ):
            continue
        sides = {side: teams[match[f"{side}_team_id"]] for side in SIDES}
        if all(_name_matches(row, side, sides[side], aliases) for side in SIDES):
            identities[match_id] = {**match, **{side: sides[side]["name"] for side in SIDES}}
    return identities


def _published_predictions(archive, manifest, identities, now, forecast_run_id):
    publications = publication_times(archive, manifest, require_legacy_history=False)
    days = {
        instant(row["kickoff_utc"]).astimezone(BEIJING).date().isoformat()
        for row in identities.values()
    }
    candidates = {}
    for run_id, run in manifest["runs"].items():
        if forecast_run_id is not None and run_id != forecast_run_id:
            continue
        for day in sorted(days & run["forecasts"].keys()):
            path = run["forecasts"][day]["path"] + "/forecast.json"
            publication = publications.get(path)
            if not publication or instant(publication["published_utc"]) > now:
                continue
            batch = jc_digest_batch(
                load_batch(_path(archive, path), allow_unknown_odds_time=True),
                spillover=day in run.get("jc_extra_dates", ()),
            )
            if batch is None or batch["mode"] != "prematch":
                continue
            frozen_at = instant(batch["generated_utc"])
            published_at = instant(publication["published_utc"])
            if not instant(batch["as_of_utc"]) <= frozen_at <= published_at:
                continue
            order = frozen_at, published_at, batch["batch_id"], path
            for prediction in batch["predictions"]:
                key = prediction["match_id"]
                identity = identities.get(key)
                if (
                    not identity
                    or prediction["status"] != "predicted"
                    or prediction.get("source") != "jc"
                    or any(prediction.get(k) != identity[k] for k in ("jc_num", "league", *SIDES))
                    or instant(prediction["kickoff_utc"]) != instant(identity["kickoff_utc"])
                    or (key in candidates and order <= candidates[key][0])
                ):
                    continue
                version = {**prediction, **{k: batch[k] for k in ("generated_utc", "as_of_utc")}}
                candidates[key] = (
                    order,
                    {
                        "forecast_run_id": run_id,
                        "forecast_path": path,
                        "forecast_sha256": manifest["files"][path],
                        "forecast_published_utc": publication["published_utc"],
                        "forecast_publication_evidence": publication,
                    },
                    version,
                )
    return candidates


def _friendly(league):
    name = league.casefold()
    return (
        "友谊" in name
        or "热身" in name
        or "friendly" in name
        or "friendlies" in name
        or name == "国际赛"
    )


def _recent(archive, feed, matches, team_ids, teams, now):
    groups = defaultdict(list)
    for result in _table(archive, feed, "results"):
        match = matches.get(result["match_id"])
        if not match or match["status"] != "finished" or _friendly(match["league"]):
            continue
        home, away = match["home_team_id"], match["away_team_id"]
        if home not in team_ids and away not in team_ids:
            continue
        if (
            result["available_source"] not in ("captured", "published")
            or not result["available_utc"]
        ):
            continue
        kickoff, available = instant(match["kickoff_utc"]), instant(result["available_utc"])
        goals = result["home_goals"], result["away_goals"]
        if not kickoff < available <= now or any(
            type(score) is not int or score < 0 for score in goals
        ):
            continue
        groups[home, away, kickoff].append((goals, match, available))
    by_team = defaultdict(list)
    for (home, away, kickoff), group in sorted(
        groups.items(), key=lambda item: (item[0][2], *item[0][:2]), reverse=True
    ):
        # A database conflict must not be handed to the provider as a verified fact.
        if len({goals for goals, _, _ in group}) != 1:
            continue
        (h, a), match, available = min(
            group,
            key=lambda item: (
                SOURCE_ORDER.get(item[1]["source"], len(SOURCE_ORDER)),
                item[1]["match_id"],
            ),
        )
        entry = dict(
            match_id=match["match_id"],
            league=match["league"],
            source=match["result_provider"] or match["source"],
            kickoff_utc=kickoff.isoformat(),
            available_utc=available.isoformat(),
            home=teams[home]["name"],
            away=teams[away]["name"],
            home_goals=h,
            away_goals=a,
        )
        for team in (home, away):
            if team in team_ids:
                by_team[team].append(entry)
    return by_team


def _odds(archive, feed, identities, now):
    match_ids, found = set(identities), {}
    columns = feed["columns"]["odds"]
    paths = sorted(
        (p for p in feed["files"] if p.startswith("state/odds/")),
        reverse=True,
    )
    for path in paths:
        if not match_ids:
            break
        quotes = defaultdict(dict)
        with _path(archive, path).open(encoding="utf-8") as stream:
            for line in stream:
                row = dict(zip(columns, json.loads(line), strict=True))
                key, selection, price = row["match_id"], row["selection"], row["price"]
                if (
                    key in match_ids
                    and row["pool"] == "had"
                    and row["line"] == 0
                    and selection in OUTCOMES
                    and type(price) in (float, int)
                    and math.isfinite(price)
                    and price > 1
                ):
                    captured = instant(row["snapshot_utc"])
                    if captured <= now < instant(identities[key]["kickoff_utc"]):
                        quotes[key, captured][selection] = price
        for (key, captured), prices in sorted(quotes.items(), reverse=True):
            if key in match_ids and prices.keys() == OUTCOMES:
                found[key] = prices, captured.isoformat()
                match_ids.remove(key)
    return found


def _identity_alias_config():
    paths = {
        "apif": "api_football.json",
        "fd": "football_data_co_uk.json",
        "fdorg": "football_data_org.json",
    }
    return {
        "jc": load_aliases(),
        **{
            source: read_json(CONFIG_ROOT / "team_aliases" / name) for source, name in paths.items()
        },
    }


def _scope_matches_squad(label, team_name, scope):
    if category := scope.get("category"):
        _, sex, age, squad = category.split("/")
        for name in (label, team_name):
            women, youth, reserve = squad_markers(name)
            if (
                (women and sex != "women")
                or (youth and (age == "senior" or youth not in {"youth", age.removeprefix("U")}))
                or (reserve and squad != "reserve")
            ):
                return False
        return True
    return squad_markers(label) == squad_markers(team_name)


def _known_alias(entry, now, archived):
    if approval := entry.get("approved_utc"):
        return instant(approval) <= now
    if archived or entry["status"] != "auto_verified":
        return archived
    witnessed = [
        instant(row[field])
        for row in entry.get("evidence", ())
        if isinstance(row, dict)
        for field in ("fetched_at_utc", "seen_utc", "created_utc")
        if row.get(field)
    ]
    return bool(witnessed) and min(witnessed) <= now


def _verified_fixture_groups(archive, feed, matches, now):
    if RESULT_LINK_FILE not in feed["files"]:
        return {}
    columns = feed["result_source_links"]["columns"]
    links = []
    with _path(archive, RESULT_LINK_FILE).open(encoding="utf-8") as stream:
        for line in stream:
            proof = dict(zip(columns, json.loads(line), strict=True))
            if instant(proof["verified_utc"]) > now:
                continue
            left, right = proof["provider_match_id"], proof["counterpart_id"]
            # Keep only still-current identity and kickoff observations, as in load_result_links.
            if any(
                key not in matches
                or any(matches[key][field] != proof[field] for field in MATCH_PROOF_FIELDS)
                or instant(matches[key]["kickoff_utc"]) != instant(proof[stamp])
                for key, stamp in (
                    (left, "provider_kickoff_utc"),
                    (right, "counterpart_kickoff_utc"),
                )
            ):
                continue
            links.append((left, right))
    _validate_claims(links)
    groups = {}
    for left, right in links:
        members = groups.get(left, {left}) | groups.get(right, {right})
        for key in members:
            groups[key] = members
    return groups


def _identity_context(
    match,
    history,
    teams,
    matches,
    league_teams,
    groups,
    config,
    snapshot,
    now,
    pair_fixtures,
    hashes,
):
    def fixture(key):
        current = matches[key]
        return {
            "id": key,
            **{field: current[field] for field in MATCH_PROOF_FIELDS},
            "kickoff_utc": instant(current["kickoff_utc"]).isoformat(),
            "fixture_ids": sorted(groups.get(key, {key})),
        }

    target = {field: match[field] for field in ("match_id", "kickoff_utc", *MATCH_PROOF_FIELDS)}
    keys = {match["match_id"], *(r["match_id"] for rows in history.values() for r in rows)}
    if snapshot is not None:
        keys.update(pair_fixtures)
    fixtures = [fixture(key) for key in sorted(keys)]
    leagues = {row["league"] for row in fixtures}
    memberships = defaultdict(set)
    if snapshot is not None:
        for league in leagues:
            for team_id in league_teams[league]:
                memberships[team_id].add(league)
    else:
        for row in fixtures:
            for side in SIDES:
                memberships[row[f"{side}_team_id"]].add(row["league"])

    registry = []
    for source, competitions in config.items():
        for league, entries in competitions.items():
            for key, entry in entries.items():
                label = key if source in {"fd", "jc"} else entry.get("name")
                provider_id = (
                    key
                    if source in {"apif", "fdorg"} and re.fullmatch(r"[1-9][0-9]*", key)
                    else None
                )
                registry.append((source, league, label, provider_id, entry))
    scoped_names = {exact_name(label) for _, _, label, _, _ in registry if label}
    scoped_ids = {(source, league, value) for source, league, _, value, _ in registry if value}
    canonical = defaultdict(list)
    for row in teams.values():
        canonical[exact_name(row["name"])].append(row["id"])
    by_team = defaultdict(list)
    for source, league, label, provider_id, entry in registry:
        if entry.get("status") not in {"approved", "auto_verified"}:
            continue
        owners = canonical.get(exact_name(entry["canonical"]) if entry.get("canonical") else "")
        if owners and len(owners) == 1 and owners[0] in memberships:
            by_team[owners[0]].append((source, league, label, provider_id, entry))

    identities = []
    for team_id, competitions in sorted(memberships.items()):
        team = teams[team_id]
        raw_aliases = set(json.loads(team["aliases"] or "[]")) if snapshot is not None else set()
        aliases, provider_ids = [], []
        for name in sorted(raw_aliases):
            binding = ARCHIVED_BINDING.fullmatch(name)
            if binding:
                jc, provider, league, identifier = binding.groups()
                for scope in sorted(competitions if jc else competitions & {league}):
                    if (provider or "jc", scope, identifier) not in scoped_ids:
                        provider_ids.append(
                            dict(id=identifier, league=scope, source=jc or provider)
                        )
            elif (
                ":team:" not in name
                and exact_name(name) not in scoped_names
                and _scope_matches_squad(name, team["name"], {})
            ):
                aliases.extend(
                    dict(name=name, league=league, source=None) for league in sorted(competitions)
                )
        for source, league, label, provider_id, entry in by_team[team_id]:
            scope = entry.get("binding_scope", {})
            if provider_id and scope and league not in scope["leagues"]:
                continue
            permitted = set(scope.get("leagues", (league,)))
            if source == "jc" and entry.get("scope") == "national_team":
                permitted = NATIONAL_TEAM_COMPETITIONS
            binding = f"{source}:{league}:team:{provider_id}"
            if not _known_alias(entry, now, label in raw_aliases or binding in raw_aliases):
                continue
            if label and not _scope_matches_squad(label, team["name"], scope):
                continue
            dates = {key: scope[key] for key in ("start_utc", "end_utc") if key in scope}
            for competition in sorted(competitions & permitted):
                if label:
                    aliases.append(dict(name=label, source=source, league=competition, **dates))
                if provider_id:
                    provider_ids.append(
                        dict(id=provider_id, source=source, league=competition, **dates)
                    )
        identities.append(
            dict(id=team_id, name=team["name"], aliases=aliases, provider_ids=provider_ids)
        )
    return {
        "target": target,
        "teams": identities,
        "fixtures": fixtures,
        "snapshot_utc": snapshot,
        "snapshot_hashes": hashes if snapshot else {},
    }


def select(archive, now, *, forecast_run_id=None):
    """Find officially selling, unstarted JC fixtures with a valid published TEAM prediction."""
    archive, now = Path(archive), instant(now)
    feed = archive_info(archive)
    sales, captured = _selling(archive, feed, now)
    if not sales:
        return []
    manifest = verify_forecasts(archive, allow_unknown_odds_time=True)
    if not manifest:
        return []
    matches = {row["match_id"]: row for row in _table(archive, feed, "matches")}
    teams = {row["id"]: row for row in _table(archive, feed, "teams")}
    identities = _sale_identities(sales, matches, teams)
    predictions = _published_predictions(archive, manifest, identities, now, forecast_run_id)
    identities = {key: identities[key] for key in predictions}
    if not identities:
        return []
    team_ids = {match[f"{side}_team_id"] for match in identities.values() for side in SIDES}
    history = _recent(archive, feed, matches, team_ids, teams, now)
    odds = _odds(archive, feed, identities, now)
    snapshot = feed.get("created_utc")
    if not snapshot or instant(snapshot) > now:
        snapshot = None
    league_teams = defaultdict(set)
    pair_fixtures = defaultdict(list)
    target_pairs = {
        frozenset(row[f"{side}_team_id"] for side in SIDES) for row in identities.values()
    }
    for row in matches.values():
        pair = frozenset(row[f"{side}_team_id"] for side in SIDES)
        league_teams[row["league"]].update(pair)
        if pair in target_pairs and row["status"] != "cancelled":
            pair_fixtures[pair].append(row["match_id"])
    hashes = {
        key: feed["files"][key]
        for key in ("state/teams.jsonl", "state/matches.jsonl", RESULT_LINK_FILE)
        if key in feed["files"]
    }
    groups = _verified_fixture_groups(archive, feed, matches, now) if snapshot else {}
    config = _identity_alias_config()
    targets = []
    for key, match in sorted(
        identities.items(), key=lambda item: (instant(item[1]["kickoff_utc"]), item[0])
    ):
        _, evidence, prediction = predictions[key]
        market = frozen_market(prediction)
        price, odds_captured = odds.get(key, (None, None))
        kickoff = instant(match["kickoff_utc"])
        team_history = {side: history[match[f"{side}_team_id"]] for side in SIDES}
        material = {
            **{k: match[k] for k in ("match_id", "jc_num", "league", *SIDES)},
            "kickoff_utc": kickoff.isoformat(),
            "kickoff_beijing": kickoff.astimezone(BEIJING).isoformat(),
            "cutoff_utc": now.isoformat(),
            "catalog_captured_utc": captured.isoformat(),
            "model_probabilities": prediction["model_probabilities"],
            "top_scores": prediction["top_scores"][:5],
            "half_full_time": prediction.get("half_full_time"),
            "recent": {side: entries[:10] for side, entries in team_history.items()},
            "odds": price,
            "odds_snapshot_utc": odds_captured,
        }
        targets.append(
            {
                "match_id": key,
                "kickoff_utc": material["kickoff_utc"],
                **evidence,
                "material": material,
                "verification_recent": team_history,
                "identity_context": _identity_context(
                    match,
                    team_history,
                    teams,
                    matches,
                    league_teams,
                    groups,
                    config,
                    snapshot,
                    now,
                    pair_fixtures[frozenset(match[f"{side}_team_id"] for side in SIDES)],
                    hashes,
                ),
                "model_probabilities": prediction["model_probabilities"],
                "market_probabilities": market["probabilities"] if market else None,
            }
        )
    return targets
