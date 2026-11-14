"""Reverify T68 frozen originals without restoring, opening or modifying a database."""

import csv
import hashlib
import io
import json
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from zucai import jc_catalogue_kickoffs as kickoffs
from zucai.identity_binding import _clock, _utc
from zucai.sources.api_football import API, FINISHED, ApiFootball
from zucai.sources.apif_aliases import valid_identity_category
from zucai.sources.fd_ingest import _canonical, fd_match_id
from zucai.sources.football_data import (
    ALIASES_PATH as FD_ALIASES,
)
from zucai.sources.football_data import (
    LEAGUES_PATH as FD_LEAGUES,
)
from zucai.sources.football_data import (
    FootballDataCoUk,
    local_kickoff,
    verified_league,
)
from zucai.sources.results import SportteryResults
from zucai.sources.sporttery import official_value
from zucai.sources.sporttery_aliases import ALIASES_PATH as JC_ALIASES
from zucai.sources.sporttery_aliases import approved_canonical

ROOT = Path(__file__).resolve().parents[2]
FORMAL = ROOT / "data/team_aliases/api_football.json"
REGISTRY = ROOT / "data/league_map.json"
NATIONS = ROOT / "data/api_football/nations.json"


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(path):
    return json.loads(path.read_bytes())


def _proof(path, root):
    raw = path.read_bytes()
    sidecar = path.with_suffix(path.suffix + ".meta.json")
    meta_raw = sidecar.read_bytes()
    meta = json.loads(meta_raw)
    captured = _utc(meta["fetched_at_utc"])
    if (
        meta.get("sha256") != _sha(raw)
        or meta.get("status_code") != 200
        or captured > datetime.now(UTC)
    ):
        raise ValueError("raw SHA/HTTP/actual capture verification failed")
    return (
        raw,
        meta,
        dict(
            path=path.relative_to(root).as_posix(),
            root=str(root.resolve()),
            sha256=_sha(raw),
            metadata_sha256=_sha(meta_raw),
            url=meta["url"],
            fetched_at_utc=meta["fetched_at_utc"],
        ),
    )


def _coverage(rows):
    clocks = [_clock(row) for row in rows if _clock(row) is not None]
    if not clocks:
        return {}
    return dict(
        start_utc=(min(clocks) - timedelta(hours=3)).isoformat(),
        end_utc=(max(clocks) + timedelta(hours=3)).isoformat(),
    )


def load_apif(root, configs):
    """All raw competitors survive score rejection; corrupt scopes stay incomplete."""
    root = Path(root)
    groups, issues, invalid_scopes = defaultdict(list), [], set()
    paths = sorted((root / "state/catalogue").glob("*/backfill/api-football/*/fixtures_*.json"))
    paths = [path for path in paths if not path.name.endswith(".meta.json")]
    manifest = (
        _json(root / "feed_manifest.json") if (root / "feed_manifest.json").is_file() else None
    )
    global_invalid = False
    if manifest:
        expected = {
            name
            for name in manifest["files"]
            if not name.endswith(".meta.json")
            and re.fullmatch(
                r"state/catalogue/[^/]+/backfill/api-football/[^/]+/fixtures_[^/]+\.json", name
            )
        }
        missing = expected - {path.relative_to(root).as_posix() for path in paths}
        if missing:
            global_invalid = True
            issues.append(dict(reasons=["missing_apif_originals"], paths=sorted(missing)))
    parser = ApiFootball(
        leagues={code: {"league": config["league"]} for code, config in configs.items()}
    )
    try:
        for path in paths:
            scope = None
            try:
                raw, meta, proof = _proof(path, root)
                url = urlparse(meta["url"])
                query = parse_qs(url.query)
                if (
                    url.scheme + "://" + url.netloc + url.path != API + "/fixtures"
                    or url.fragment
                    or set(query) - {"league", "season", "page"}
                    or any(len(values) != 1 for values in query.values())
                    or not {"league", "season"} <= query.keys()
                ):
                    raise ValueError("APIF original is not an unfiltered season URL")
                code, season = query["league"][0], int(query["season"][0])
                scope = code, season
                doc = parser._document(raw, {"league": code, "season": str(season)})
                if code not in configs:
                    continue
                if manifest and any(
                    manifest["files"].get(path.relative_to(root).as_posix() + suffix)
                    != _sha((root / (path.relative_to(root).as_posix() + suffix)).read_bytes())
                    for suffix in ("", ".meta.json")
                ):
                    raise ValueError("APIF original is not authenticated by frozen manifest")
                games = {g.provider_match_id: g for g in parser.parse(raw)}
                rows = []
                for index, item in enumerate(doc["response"]):
                    f = item.get("fixture", {})
                    league = item.get("league", {})
                    if str(league.get("id")) != code or league.get("season") != season:
                        raise ValueError("APIF row competition/season disagrees with request")
                    mid, home, away = (
                        f.get("id"),
                        item.get("teams", {}).get("home", {}),
                        item.get("teams", {}).get("away", {}),
                    )
                    if (
                        any(
                            type(value) is not int or value <= 0
                            for value in (mid, home.get("id"), away.get("id"))
                        )
                        or home["id"] == away["id"]
                    ):
                        raise ValueError("APIF raw competitor identity missing/invalid")
                    when = None
                    try:
                        stamp = f["timestamp"]
                        if type(stamp) is not int:
                            raise ValueError("invalid timestamp")
                        when = datetime.fromtimestamp(stamp, UTC)
                        if _utc(f["date"]) != when:
                            raise ValueError("APIF date/timestamp disagreement")
                    except (KeyError, ValueError, TypeError, OverflowError) as exc:
                        invalid_scopes.add(scope)
                        issues.append(
                            dict(
                                apif_id=f"apif:{mid}",
                                reasons=["missing_kickoff_evidence"],
                                detail=str(exc),
                                provenance=proof,
                            )
                        )
                    game = games.get(mid)
                    rows.append(
                        dict(
                            match_id=f"apif:{mid}",
                            league=configs[code]["league"],
                            competition_id=code,
                            season=season,
                            home=str(home["id"]),
                            away=str(away["id"]),
                            home_name=home.get("name", ""),
                            away_name=away.get("name", ""),
                            home_category=configs[code].get("identity_category"),
                            away_category=configs[code].get("identity_category"),
                            kickoff_utc=when.isoformat() if when else None,
                            time_precision="second",
                            status="finished"
                            if game and game.status in FINISHED
                            else f.get("status", {}).get("short"),
                            score=list(game.full) if game and game.full else None,
                            regular_time_basis=game.regular_time_basis if game else "missing",
                            complete=True,
                            provenance=[dict(proof, pointer=f"/response/{index}")],
                            score_fields=item.get("score"),
                            raw_status=f.get("status", {}).get("short"),
                        )
                    )
                groups[code, season, path.relative_to(root).parts[2]].append(
                    (doc["paging"], rows, proof)
                )
            except (OSError, KeyError, ValueError, TypeError, AttributeError) as exc:
                if scope:
                    invalid_scopes.add(scope)
                else:
                    global_invalid = True
                issues.append(
                    dict(
                        path=path.relative_to(root).as_posix(),
                        reasons=["incomplete_apif_original_scope"],
                        detail=str(exc),
                    )
                )
    finally:
        parser.session.close()
    output, complete_scopes = [], set()
    for scope, captures in sorted(groups.items()):
        totals = {paging["total"] for paging, _, _ in captures}
        pages = {paging["current"] for paging, _, _ in captures}
        complete = (
            not global_invalid
            and scope[:2] not in invalid_scopes
            and len(totals) == 1
            and pages == set(range(1, next(iter(totals)) + 1))
        )
        if complete:
            complete_scopes.add(scope)
        rows = [row for _, captured, _ in captures for row in captured]
        # Repeated complete captures are kept as original evidence; contradictory
        # fixture IDs are rejected by the engine, never resolved by newest-wins.
        coverage = _coverage(rows)
        if not complete:
            issues.append(
                dict(
                    competition_id=scope[0],
                    season=scope[1],
                    reasons=["incomplete_competitor_window"],
                )
            )
        for row in rows:
            row.update(complete=complete, coverage=coverage)
        output.extend(rows)
    return (
        output,
        issues,
        dict(
            raw_files=len(paths),
            scopes=len(groups),
            raw_competitor_rows=len(output),
            complete_scopes=len(complete_scopes),
            original_issues=len(issues),
        ),
    )


def _tables(root):
    from zucai.cloud_feeds import TABLES

    manifest_path = root / "feed_manifest.json"
    manifest = _json(manifest_path)
    tables = {}
    for name in ("teams", "matches", "results"):
        path = root / f"state/{name}.jsonl"
        raw = path.read_bytes()
        if manifest["files"].get(f"state/{name}.jsonl") != _sha(raw):
            raise ValueError(f"Frozen {name} table SHA mismatch")
        columns = manifest["columns"][name]
        if columns != list(TABLES[name]):
            raise ValueError(f"Frozen {name} schema differs from the project schema")
        rows, seen = [], set()
        for index, line in enumerate(raw.splitlines(), 1):
            values = json.loads(line)
            if not isinstance(values, list) or len(values) != len(columns):
                raise ValueError(f"Frozen {name} row schema mismatch")
            if values[0] in seen:
                raise ValueError(f"Frozen {name} duplicate primary key")
            seen.add(values[0])
            rows.append(dict(zip(columns, values, strict=True), row_number=index))
        if len(rows) != manifest["counts"][name]:
            raise ValueError(f"Frozen {name} row count mismatch")
        tables[name] = rows
    return tables, dict(path=str(manifest_path), sha256=_sha(manifest_path.read_bytes()))


def _fd_day(value, zone):
    from zoneinfo import ZoneInfo

    fmt = "%d/%m/%Y" if len(value.split("/")[-1]) == 4 else "%d/%m/%y"
    low = datetime.strptime(value, fmt).replace(tzinfo=ZoneInfo(zone))
    high = low + timedelta(days=1)
    return low.astimezone(UTC).isoformat(), high.astimezone(UTC).isoformat()


def load_fd(directory, teams, configs, *, league_codes=None):
    """Auxiliary FD originals have their own roots/capture times, never relabelled."""
    directory = Path(directory)
    aliases, leagues = _json(FD_ALIASES), _json(FD_LEAGUES)
    names = defaultdict(set)
    for key, team in teams.items():
        names[team["name"]].add(key)
    output, issues = [], []
    seen_hashes = set()
    unattributed_failure = False
    for path in sorted(directory.rglob("*.csv")):
        league, code, proof = None, None, None
        try:
            meta = _json(path.with_suffix(".csv.meta.json"))
            url = urlparse(meta["url"])
            match = re.fullmatch(
                r"/(?:new/([A-Z]{3})|mmz4281/(\d{4})/([A-Z][A-Z0-9]+))\.csv", url.path
            )
            if (
                url.scheme != "https"
                or url.netloc not in {"www.football-data.co.uk", "football-data.co.uk"}
                or not match
                or url.query
                or url.fragment
            ):
                raise ValueError("FD original URL is not an approved full CSV")
            code, season = match[1] or match[3], match[2]
            if code not in leagues:
                continue
            config = verified_league(leagues, code)
            league = config["league"]
            if league_codes is not None and league not in league_codes:
                continue
            if (
                meta.get("status_code") in {301, 302, 303, 307, 308}
                and path.read_bytes() == b""
                and meta.get("sha256") == _sha(b"")
            ):
                issues.append(
                    dict(
                        path=str(path),
                        reasons=["fd_redirect_observation"],
                        status_code=meta["status_code"],
                    )
                )
                continue
            raw, meta, proof = _proof(path, directory)
            if proof["sha256"] in seen_hashes:
                continue
            seen_hashes.add(proof["sha256"])
            parser = FootballDataCoUk(leagues=leagues, div_code=code, season=season)
            try:
                games = {game.row_number: game for game in parser.parse(raw)}
            finally:
                parser.session.close()
            text = raw.decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
            rows = []
            for index, rawrow in enumerate(reader, 2):
                values = {k: (v or "").strip() for k, v in rawrow.items() if k is not None}
                if not any(values.values()):
                    continue
                home, away = (
                    values.get("HomeTeam", values.get("Home")),
                    values.get("AwayTeam", values.get("Away")),
                )
                if not home or not away:
                    issues.append(
                        dict(
                            path=str(path),
                            pointer=f"/csv/line/{index}",
                            reasons=["fd_competitor_identity_unverified"],
                        )
                    )
                game = games.get(index)
                try:
                    when = local_kickoff(
                        values["Date"], values.get("Time", ""), config["time_basis"]
                    )
                except ValueError:
                    when = None
                canonical = {}
                for side, label in (("home", home), ("away", away)):
                    target = _canonical(aliases, league, label)
                    owners = names.get(target, set())
                    canonical[side] = (
                        next(iter(owners)) if len(owners) == 1 else f"unresolved:fd:{code}:{label}"
                    )
                mid = fd_match_id(game) if game else f"fd:{code}:raw:{proof['sha256']}:{index}"
                rows.append(
                    dict(
                        match_id=mid,
                        league=league,
                        competition_id=(league_codes or {}).get(league),
                        season=int(values.get("Season", "2000")[:4])
                        if not season
                        else 2000 + int(season[:2]),
                        **canonical,
                        home_source_id=home,
                        away_source_id=away,
                        kickoff_utc=when.isoformat() if when else None,
                        time_precision="minute",
                        status="finished" if game else "unverified",
                        match_date=None,
                        possible_start_utc=_fd_day(values["Date"], config["time_basis"])[0],
                        possible_end_utc=_fd_day(values["Date"], config["time_basis"])[1],
                        score=list(game.full) if game and game.full else None,
                        regular_time_basis="regular" if game else "missing",
                        complete=True,
                        provenance=[
                            dict(
                                proof,
                                pointer=f"/csv/line/{index}",
                                timezone=config["time_basis"],
                                timezone_config_sha256=_sha(FD_LEAGUES.read_bytes()),
                                canonical_aliases_sha256=_sha(FD_ALIASES.read_bytes()),
                            )
                        ],
                    )
                )
            coverage = _coverage(rows)
            for row in rows:
                row.update(coverage=coverage, complete=True)
            output.extend(rows)
        except (OSError, ValueError, KeyError, TypeError, csv.Error) as exc:
            issues.append(dict(path=str(path), reasons=["fd_original_unverified"], detail=str(exc)))
            if league is None:
                unattributed_failure = True
            else:
                # The request establishes the league, but a structural failure
                # supplies no safe narrower competitor window. Retain a blocker.
                lower, upper = "1900-01-01T00:00:00Z", "9999-12-31T23:59:59Z"
                output.append(
                    dict(
                        match_id=f"fd:{code}:invalid:{_sha(str(path).encode())}",
                        league=league,
                        competition_id=(league_codes or {}).get(league),
                        home="unresolved:fd:home",
                        away="unresolved:fd:away",
                        kickoff_utc=None,
                        time_precision="date",
                        status="unverified",
                        score=None,
                        regular_time_basis="missing",
                        complete=False,
                        possible_start_utc=lower,
                        possible_end_utc=upper,
                        coverage=dict(start_utc=lower, end_utc=upper),
                        provenance=[proof or dict(path=str(path), verification="failed")],
                    )
                )
    if unattributed_failure:
        for row in output:
            row["complete"] = False
    return output, issues


def select_clock_evidence(row, db_evidence, plan, root, *, extra_paths=()):
    """A verified head contradiction cannot be washed away by a DB fallback."""
    pending = next((item for item in plan["pending"] if item["match_id"] == row["match_id"]), None)
    if pending and pending["reason"] == "conflicting_jc_kickoff_evidence":
        return [], [
            dict(
                match_id=row["match_id"], reasons=[pending["reason"]], evidence=pending["evidence"]
            )
        ]
    verified = next(
        (item for item in plan["verified"] if item["match_id"] == row["match_id"]), None
    )
    evidence = list(verified["evidence"] if verified else db_evidence)
    issues, contradiction = [], False
    for path in extra_paths:
        try:
            evidence.append(kickoffs._raw_evidence(path, row, root))
        except kickoffs.TrustedJcContradiction as exc:
            evidence.append(exc.evidence)
            contradiction = True
        except (OSError, ValueError, KeyError, TypeError) as exc:
            issues.append(
                dict(match_id=row["match_id"], reasons=["jc_raw_clock_unverified"], detail=str(exc))
            )
    clocks = {_utc(item["kickoff_utc"]) for item in evidence if "kickoff_utc" in item}
    if contradiction or len(clocks) > 1:
        return [], [
            dict(
                match_id=row["match_id"],
                reasons=["conflicting_jc_kickoff_evidence"],
                evidence=evidence,
            ),
            *issues,
        ]
    return evidence, issues


def official_entities(rows, matches):
    """Collapse existing links only after checking every node in the component."""
    parents = {}
    stored = {row["match_id"]: row for row in matches}

    def find(key):
        parents.setdefault(key, key)
        if parents[key] != key:
            parents[key] = find(parents[key])
        return parents[key]

    for match in matches:
        if match.get("counterpart_id"):
            parents[find(match["counterpart_id"])] = find(match["match_id"])
    groups = defaultdict(list)
    for row in rows:
        groups[find(row["match_id"])].append(row)
    members = defaultdict(set)
    for mid in list(parents):
        members[find(mid)].add(mid)
    output, issues = [], []
    for component, copies in groups.items():
        if len(copies) == 1:
            output.extend(copies)
            continue
        selected = {row["match_id"]: row for row in copies}
        ids = members[component]
        providers = [mid.split(":")[0] for mid in ids]
        semantic = {
            (row["league"], row["home"], row["away"], _clock(row), tuple(row["score"] or []))
            for row in copies
        }
        problem = len(providers) != len(set(providers)) or len(semantic) != 1
        anchor = copies[0]
        for mid in ids:
            record = stored.get(mid)
            if record is None:
                # A retained original can supply a virtual endpoint, but there is
                # no permission to invent a missing intermediate snapshot.
                if mid not in selected:
                    problem = True
                continue
            try:
                correct_identity = (
                    record["source"] == mid.split(":")[0]
                    and record["league"] == anchor["league"]
                    and str(record["home_team_id"]) == anchor["home"]
                    and str(record["away_team_id"]) == anchor["away"]
                )
                when = _utc(record["kickoff_utc"])
                correct_time = _clock(anchor) is not None and abs(
                    when - _clock(anchor)
                ) <= timedelta(hours=3)
                if mid in selected:
                    correct_time = correct_time and when == _clock(selected[mid])
                if not correct_identity or not correct_time:
                    problem = True
            except (KeyError, ValueError, TypeError, AttributeError):
                problem = True
        if problem:
            issues.append(
                dict(entity_members=sorted(ids), reasons=["linked_entity_evidence_conflict"])
            )
            output.extend(dict(row, complete=False) for row in copies)
            continue
        first = dict(
            min(copies, key=lambda row: (not row["match_id"].startswith("jc:"), row["match_id"]))
        )
        first.update(
            entity_members=sorted(ids),
            complete=all(row["complete"] for row in copies),
            provenance=[proof for row in copies for proof in row["provenance"]],
            existing_component_snapshots=[stored[mid] for mid in sorted(ids) if mid in stored],
        )
        output.append(first)
    return output, issues


def load_archive(root, *, leagues=None, fd_raw_dir=None):
    root = Path(root)
    tables, manifest = _tables(root)
    formal, registry, nations = _json(FORMAL), _json(REGISTRY), _json(NATIONS)
    configs = {
        code: entry["config"]
        for code, entry in registry["providers"]["apif"].items()
        if entry["status"] == "verified"
        and (leagues is None or code in leagues or entry["config"]["league"] in leagues)
    }
    league_codes = {config["league"]: code for code, config in configs.items()}
    teams, occupied, names = {}, defaultdict(set), defaultdict(set)
    categories = defaultdict(set)
    for entries in formal.values():
        for entry in entries.values():
            category = entry.get("binding_scope", {}).get("category", entry.get("category"))
            if entry.get("status") in {"approved", "auto_verified"} and valid_identity_category(
                category
            ):
                categories[entry["canonical"]].add(category)
    for team in nations["teams"].values():
        categories[team["canonical"]].add("national/men/senior/standard")
    for row in tables["teams"]:
        key = str(row["id"])
        available = categories[row["name"]]
        teams[key] = dict(
            name=row["name"],
            category=next(iter(available)) if len(available) == 1 else None,
            provenance=[dict(manifest, pointer=f"state/teams.jsonl#/line/{row['row_number']}")],
        )
        names[row["name"]].add(key)
        for alias in (
            json.loads(row["aliases"]) if isinstance(row["aliases"], str) else row["aliases"]
        ):
            match = re.fullmatch(r"apif:[^:]+:team:([1-9][0-9]*)", alias)
            if match:
                occupied[match[1]].add(key)
    for ids in names.values():
        if len(ids) > 1:
            for key in ids:
                teams[key]["ambiguous"] = True
    apif, issues, apif_summary = load_apif(root, configs)
    # Only explicit adult national registry group + formal nations ID grants this
    # category. Generic International/qualifying/cup rows remain outside A.
    adult_codes = set()
    for competition in registry["competitions"]:
        if competition["master_fields"]["分组"].startswith("7 ") and competition[
            "project_name"
        ] not in {"国际赛", "世预赛", "杯赛"}:
            adult_codes.update(
                m["code"]
                for m in competition["mappings"]
                if m["provider"] == "apif" and m["status"] == "verified"
            )
    for row in apif:
        if row["competition_id"] in adult_codes:
            for side in ("home", "away"):
                if row[side] in nations["teams"]:
                    row[f"{side}_category"] = "national/men/senior/standard"
                    row.setdefault("category_evidence", []).append(
                        dict(
                            path=str(NATIONS),
                            sha256=_sha(NATIONS.read_bytes()),
                            pointer=f"/teams/{row[side]}",
                            registry_sha256=_sha(REGISTRY.read_bytes()),
                        )
                    )
    catalogue_rows, _, catalogue = kickoffs._catalogue(root)
    db_clocks, db_issues = kickoffs._database_evidence(root, catalogue_rows, catalogue)
    issues.extend(dict(item, reasons=["jc_db_evidence_unverified"]) for item in db_issues)
    head_plan = kickoffs.plan_kickoffs(root)
    extra_paths = defaultdict(list)
    for path in sorted((root / "state/catalogue").glob("*/jc-heads/*.json")):
        if path.stem.isdigit():
            extra_paths[int(path.stem)].append(path)
    for path in sorted((root / "raw").rglob("match-*.json")):
        match = re.match(r"match-([1-9][0-9]*)_", path.name)
        if match and not path.name.endswith(".meta.json"):
            extra_paths[int(match[1])].append(path)
    matches = {row["match_id"]: row for row in tables["matches"]}
    official, page_cache = [], {}
    jc_aliases = _json(JC_ALIASES)
    coverage = dict(start_utc="2025-10-31T16:00:00Z", end_utc="2026-10-31T16:00:00Z")
    for row in catalogue_rows:
        league = row["league"]
        if row["league_id"] in kickoffs.GENERIC_IDS or league not in league_codes:
            continue
        path = row["catalogue_path"]
        if path not in page_cache:
            raw, _, proof = _proof(root / path, root)
            page_cache[path] = (
                {
                    item["matchId"]: (i, item)
                    for i, item in enumerate(official_value(raw)["matchResult"])
                },
                proof,
            )
        index, original = page_cache[path][0][row["match_id"]]
        proof = dict(page_cache[path][1], pointer=f"/value/matchResult/{index}")
        clocks, clock_issues = select_clock_evidence(
            row,
            db_clocks.get(row["match_id"], []),
            head_plan,
            root,
            extra_paths=extra_paths[row["match_id"]],
        )
        issues.extend(clock_issues)
        times = {item["kickoff_utc"] for item in clocks if "kickoff_utc" in item}
        when = next(iter(times)) if len(times) == 1 else None
        match = matches.get(f"jc:{row['match_id']}")
        canonical = {}
        for side in ("home", "away"):
            authorized = approved_canonical(jc_aliases, league, row[f"{side}_name"])
            owners = names.get(authorized, set())
            if (
                match
                and clocks
                and any(item["basis"] == "db_same_jc_id_verified" for item in clocks)
            ):
                canonical[side] = str(match[f"{side}_team_id"])
            else:
                canonical[side] = (
                    next(iter(owners))
                    if len(owners) == 1
                    else f"unresolved:jc:{row[f'{side}_team_id']}"
                )
        try:
            game = SportteryResults._row(original)
        except (ValueError, KeyError):
            game = None
        official.append(
            dict(
                match_id=f"jc:{row['match_id']}",
                league=league,
                competition_id=league_codes[league],
                home_source_id=str(row["home_team_id"]),
                away_source_id=str(row["away_team_id"]),
                **canonical,
                kickoff_utc=when,
                time_precision="second" if when else "date",
                match_date=row["match_date"],
                status=game.status if game else "unverified",
                score=list(game.full) if game and game.full else None,
                regular_time_basis="regular" if game and game.full else "missing",
                complete=True,
                coverage=coverage,
                provenance=[proof, *clocks],
            )
        )
    if fd_raw_dir:
        fd, fd_issues = load_fd(fd_raw_dir, teams, configs, league_codes=league_codes)
        official.extend(fd)
        issues.extend(fd_issues)
    official, entity_issues = official_entities(official, tables["matches"])
    issues.extend(entity_issues)
    new_leagues = {
        configs[code]["league"] for code in kickoffs.APPROVED_ROUTE_IDS if code in configs
    }
    catalogue_ids = {f"jc:{row['match_id']}" for row in catalogue_rows}
    new_finished = [
        row
        for row in tables["matches"]
        if row["source"] == "jc" and row["league"] in new_leagues and row["status"] == "finished"
    ]
    return dict(
        apif=apif,
        official=official,
        teams=teams,
        formal=formal,
        occupied=occupied,
        issues=issues,
        summary=dict(
            manifest=manifest,
            new_route_leagues=sorted(new_leagues),
            new_route_finished_db_count=len(new_finished),
            new_route_finished_in_catalogue=sum(
                row["match_id"] in catalogue_ids for row in new_finished
            ),
            new_route_finished_outside_catalogue=[
                row for row in new_finished if row["match_id"] not in catalogue_ids
            ],
            apif=apif_summary,
            catalogue_matches=len(catalogue_rows),
            jc_verified_clocks=sum(
                _clock(row) is not None for row in official if row["match_id"].startswith("jc:")
            ),
            fd_raw_dir=str(fd_raw_dir) if fd_raw_dir else None,
            formal_aliases_sha256=_sha(FORMAL.read_bytes()),
            registry_sha256=_sha(REGISTRY.read_bytes()),
            nations_sha256=_sha(NATIONS.read_bytes()),
        ),
    )
