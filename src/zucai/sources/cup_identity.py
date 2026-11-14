"""Cup-only identity evidence, reusing the existing oriented fixture graphs."""

import csv
import hashlib
import json
import sqlite3
from collections import defaultdict
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

from zucai.sources.apif_verify import infer_aliases
from zucai.sources.fd_ingest import WINDOW, _canonical
from zucai.sources.fd_verify import _read_official, _utc
from zucai.sources.fdorg_verify import _active, _Fixture, _graph, _new_aliases
from zucai.sources.teams import normalize_team

CUP_LEAGUES = {"fdorg": {"欧冠"}, "apif": {"欧罗巴", "欧协联"}}
CUP_CODES = {"fdorg": {"CL": "欧冠"}, "apif": {"3": "欧罗巴", "848": "欧协联"}}


def _scope_leagues(provider, scope):
    codes = scope.get("competition" if provider == "fdorg" else "league_id")
    if codes is None:
        return CUP_LEAGUES[provider]
    return {
        CUP_CODES[provider][code] for code in str(codes).split(",") if code in CUP_CODES[provider]
    }


def identity_capture(games, rejected, scope, raw, metadata, sidecar):
    """The importer has already checked scope, HTTP status and raw SHA256."""
    doc = json.loads(raw)
    filters = doc.get("filters", {})
    restricted = ("status", "matchday", "stage", "group", "limit", "offset")
    complete = not rejected and not any(
        values.get(k) not in (None, "", []) for values in (filters, scope) for k in restricted
    )
    if "matches" in doc:
        count = doc.get("resultSet", {}).get("count")
        complete = complete and type(count) is int and count == len(doc["matches"])
    if "season" in scope:
        complete = complete and not any(filters.get(k) for k in ("dateFrom", "dateTo"))
    if "paging" in doc:
        complete = complete and doc["paging"] == {"current": 1, "total": 1}
    return dict(
        games=games,
        scope=scope,
        complete=complete,
        raw_sha256=metadata["sha256"],
        sidecar=str(sidecar),
        seen_utc=metadata["fetched_at_utc"],
    )


def needs_cup_union(provider, scopes):
    return (
        len(scopes) > 1
        and all(s.get("season") is not None for s in scopes)
        and any(_scope_leagues(provider, scope) for scope in scopes)
    )


def capture_cup_history(source, scopes):
    """Stage complete scopes before import; preserve earlier scopes on a later failure."""
    staged = []
    for scope in scopes:
        try:
            path = source.fetch_raw(**scope)
            raw = path.read_bytes()
            games = source.parse(raw, **scope)
            capture = identity_capture(
                games,
                source.rejected,
                scope,
                raw,
                source.store.load_metadata(path),
                path.with_suffix(path.suffix + ".meta.json"),
            )
            staged.append((path, capture))
        except Exception as exc:
            return staged, exc
    return staged, None


def _interval(scope):
    if scope.get("season") is not None:
        year = int(scope["season"])
        return datetime(year, 7, 1, tzinfo=UTC), datetime(year + 1, 7, 1, tzinfo=UTC)
    if scope.get("date"):
        begin = datetime.fromisoformat(scope["date"]).replace(tzinfo=UTC)
        return begin, begin + timedelta(days=1)
    return tuple(
        datetime.fromisoformat(scope[k]).replace(tzinfo=UTC) for k in ("date_from", "date_to")
    )


def _covered(instant, intervals):
    cursor, end = instant - WINDOW, instant + WINDOW
    for begin, finish in sorted(intervals):
        if begin > cursor:
            return False
        cursor = max(cursor, finish)
        if cursor > end:
            return True
    return False


def _persisted_bindings(database, provider, league):
    prefix = f"{provider}:{league}:team:"
    found = {}
    with sqlite3.connect(database) as con:
        for team_id, canonical, encoded in con.execute("SELECT id,name,aliases FROM teams"):
            canonical = normalize_team(canonical)
            for binding in json.loads(encoded or "[]"):
                if not binding.startswith(prefix):
                    continue
                key = binding.removeprefix(prefix)
                if key in found and found[key]["canonical"] != canonical:
                    raise ValueError("既有来源球队 ID 关联冲突")
                found[key] = dict(
                    canonical=canonical,
                    status="auto_verified",
                    evidence=[dict(kind="database_binding", binding=binding, team_id=team_id)],
                )
    return found


def _bridge(provider, database, league, labels, aliases):
    entries = deepcopy(aliases.get(league, {}))
    scoped = _scoped_ids(provider, aliases)
    known = defaultdict(list)
    for source_league, records in aliases.items():
        # Check the originating league's complete reverse map, including IDs
        # absent from this cup capture. A colliding domestic identity is no anchor.
        if any(_canonical(aliases, source_league, key) for key in labels if key in records):
            _active(aliases, source_league)
        for key in labels:
            if key in records:
                name = _canonical(aliases, source_league, key)
                if name:
                    known[key].append((source_league, name))
    base_sha = hashlib.sha256(json.dumps(aliases, sort_keys=True).encode()).hexdigest()
    for key, record in _persisted_bindings(database, provider, league).items():
        if key not in labels:
            continue
        known[key].append((league, record["canonical"]))
        if key not in scoped and not _canonical({league: entries}, league, key):
            entries[key] = {"name": labels[key], **record}
    for key, names in known.items():
        if len({name for _, name in names}) != 1:
            raise ValueError("跨联赛的同源数字球队 ID 存在规范名冲突")
        if key not in scoped and not _canonical({league: entries}, league, key):
            entries[key] = dict(
                name=labels[key],
                canonical=names[0][1],
                status="auto_verified",
                evidence=[
                    dict(
                        kind="same_provider_id",
                        provider=provider,
                        team_id=key,
                        source_leagues=sorted({name for name, _ in names}),
                        source_aliases_sha256=base_sha,
                    )
                ],
            )
    _active({league: entries}, league)
    return entries


def _scoped_ids(provider, aliases):
    # Legacy bridges and graphs cannot retain the new category/time authorization.
    # Keep their old behavior only for IDs without an explicit APIF scope.
    return {
        key
        for records in aliases.values()
        for key, entry in records.items()
        if provider == "apif" and "binding_scope" in entry
    }


def _audit_aliases(path, rows):
    if path.name == "apif_cup_aliases.csv":
        from zucai.sources.apif_audit import audit_cup_aliases

        return audit_cup_aliases(path, rows)
    fields = ["league", "team_id", "name", "canonical", "status", "reason", "evidence"]
    previous = []
    if path.exists():
        with path.open(encoding="utf-8", newline="") as stream:
            previous = list(csv.DictReader(stream))
    seen = {tuple(r.get(k, "") for k in fields) for r in previous}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        if stream.tell() == 0:
            writer.writeheader()
        for row in rows:
            values = tuple(row.get(k, "") for k in fields)
            if values not in seen:
                writer.writerow(row)
                seen.add(values)


def resolve_cup_aliases(provider, database, aliases, captures, audit_dir):
    """Return runtime aliases; checked-in files and non-cup policies stay untouched."""
    if provider == "apif":
        from zucai.sources.apif_aliases import validate_approvals

        validate_approvals(aliases)
    groups, coverage = defaultdict(dict), defaultdict(list)
    provenance, incomplete = {}, set()
    for capture in captures:
        for game in capture["games"]:
            if game.league not in CUP_LEAGUES[provider]:
                continue
            group, mid = groups[game.league], game.provider_match_id
            if mid in group and (
                group[mid].home_provider_id,
                group[mid].away_provider_id,
                group[mid].kickoff_utc,
            ) != (game.home_provider_id, game.away_provider_id, game.kickoff_utc):
                raise ValueError("跨原件比赛身份冲突")
            group[mid] = game
            provenance[mid] = {k: capture[k] for k in ("raw_sha256", "sidecar", "seen_utc")}
        leagues = _scope_leagues(provider, capture["scope"])
        if capture["complete"]:
            for league in leagues:
                coverage[league].append(_interval(capture["scope"]))
        else:
            incomplete.update(leagues)
    if not groups:
        return aliases
    updated = deepcopy(aliases)
    official = _read_official(database)
    audit = []
    for league, indexed in groups.items():
        games = list(indexed.values())
        labels = {
            str(key): name
            for g in games
            for key, name in ((g.home_provider_id, g.home_raw), (g.away_provider_id, g.away_raw))
        }
        entries = _bridge(provider, database, league, labels, aliases)
        jc = [row for row in official if row["league"] == league]
        valid = league not in incomplete and all(
            _covered(_utc(j["kickoff_utc"]), coverage[league])
            or not any(abs(g.kickoff_utc - _utc(j["kickoff_utc"])) <= WINDOW for g in games)
            for j in jc
        )
        if _scoped_ids(provider, aliases) & labels.keys() or _scoped_ids(
            provider, {league: entries}
        ):
            # Do not remove scoped rows from the competitor graph or let a scoped
            # anchor infer other IDs. Preserve explicit entries for per-game guards.
            valid = False
        if valid and provider == "fdorg":
            identity = [
                _Fixture(
                    g.provider_match_id,
                    g.home_provider_id,
                    g.away_provider_id,
                    g.kickoff_utc,
                    g.home_raw,
                    g.away_raw,
                    g.status,
                    None,
                )
                for g in games
            ]
            graph = _graph(identity, jc, _active({league: entries}, league))
            entries = _new_aliases(
                identity, jc, entries, graph, provenance[games[0].provider_match_id]
            )
            for entry in entries.values():
                for proof in entry.get("evidence", []):
                    if "fdorg_id" in proof:
                        mid = int(proof["fdorg_id"].split(":")[1])
                        if mid in provenance:
                            proof.update(provenance[mid])
        elif valid:
            entries = infer_aliases(games, jc, aliases=entries, provenance=provenance)
        for key, label in labels.items():
            if not _canonical({league: entries}, league, key):
                entries[key] = dict(
                    name=label,
                    canonical=None,
                    status="proposed",
                    reason="缺少完整且双向唯一的主客方向比赛证据",
                )
            entry = entries[key]
            audit.append(
                dict(
                    league=league,
                    team_id=key,
                    name=label,
                    canonical=entry.get("canonical") or "",
                    status=entry["status"],
                    reason=entry.get("reason", ""),
                    evidence=json.dumps(
                        entry.get("evidence", []), ensure_ascii=False, sort_keys=True
                    ),
                )
            )
        _active({league: entries}, league)
        updated[league] = entries
    _audit_aliases(Path(audit_dir) / f"{provider}_cup_aliases.csv", audit)
    return updated


def alignment_counts(games, aliases):
    teams = defaultdict(set)
    for game in games:
        teams[game.league].update((str(game.home_provider_id), str(game.away_provider_id)))
    result = {}
    for league, ids in teams.items():
        aligned = sum(_canonical(aliases, league, key) is not None for key in ids)
        result[league] = dict(
            teams=len(ids),
            aligned_teams=aligned,
            unmatched_teams=len(ids) - aligned,
            alignment_rate=aligned / len(ids) if ids else None,
        )
    return result


def league_counts(database, provider):
    with sqlite3.connect(database) as con:
        rows = con.execute(
            "SELECT m.league,count(*),count(r.match_id) FROM matches m "
            "LEFT JOIN results r USING(match_id) WHERE m.source=? GROUP BY m.league",
            (provider,),
        )
        return {league: dict(matches=matches, results=results) for league, matches, results in rows}
