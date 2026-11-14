"""Read-only, source-verified kickoff evidence for the frozen official JC catalogue.

This module proposes no identities or fixture relationships and never opens a DB.
Only the explicitly dispatched self-hosted probe may acquire new originals.
"""

import argparse
import hashlib
import json
import logging
import os
import re
import time
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from requests import RequestException
from requests.adapters import HTTPAdapter

from zucai.cloud_jc_catalogue import (
    _archived_page,
    _utc,
    parse_day_pages,
    parse_official_leagues,
    require_jc_catalogue_actions,
)
from zucai.sources.base import RawStore, _atomic_write
from zucai.sources.results import GameResult, SportteryResults
from zucai.sources.sporttery import API, BEIJING, POOLS, beijing_kickoff, official_value
from zucai.sources.sporttery_aliases import ALIASES_PATH, approved_canonical
from zucai.sources.teams import normalize_team

LOG = logging.getLogger(__name__)
REGISTRY = Path(__file__).resolve().parents[2] / "data/league_map.json"
GENERIC_IDS = {39, 73, 76}
CATALOGUE_SOURCE_RUN = "43539673769-1"
CATALOGUE_WINDOW = (date(2025, 11, 1), date(2026, 10, 31))
# PR135 explicitly reviewed these 62 identities. Operational daily switches and
# future approvals must not silently alter this approved historical request set.
APPROVED_ROUTE_IDS = frozenset(
    "1 4 6 7 9 10 13 15 16 17 18 22 23 30 32 34 37 38 41 45 46 48 66 73 "
    "79 80 81 89 90 95 96 99 101 102 104 105 114 115 137 143 235 293 294 307 "
    "480 490 524 526 528 529 531 532 547 556 803 897 960 965 1168 1188 1222 1245".split()
)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _pair_sha(raw_sha256: str, metadata_sha256: str) -> str:
    return _sha((raw_sha256 + metadata_sha256).encode("ascii"))


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def _game(row: dict) -> GameResult:
    return GameResult(
        match_id=f"jc:{row['match_id']}",
        league="",
        jc_num="",
        home="",
        away="",
        play_date=date.fromisoformat(row["match_date"]),
        status="",
        full=None,
        half=None,
        team_ids=(row["home_team_id"], row["away_team_id"]),
    )


class TrustedJcContradiction(ValueError):
    """A verified request returned conflicting official identity or time evidence."""

    def __init__(self, reason: str, evidence: dict):
        super().__init__(reason)
        basis = evidence["basis"].removesuffix("_verified") + "_contradiction"
        self.evidence = dict(evidence, basis=basis, reason=reason)


def _head(path: Path, row: dict, root: Path) -> dict:
    """Reverify bytes, HTTP provenance, official IDs and exact clock every time."""
    raw = path.read_bytes()
    metadata_raw = path.with_suffix(path.suffix + ".meta.json").read_bytes()
    meta = json.loads(metadata_raw)
    if path.parent.name == "conflicts":
        name = re.fullmatch(r"([1-9][0-9]*)-([0-9a-f]{64})\.json", path.name)
        if (
            name is None
            or int(name[1]) != row["match_id"]
            or name[2] != _pair_sha(_sha(raw), _sha(metadata_raw))
        ):
            raise ValueError("JC conflict evidence filename/hash mismatch")
    target = urlparse(API + "getMatchHeadV1.qry")
    url = urlparse(meta.get("url") or "")
    captured = _utc(meta.get("fetched_at_utc"), "fetched_at_utc")
    if (
        (url.scheme, url.netloc, url.path) != (target.scheme, target.netloc, target.path)
        or url.fragment
        or parse_qs(url.query) != {"source": ["web"], "sportteryMatchId": [str(row["match_id"])]}
        or type(meta.get("status_code")) is not int
        or meta["status_code"] != 200
        or meta.get("sha256") != _sha(raw)
        or captured > datetime.now(UTC)
    ):
        raise ValueError("JC head HTTP/source/SHA/capture verification failed")
    proof = dict(
        basis="jc_head_raw_verified",
        path=path.relative_to(root).as_posix(),
        sha256=meta["sha256"],
        metadata_sha256=_sha(metadata_raw),
        fetched_at_utc=meta["fetched_at_utc"],
    )
    head = official_value(raw)
    actual = tuple(
        head.get(key) for key in ("sportteryMatchId", "sportteryHomeTeamId", "sportteryAwayTeamId")
    )
    if any(type(value) is not int or value <= 0 for value in actual):
        raise ValueError("JC head missing complete official identity")
    if actual != (row["match_id"], row["home_team_id"], row["away_team_id"]):
        raise TrustedJcContradiction("JC head official match/home/away contradiction", proof)
    # _head_time alone accepts date-only strings through datetime.fromisoformat.
    # Preserve supplied minute/second precision; a real midnight clock is valid.
    clock = head.get("matchDateTime")
    if not isinstance(clock, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}[ T](?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?", clock
    ):
        raise ValueError("JC head missing/malformed exact clock")
    try:
        when = SportteryResults._head_time(raw, _game(row))
    except ValueError as exc:
        raise TrustedJcContradiction(str(exc), proof) from exc
    return dict(proof, kickoff_utc=when.isoformat())


def _catalogue(root: Path) -> tuple[list[dict], list[dict], dict]:
    fixed = root / f"state/catalogue/{CATALOGUE_SOURCE_RUN}/jc.json"
    if not fixed.is_file():
        raise ValueError("JC fixed catalogue source run is missing")
    pointer = dict(
        run_id=CATALOGUE_SOURCE_RUN,
        path=fixed.relative_to(root).as_posix(),
        sha256=_sha(fixed.read_bytes()),
    )
    folder = root / Path(pointer["path"]).parent
    report = json.loads((root / pointer["path"]).read_bytes())
    if report.get("status") not in {"complete", "partial"}:
        raise ValueError("JC catalogue report status invalid")
    start = date.fromisoformat(report["window"]["start_beijing"])
    end = date.fromisoformat(report["window"]["end_beijing"])
    if (start, end) != CATALOGUE_WINDOW:
        raise ValueError("JC fixed catalogue window mismatch")
    day, rows, pages, dictionary, seen = start, [], [], {}, set()
    while day <= end:
        raw_pages, number = [], 1
        while True:
            path = folder / f"jc/{day:%Y-%m}/{day}-p{number}.json"
            archived = _archived_page(
                path.read_bytes(), path.with_suffix(".json.meta.json").read_bytes(), day, number
            )
            raw_pages.append(archived.raw)
            pages.append(
                dict(
                    path=path.relative_to(root).as_posix(),
                    sha256=_sha(archived.raw),
                    metadata_sha256=_sha(archived.sidecar_raw),
                )
            )
            for league in parse_official_leagues(archived.raw):
                old = dictionary.setdefault(league["league_id"], league)
                if old != league:
                    raise ValueError("JC catalogue dictionary conflict")
            if number >= official_value(archived.raw)["pages"]:
                break
            number += 1
        parse_day_pages(day, raw_pages)
        for page_index, raw in enumerate(raw_pages):
            for row in official_value(raw)["matchResult"]:
                sid = row["matchId"]
                if sid in seen:
                    raise ValueError("JC catalogue duplicate matchId")
                seen.add(sid)
                if any(
                    type(row.get(k)) is not int or row[k] <= 0 for k in ("homeTeamId", "awayTeamId")
                ):
                    raise ValueError("JC catalogue missing official numeric team IDs")
                rows.append(
                    dict(
                        match_id=sid,
                        league_id=row["leagueId"],
                        league=row["leagueNameAbbr"],
                        home_name=row["homeTeam"],
                        away_name=row["awayTeam"],
                        match_date=row["matchDate"],
                        home_team_id=row["homeTeamId"],
                        away_team_id=row["awayTeamId"],
                        catalogue_path=pages[len(pages) - len(raw_pages) + page_index]["path"],
                    )
                )
        day += timedelta(days=1)
    expected_files = {item["path"] + suffix for item in pages for suffix in ("", ".meta.json")}
    actual_files = {
        path.relative_to(root).as_posix() for path in (folder / "jc").rglob("*") if path.is_file()
    }
    if actual_files != expected_files:
        raise ValueError("JC catalogue unexpected/missing page or sidecar")
    return (
        rows,
        list(dictionary.values()),
        dict(
            pointer=pointer,
            pages=pages,
            window=report["window"],
            original_status=report["status"],
            recomputed_status="complete",
            distinct_match_count=len(rows),
            page_count=len(pages),
        ),
    )


def _schedule(path: Path, row: dict, root: Path) -> dict:
    raw = path.read_bytes()
    metadata_raw = path.with_suffix(".json.meta.json").read_bytes()
    metadata = json.loads(metadata_raw)
    url = urlparse(metadata.get("url") or "")
    target = urlparse(API + "getMatchCalculatorV1.qry")
    captured = _utc(metadata.get("fetched_at_utc"), "fetched_at_utc")
    if (
        (url.scheme, url.netloc, url.path) != (target.scheme, target.netloc, target.path)
        or url.fragment
        or parse_qs(url.query) != {"poolCode": [",".join(POOLS)], "channel": ["c"]}
        or type(metadata.get("status_code")) is not int
        or metadata["status_code"] != 200
        or metadata.get("sha256") != _sha(raw)
        or captured > datetime.now(UTC)
    ):
        raise ValueError("JC schedule HTTP/source/SHA/capture verification failed")
    if path.parent.name == "jc-schedules" and path.name != (
        f"{_pair_sha(_sha(raw), _sha(metadata_raw))}.json"
    ):
        raise ValueError("JC schedule evidence filename/hash mismatch")
    proof = dict(
        basis="jc_schedule_raw_verified",
        path=path.relative_to(root).as_posix(),
        sha256=_sha(raw),
        metadata_sha256=_sha(metadata_raw),
        fetched_at_utc=metadata["fetched_at_utc"],
    )
    matches = [
        item
        for group in official_value(raw)["matchInfoList"]
        for item in group["subMatchList"]
        if item.get("matchId") == row["match_id"]
    ]
    if len(matches) != 1:
        claims = {
            (
                item.get("homeTeamId"),
                item.get("awayTeamId"),
                item.get("matchDate"),
                item.get("matchTime"),
            )
            for item in matches
        }
        if len(claims) > 1:
            raise TrustedJcContradiction("JC schedule inconsistent duplicate official ID", proof)
        raise ValueError("JC schedule missing/duplicate official match ID")
    item = matches[0]
    if any(
        type(item.get(key)) is not int or item[key] <= 0 for key in ("homeTeamId", "awayTeamId")
    ):
        raise ValueError("JC schedule missing complete official team identity")
    if (item.get("homeTeamId"), item.get("awayTeamId")) != (
        row["home_team_id"],
        row["away_team_id"],
    ):
        raise TrustedJcContradiction("JC schedule official home/away team mismatch", proof)
    clock = f"{item.get('matchDate', '')} {item.get('matchTime', '')}"
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2} (?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?", clock):
        raise ValueError("JC schedule missing/malformed exact clock")
    try:
        when = beijing_kickoff(clock)
    except ValueError as exc:
        raise TrustedJcContradiction(str(exc), proof) from exc
    if when.astimezone(BEIJING).date().isoformat() != row["match_date"]:
        raise TrustedJcContradiction("JC schedule Beijing date mismatch", proof)
    return dict(proof, kickoff_utc=when.isoformat())


def _raw_evidence(path: Path, row: dict, root: Path) -> dict:
    meta = json.loads(path.with_suffix(".json.meta.json").read_bytes())
    if urlparse(meta.get("url") or "").path == urlparse(API + "getMatchCalculatorV1.qry").path:
        return _schedule(path, row, root)
    return _head(path, row, root)


def _database_evidence(
    root: Path, selected: list[dict], catalogue: dict
) -> tuple[dict, list[dict]]:
    """Validate a limited same-JC-ID clock; this grants no numeric team binding."""
    from zucai.cloud_feeds import TABLES

    sources = []
    if (root / "feed_manifest.json").is_file():
        sources.append(
            (
                root / "feed_manifest.json",
                root / "state/matches.jsonl",
                root / "state/teams.jsonl",
                ALIASES_PATH,
            )
        )
    for manifest in sorted((root / "state/catalogue").glob("*/jc-db-manifests/*.json")):
        if re.fullmatch(r"[0-9a-f]{64}\.json", manifest.name):
            folder = manifest.parent.parent
            sources.append(
                (
                    manifest,
                    folder / "jc-db-matches.jsonl",
                    folder / "jc-db-teams.jsonl",
                    folder / "jc-db-aliases.json",
                )
            )
    output, issues = defaultdict(list), []
    page_hashes = {page["path"]: page["sha256"] for page in catalogue["pages"]}
    page_metadata_hashes = {page["path"]: page["metadata_sha256"] for page in catalogue["pages"]}
    current_aliases_hash = _sha(ALIASES_PATH.read_bytes())
    cache = {}
    for manifest_path, matches_path, teams_path, aliases_path in sources:
        manifest_raw = manifest_path.read_bytes()
        manifest = json.loads(manifest_raw)
        if manifest.get("version") not in (1, 2):
            raise ValueError("JC database source manifest version invalid")
        if any(
            manifest.get("columns", {}).get(name) != list(TABLES[name])
            for name in ("matches", "teams")
        ):
            raise ValueError("JC database source columns mismatch")
        file_hashes = {}
        for name, path in [("matches", matches_path), ("teams", teams_path)]:
            if path not in cache:
                cache[path] = path.read_bytes()
            raw = cache[path]
            want = manifest["files"].get(f"state/{name}.jsonl")
            if want != _sha(raw):
                raise ValueError(f"JC database {name} source SHA verification failed")
            file_hashes[name] = want
        aliases_raw = aliases_path.read_bytes()
        if _sha(aliases_raw) != current_aliases_hash:
            raise ValueError("JC database aliases snapshot SHA changed")
        aliases = json.loads(aliases_raw)
        columns = {name: manifest["columns"][name] for name in ("matches", "teams")}
        core = _sha(
            json.dumps(
                dict(files=file_hashes, columns=columns, aliases_sha256=current_aliases_hash),
                sort_keys=True,
            ).encode()
        )
        if manifest_path.parent.name == "jc-db-manifests" and (
            manifest_path.stem != _sha(manifest_raw) or manifest_path.parent.parent.name != core
        ):
            raise ValueError("JC database snapshot filename/SHA mismatch")
        parsed = {}
        for name, path in [("matches", matches_path), ("teams", teams_path)]:
            if (name, path) not in cache:
                parsed_rows = []
                for number, line in enumerate(cache[path].splitlines(), 1):
                    values = json.loads(line)
                    if not isinstance(values, list) or len(values) != len(columns[name]):
                        raise ValueError("JC database source row schema mismatch")
                    parsed_rows.append((dict(zip(columns[name], values, strict=True)), number))
                cache[name, path] = parsed_rows
            parsed[name] = cache[name, path]
            count = manifest.get("counts", {}).get(name)
            if type(count) is not int or count != len(parsed[name]):
                raise ValueError(f"JC database {name} source count mismatch")
        matches, owners, team_lines = {}, defaultdict(set), {}
        for team, number in parsed["teams"]:
            tid = team["id"]
            if tid in team_lines:
                raise ValueError("JC database duplicate project team ID")
            team_lines[tid] = number
            names = team["aliases"]
            if isinstance(names, str):
                names = json.loads(names)
            for name in [team["name"], *names]:
                owners[normalize_team(name).casefold()].add(tid)
        for match, number in parsed["matches"]:
            if match["match_id"] in matches:
                raise ValueError("JC database duplicate match ID")
            matches[match["match_id"]] = match, number
        for row in selected:
            found = matches.get(f"jc:{row['match_id']}")
            if found is None:
                continue
            match, number = found
            try:
                if (
                    match["source"] != "jc"
                    or match["result_provider"] != "jc"
                    or match["league"] != row["league"]
                ):
                    raise ValueError("same-ID database source/provider/league mismatch")
                when = _utc(match["kickoff_utc"], "kickoff_utc")
                checked = _utc(match["result_checked_utc"], "result_checked_utc")
                if when.astimezone(BEIJING).date().isoformat() != row[
                    "match_date"
                ] or checked > datetime.now(UTC):
                    raise ValueError("same-ID database Beijing date/verification time mismatch")
                for side in ("home", "away"):
                    raw_name = row[f"{side}_name"]
                    canonical = approved_canonical(aliases, row["league"], raw_name) or raw_name
                    tid = match[f"{side}_team_id"]
                    if owners[normalize_team(canonical).casefold()] != {tid}:
                        raise ValueError(f"same-ID database {side} project identity is not unique")
                page = row["catalogue_path"]
                if (
                    manifest["files"].get(page) != page_hashes[page]
                    or manifest["files"].get(page + ".meta.json") != page_metadata_hashes[page]
                ):
                    raise ValueError("same-ID database catalogue manifest SHA mismatch")
                output[row["match_id"]].append(
                    dict(
                        basis="db_same_jc_id_verified",
                        kickoff_utc=when.isoformat(),
                        result_checked_utc=match["result_checked_utc"],
                        verified_utc=datetime.now(UTC).isoformat(),
                        manifest_path=manifest_path.relative_to(root).as_posix(),
                        manifest_sha256=_sha(manifest_raw),
                        matches_path=matches_path.relative_to(root).as_posix(),
                        matches_sha256=file_hashes["matches"],
                        teams_path=teams_path.relative_to(root).as_posix(),
                        teams_sha256=file_hashes["teams"],
                        aliases_path=(
                            None
                            if aliases_path == ALIASES_PATH
                            else aliases_path.relative_to(root).as_posix()
                        ),
                        aliases_sha256=current_aliases_hash,
                        snapshot_key=core,
                        matches_row_pointer=f"state/matches.jsonl#/line/{number}",
                        home_team_row_pointer=f"state/teams.jsonl#/line/{team_lines[match['home_team_id']]}",
                        away_team_row_pointer=f"state/teams.jsonl#/line/{team_lines[match['away_team_id']]}",
                        catalogue_path=page,
                        catalogue_sha256=page_hashes[page],
                    )
                )
            except (ValueError, KeyError, TypeError) as exc:
                issues.append(
                    dict(match_id=row["match_id"], basis="db_same_jc_id_unproven", reason=str(exc))
                )
    return output, issues


def _copy_original(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.read_bytes() != source.read_bytes():
            raise ValueError("JC immutable evidence path collision")
    else:
        _atomic_write(target, source.read_bytes())


def _preserve_evidence(evidence: dict, row: dict, origin: Path, root: Path, folder: Path) -> dict:
    if evidence["basis"] == "db_same_jc_id_verified":
        core = root / "state/catalogue" / evidence["snapshot_key"]
        saved = dict(evidence)
        for kind in ("matches", "teams", "aliases", "manifest"):
            original = (
                ALIASES_PATH
                if kind == "aliases" and evidence["aliases_path"] is None
                else origin / evidence[f"{kind}_path"]
            )
            if kind == "manifest":
                target = core / "jc-db-manifests" / f"{evidence['manifest_sha256']}.json"
            else:
                target = core / f"jc-db-{kind}.{'json' if kind == 'aliases' else 'jsonl'}"
            _copy_original(original, target)
            saved[f"{kind}_path"] = target.relative_to(root).as_posix()
        return saved
    original = origin / evidence["path"]
    pair = _pair_sha(evidence["sha256"], evidence["metadata_sha256"])
    if evidence["basis"] in {"jc_schedule_raw_verified", "jc_schedule_raw_contradiction"}:
        target = folder / "jc-schedules" / f"{pair}.json"
    else:
        target = folder / "jc-heads/conflicts" / f"{row['match_id']}-{pair}.json"
    _copy_original(original.with_suffix(".json.meta.json"), target.with_suffix(".json.meta.json"))
    _copy_original(original, target)
    try:
        return _raw_evidence(target, row, root)
    except TrustedJcContradiction as exc:
        return exc.evidence


def plan_kickoffs(archive: Path) -> dict:
    """Freeze the exact selected catalogue IDs, with independent reusable evidence."""
    from zucai.cloud_feeds import _catalogue_files

    root = Path(archive)
    _catalogue_files(root)
    rows, dictionary, catalogue = _catalogue(root)
    registry_raw = REGISTRY.read_bytes()
    registry = json.loads(registry_raw)
    routes = {}
    for code in sorted(APPROVED_ROUTE_IDS, key=int):
        route = registry["providers"]["apif"].get(code)
        if (
            not isinstance(route, dict)
            or route.get("status") != "verified"
            or not any(
                item.get("kind") == "pr_identity_review"
                and item.get("url") == "https://github.com/qw5563680/FOOTBALL/pull/135"
                and item.get("provider") == "apif"
                and item.get("code") == code
                for item in route.get("evidence", [])
            )
        ):
            raise ValueError(f"JC fixed route {code} lacks PR135 approval evidence")
        routes[code] = route
    names = {route["config"]["league"] for route in routes.values()}
    official_ids = {league["league_id"] for league in dictionary if league["league_abbr"] in names}
    official_ids |= GENERIC_IDS
    selected = sorted(
        (row for row in rows if row["league_id"] in official_ids),
        key=lambda row: (row["match_date"], row["match_id"]),
    )
    evidence_paths = defaultdict(list)
    state_heads = list((root / "state/catalogue").glob("*/jc-heads/*.json"))
    state_heads.extend((root / "state/catalogue").glob("*/jc-heads/conflicts/*.json"))
    for path in sorted(state_heads):
        match = re.fullmatch(r"([1-9][0-9]*)(?:-[0-9a-f]{64})?\.json", path.name)
        if match:
            evidence_paths[int(match[1])].append(path)
    # Existing original headers are acceptable even without a completion cursor.
    for path in sorted((root / "raw").rglob("match-*.json")):
        if path.name.endswith(".meta.json"):
            continue
        match = re.match(r"match-([1-9][0-9]*)_", path.name)
        if match and "jc-kickoffs" not in path.relative_to(root / "raw").parts:
            evidence_paths[int(match[1])].append(path)
    schedules = list((root / "state/catalogue").glob("*/jc-schedules/*.json"))
    for path in (root / "raw").rglob("*.json"):
        if (
            path.name.endswith(".meta.json")
            or "jc-kickoffs" in path.relative_to(root / "raw").parts
        ):
            continue
        try:
            meta = json.loads(path.with_suffix(".json.meta.json").read_bytes())
            if (
                urlparse(meta.get("url") or "").path
                == urlparse(API + "getMatchCalculatorV1.qry").path
            ):
                schedules.append(path)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    for path in sorted(schedules):
        try:
            ids = {
                item["matchId"]
                for group in official_value(path.read_bytes())["matchInfoList"]
                for item in group["subMatchList"]
                if type(item.get("matchId")) is int
            }
            for match_id in sorted(ids):
                evidence_paths[match_id].append(path)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    db_evidence, issues = _database_evidence(root, selected, catalogue)
    pending, verified = [], []
    for row in selected:
        evidence = list(db_evidence[row["match_id"]])
        for path in evidence_paths[row["match_id"]]:
            try:
                evidence.append(_raw_evidence(path, row, root))
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                if isinstance(exc, TrustedJcContradiction):
                    evidence.append(exc.evidence)
                issues.append(
                    dict(
                        match_id=row["match_id"],
                        path=path.relative_to(root).as_posix(),
                        reason=str(exc),
                    )
                )
        clocks = {item["kickoff_utc"] for item in evidence if "kickoff_utc" in item}
        contradiction = any(item["basis"].endswith("_raw_contradiction") for item in evidence)
        if len(clocks) == 1 and not contradiction:
            verified.append(dict(row, status="success", evidence=evidence))
        else:
            reason = (
                "conflicting_jc_kickoff_evidence"
                if clocks or contradiction
                else "missing_jc_kickoff_evidence"
            )
            pending.append(dict(row, status="pending", reason=reason, evidence=evidence))
    fixed = dict(
        catalogue=catalogue,
        registry_sha256=_sha(registry_raw),
        aliases_sha256=_sha(ALIASES_PATH.read_bytes()),
        official_league_ids=sorted(official_ids),
        selected=selected,
    )
    fingerprint = _sha(json.dumps(fixed, sort_keys=True, separators=(",", ":")).encode())
    return dict(
        version=1,
        generated_utc=datetime.now(UTC).isoformat(),
        plan_sha256=fingerprint,
        new_route_count=len(routes),
        **fixed,
        selected_count=len(selected),
        new_route_match_count=sum(row["league_id"] not in GENERIC_IDS for row in selected),
        generic_match_count=sum(row["league_id"] in GENERIC_IDS for row in selected),
        pending=pending,
        verified=verified,
        issues=issues,
        pilot=pending[:5],
    )


def _pilot_verified(root: Path, plan: dict) -> bool:
    by_id = {row["match_id"]: row for row in plan["verified"]}
    for path in sorted((root / "state/catalogue").glob("*/jc-kickoffs.json")):
        try:
            report = json.loads(path.read_bytes())
            previous = json.loads((path.parent / "jc-kickoff-plan.json").read_bytes())
            expected = [row["match_id"] for row in previous["pilot"]]
            if (
                report.get("plan_sha256") != plan["plan_sha256"]
                or previous.get("plan_sha256") != plan["plan_sha256"]
                or report.get("pilot_verified") is not True
                or report.get("pilot_ids") != expected
                or not expected
                or len(expected) > 5
                or not 1 <= report["max_requests"] <= 5
                or report["requests"] != len(expected)
            ):
                continue
            successes = {
                row["match_id"]: row for row in report["rows"] if row["status"] == "success"
            }
            if all(
                sid in successes
                and sid in by_id
                and any(
                    item["path"] == f"state/catalogue/{path.parent.name}/jc-heads/{sid}.json"
                    and _head(root / item["path"], by_id[sid], root) == item
                    for item in successes[sid]["evidence"]
                )
                for sid in expected
            ):
                return True
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return False


def collect_kickoffs(
    database: Path, run_id: str, *, max_requests: int = 5, archive: Path | None = None
) -> dict:
    """Acquire read-only headers; archive every attempt and reverify every resumed ID."""
    from zucai.cloud_feeds import CATALOGUE_RUN_ID

    require_jc_catalogue_actions()
    if not isinstance(run_id, str) or not re.fullmatch(CATALOGUE_RUN_ID, run_id):
        raise ValueError("JC kickoff invalid run_id")
    if type(max_requests) is not int or not 1 <= max_requests <= 7000:
        raise ValueError("JC kickoff invalid max_requests")
    root = Path(database).parent
    origin = Path(archive) if archive is not None else root
    plan = plan_kickoffs(origin)
    pilot_before = _pilot_verified(origin, plan)
    if max_requests > 5 and not pilot_before:
        raise ValueError("JC kickoff verified pilot required before full collection")
    folder = root / "state/catalogue" / run_id
    folder.mkdir(parents=True, exist_ok=False)
    # Preserve all independently verified evidence before any request. In particular,
    # legacy competing clocks and manifest-backed DB times must survive clean restore.
    for row in [*plan["pending"], *plan["verified"]]:
        row["evidence"] = [
            _preserve_evidence(e, row, origin, root, folder) for e in row["evidence"]
        ]
    _write(folder / "jc-kickoff-plan.json", plan)
    report = dict(
        collection="jc-kickoffs",
        provider="jc",
        run_id=run_id,
        status="partial",
        plan_sha256=plan["plan_sha256"],
        started_utc=datetime.now(UTC).isoformat(),
        max_requests=max_requests,
        requests=0,
        pilot_verified=False,
        pilot_ids=[row["match_id"] for row in plan["pilot"]],
        rows=list(plan["verified"]),
    )
    source = SportteryResults(store=RawStore(root / "raw/jc-kickoffs" / run_id))
    # Hidden adapter retries would violate the explicit HTTP request budget.
    source.session.mount("https://", HTTPAdapter(max_retries=0))
    for row in report["rows"]:
        row["kickoff_utc"] = row["evidence"][0]["kickoff_utc"]
    todo = plan["pending"][:max_requests]
    stopped = False
    for row in todo:
        if stopped:
            break
        sid = row["match_id"]
        time.sleep(1)  # Every actual request, including the first, waits at least one second.
        report["requests"] += 1
        result = dict(row, status="failed")
        try:
            response = source.session.get(
                API + "getMatchHeadV1.qry",
                params={"source": "web", "sportteryMatchId": str(sid)},
                allow_redirects=False,
            )
            saved = source.store.save_response("sporttery-head", f"match-{sid}", response, "json")
            target = folder / f"jc-heads/{sid}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(
                target.with_suffix(".json.meta.json"),
                saved.with_suffix(".json.meta.json").read_bytes(),
            )
            _atomic_write(target, saved.read_bytes())
            result["raw_path"] = target.relative_to(root).as_posix()
            metadata = source.store.load_metadata(saved)
            result.update(
                raw_sha256=metadata["sha256"],
                fetched_at_utc=metadata["fetched_at_utc"],
                status_code=metadata["status_code"],
                metadata_sha256=_sha(saved.with_suffix(".json.meta.json").read_bytes()),
            )
            result["evidence"] = [_head(target, row, root)]
            # A new clock cannot resolve a contradictory earlier original automatically.
            if row["reason"] == "conflicting_jc_kickoff_evidence":
                raise ValueError("conflicting_jc_kickoff_evidence requires review")
            result.update(status="success", kickoff_utc=result["evidence"][0]["kickoff_utc"])
        except (RequestException, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            result["reason"] = str(exc)
            if not pilot_before:
                stopped = True
        report["rows"].append(result)
        _write(folder / "jc-kickoffs.json", report)
    attempted = {row["match_id"] for row in report["rows"]}
    report["rows"].extend(
        dict(row, status="not_requested")
        for row in plan["pending"]
        if row["match_id"] not in attempted
    )
    report["success_count"] = sum(row["status"] == "success" for row in report["rows"])
    report["failed_count"] = sum(row["status"] == "failed" for row in report["rows"])
    report["not_requested_count"] = sum(row["status"] == "not_requested" for row in report["rows"])
    report["skipped_verified_count"] = len(plan["verified"])
    report["selected_count"] = plan["selected_count"]
    report["pilot_verified"] = (
        bool(
            not pilot_before
            and max_requests <= 5
            and plan["pilot"]
            and len(plan["pilot"]) == report["requests"]
            and report["failed_count"] == 0
        )
        or pilot_before
    )
    report["status"] = (
        "complete" if report["success_count"] == plan["selected_count"] else "partial"
    )
    report["captured_utc"] = datetime.now(UTC).isoformat()
    _write(folder / "jc-kickoffs.json", report)
    return report


def write_summary(report: dict) -> None:
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
            stream.write(
                f"### JC catalogue kickoffs: {report['status']}\n\n"
                f"Selected {report['selected_count']}; success {report['success_count']}; "
                f"failed {report['failed_count']}; not requested {report['not_requested_count']}; "
                f"verified skips {report['skipped_verified_count']}; "
                f"requests {report['requests']}.\n"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--archive", type=Path, required=True)
    plan.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = plan_kickoffs(args.archive)
    _write(args.out, result)
    LOG.info("Selected %d JC matches; pending %d", result["selected_count"], len(result["pending"]))


if __name__ == "__main__":
    main()
