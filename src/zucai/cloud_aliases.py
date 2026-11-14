"""Accumulate review-only candidates from complete, safely archived capture unions."""

import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

from zucai.league_registry import daily_provider_config
from zucai.sources.api_football import ApiFootball
from zucai.sources.apif_command import ALIASES as APIF_ALIASES
from zucai.sources.apif_verify import infer_aliases
from zucai.sources.base import RawStore, _atomic_write
from zucai.sources.fd_ingest import WINDOW
from zucai.sources.fd_verify import _read_official, _utc
from zucai.sources.fdorg_command import ALIASES as FDORG_ALIASES
from zucai.sources.fdorg_verify import _active, _Fixture, _graph, _new_aliases
from zucai.sources.football_data import read_json
from zucai.sources.football_data_org import FootballDataOrg


def _covered(instant, intervals):
    """The entire inclusive +/-180-minute neighborhood must be observed."""
    cursor, high = instant - WINDOW, instant + WINDOW
    for begin, end in sorted(intervals):
        if begin > cursor:
            return False
        cursor = max(cursor, end)
        if cursor > high:  # HTTP dateTo and next-midnight bounds are exclusive.
            return True
    return False


def _identity(game):
    return game.league, game.home_provider_id, game.away_provider_id, game.kickoff_utc


def _validate_candidates(candidates, aliases):
    for league, records in candidates.items():
        combined = dict(aliases.get(league, {}))
        for key, entry in records.items():
            prior = combined.get(key)
            if prior and prior.get("status") in {"approved", "auto_verified"}:
                if prior["canonical"] != entry["canonical"]:
                    raise ValueError("候选与正式别名冲突")
            combined[key] = entry
        try:
            _active({league: combined}, league)
        except ValueError:
            raise ValueError("候选存在主客身份或反向名称冲突，须人工核验") from None


def _rejected_leagues(provider, source, raw):
    """Use original numeric competition IDs, never a rejected row's display label."""
    doc = json.loads(raw)
    rows = doc["response" if provider == "apif" else "matches"]
    if provider == "apif":
        if doc["paging"] != {"current": 1, "total": 1}:
            return None
        # Missing/invalid IDs may have been filtered before the parser rejected them.
        if any(
            not isinstance(row, dict)
            or not isinstance(row.get("league"), dict)
            or type(row["league"].get("id")) is not int
            or row["league"]["id"] <= 0
            for row in rows
        ):
            return None
    blocked = set()
    for rejection in source.rejected:
        number = rejection.get("row_number")
        if type(number) is not int or not 1 <= number <= len(rows):
            return None
        row = rows[number - 1]
        competition = (
            row.get("league" if provider == "apif" else "competition", {})
            if isinstance(row, dict)
            else {}
        )
        if (
            not isinstance(competition, dict)
            or type(competition.get("id")) is not int
            or competition["id"] <= 0
        ):
            return None
        if provider == "apif":
            config = source.leagues.get(str(competition["id"]))
        else:
            code = source.league_codes.get(str(competition["id"]))
            if competition.get("code") != code:
                return None
            config = source.competitions.get(code)
        if not config:
            return None
        blocked.add(config["league"])
    return blocked


def write_candidates(provider, database, raw_dir, captures, output):
    """Reuse T09/T10 graphs once per league, retaining every competing captured fixture."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"{provider}.json"
    candidates = read_json(target) if target.exists() else {}
    aliases = read_json(FDORG_ALIASES if provider == "fdorg" else APIF_ALIASES)
    official = _read_official(database)
    store = RawStore(raw_dir)
    source = (
        FootballDataOrg(store=store)
        if provider == "fdorg"
        else ApiFootball(store=store, leagues=daily_provider_config("apif"))
    )
    paths = {}
    groups, coverage = defaultdict(dict), defaultdict(list)
    provenance, identities = {}, {}
    new_count = 0
    try:
        for sidecar in (Path(raw_dir) / source.name).rglob("*.meta.json"):
            meta = read_json(sidecar)
            if meta.get("status_code") == 200:
                paths[meta["sha256"]] = sidecar
        for capture in captures:
            sha, scope = capture["raw_sha256"], capture["scope"]
            sidecar = paths[sha]
            raw = sidecar.with_suffix("").with_suffix("").read_bytes()
            if sha256(raw).hexdigest() != sha:
                raise ValueError("候选原件哈希不符")
            games = source.parse(raw, **scope)
            blocked = _rejected_leagues(provider, source, raw)
            # Unknown attribution or incomplete paging removes the whole capture.
            if blocked is None:
                continue
            if provider == "fdorg":
                begin = datetime.fromisoformat(scope["date_from"]).replace(tzinfo=UTC)
                end = datetime.fromisoformat(scope["date_to"]).replace(tzinfo=UTC)
                leagues = [
                    source.competitions[c]["league"] for c in scope["competition"].split(",")
                ]
            else:
                begin = datetime.fromisoformat(scope["date"]).replace(tzinfo=UTC)
                end = begin + timedelta(days=1)
                leagues = [config["league"] for config in source.leagues.values()]
            for league in leagues:
                if league not in blocked:
                    coverage[league].append((begin, end))
            for game in games:
                if game.league in blocked:
                    continue
                mid = game.provider_match_id
                if mid in identities and identities[mid] != _identity(game):
                    raise ValueError("跨原件比赛身份冲突，须人工核验")
                identities[mid] = _identity(game)
                groups[game.league][mid] = game
                provenance[mid] = dict(
                    raw_sha256=sha,
                    sidecar=sidecar.relative_to(raw_dir).as_posix(),
                    coverage_start_utc=begin.isoformat(),
                    coverage_end_exclusive_utc=end.isoformat(),
                )
        for league, indexed in groups.items():
            games = list(indexed.values())
            entries = aliases.get(league, {})
            jc = [j for j in official if j["league"] == league]
            # Keep all JC competitors. If an edge touches an unobserved boundary,
            # defer this league rather than let an unsafe anchor propagate aliases.
            if any(
                not _covered(_utc(j["kickoff_utc"]), coverage[league])
                and any(abs(g.kickoff_utc - _utc(j["kickoff_utc"])) <= WINDOW for g in games)
                for j in jc
            ):
                continue
            if provider == "fdorg":
                games = [
                    _Fixture(
                        provider_match_id=g.provider_match_id,
                        home_provider_id=g.home_provider_id,
                        away_provider_id=g.away_provider_id,
                        kickoff_utc=g.kickoff_utc,
                        home_raw=g.home_raw,
                        away_raw=g.away_raw,
                        provider_status=g.status,
                        import_rejection=None,
                    )
                    for g in games
                ]
                graph = _graph(games, jc, _active({league: entries}, league))
                inferred = _new_aliases(
                    games, jc, entries, graph, provenance[games[0].provider_match_id]
                )
            else:
                inferred = infer_aliases(games, jc, aliases=entries, provenance=provenance)
            for key, entry in inferred.items():
                if entry.get("status") != "auto_verified" or entry == entries.get(key):
                    continue
                # T09's single-archive helper is supplied each witness's actual origin.
                if provider == "fdorg":
                    for evidence in entry["evidence"]:
                        mid = int(evidence["fdorg_id"].split(":")[1])
                        evidence.update(provenance[mid])
                prior = candidates.setdefault(league, {}).get(key)
                if prior and prior["canonical"] != entry["canonical"]:
                    raise ValueError("候选与历史证据冲突，须人工核验")
                if prior:
                    entry["evidence"] = list(prior["evidence"]) + [
                        e for e in entry["evidence"] if e not in prior["evidence"]
                    ]
                else:
                    new_count += 1
                candidates[league][key] = entry
        _validate_candidates(candidates, aliases)
    finally:
        source.session.close()
    _atomic_write(target, (json.dumps(candidates, ensure_ascii=False, indent=2) + "\n").encode())
    return new_count
