"""Compare observations from a research report with existing, scoped fixture identities."""

import re
import unicodedata
from collections import defaultdict
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

from zucai.sources.sporttery import API as JC_API
from zucai.sources.sporttery import BEIJING

SIDES = ("home", "away")
SOURCES = {
    "webapi.sporttery.cn": "jc",
    "www.sporttery.cn": "jc",
    "sporttery.cn": "jc",
    "api.football-data.org": "fdorg",
    "www.football-data.org": "fdorg",
    "football-data.org": "fdorg",
    "football-data.co.uk": "fd",
    "www.football-data.co.uk": "fd",
    "v3.football.api-sports.io": "apif",
}
FIXTURE_FIELDS = ("league", "kickoff_utc", "home_team_id", "away_team_id")
_WOMEN = re.compile(r"\b(?:women|ladies|w)\b|女足|女队|女隊", re.I)
_YOUTH = re.compile(r"(?<![a-z])u[- ]?([0-9]{2})(?![0-9])|青年(?:队|隊)?", re.I)
_RESERVE = re.compile(r"\b(?:ii|b|jong|castilla|reserves?)\b|b(?:队|隊)|预备|預備", re.I)


def exact_name(value):
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def squad_markers(label):
    """Recognize explicit squad distinctions without altering the matching key."""
    label = exact_name(label)
    youth = _YOUTH.search(label)
    return (
        bool(_WOMEN.search(label)),
        (youth[1] or "youth") if youth else None,
        bool(_RESERVE.search(label)),
    )


def _time(value):
    return datetime.fromisoformat(value)


def _source(url):
    try:
        parts = urlsplit(url)
        if (
            parts.scheme in {"http", "https"}
            and parts.username is None
            and parts.port in (None, 80, 443)
        ):
            return SOURCES.get(parts.hostname)
    except ValueError:
        pass
    return None


def _source_ids(urls, kind):
    for url in urls:
        source = _source(url)
        if source is None:
            continue
        parts = urlsplit(url)
        number = None
        if parts.hostname == "api.football-data.org" and source == "fdorg":
            collection = "matches" if kind == "fixture" else "teams"
            path = re.fullmatch(rf"/v4/{collection}/([1-9][0-9]*)/?", parts.path)
            number = path[1] if path else None
        elif source == "apif" and parts.path == ("/fixtures" if kind == "fixture" else "/teams"):
            ids = parse_qs(parts.query).get("id", ())
            number = ids[0] if len(ids) == 1 else None
        elif kind == "fixture" and url.split("?", 1)[0] == JC_API + "getMatchHeadV1.qry":
            ids = parse_qs(parts.query).get("sportteryMatchId", ())
            number = ids[0] if len(ids) == 1 else None
        if number and re.fullmatch(r"[1-9][0-9]*", number):
            yield source, number


def _compatible(record, league, sources, kickoff):
    return (
        record.get("league") in (None, league)
        and (record.get("source") is None or record["source"] in sources)
        and (not record.get("start_utc") or _time(record["start_utc"]) <= kickoff)
        and (not record.get("end_utc") or kickoff <= _time(record["end_utc"]))
    )


class IdentityIndex:
    def __init__(self, material, target):
        context = material.get("identity_context") or {}
        self.target = {**target, **context.get("target", {})}
        self.snapshot_utc = context.get("snapshot_utc")
        self.snapshot_hashes = context.get("snapshot_hashes", {})
        self.names = defaultdict(list)
        self.canonical_names = {}
        self.provider = defaultdict(list)
        self.fixtures = {}
        self.references = defaultdict(list)
        self.leagues = {exact_name(target["league"])}
        for side in SIDES:
            self.target.setdefault(f"{side}_team_id", exact_name(target[side]))
        for record in context.get("teams", ()):
            self.canonical_names[record["id"]] = record["name"]
            self.names[exact_name(record["name"])].append((record["id"], {}))
            for alias in record.get("aliases", ()):
                self.names[exact_name(alias["name"])].append((record["id"], alias))
            for binding in record.get("provider_ids", ()):
                source = binding["source"]
                number = str(binding["id"]).rsplit(":", 1)[-1]
                self.provider[source, number].append((record["id"], binding))
        for fixture in context.get("fixtures", ()):
            self.fixtures[fixture["id"]] = fixture
            self.leagues.add(exact_name(fixture["league"]))
            for reference in fixture.get("fixture_ids", ()):
                self.references[reference].append(fixture)
        if not context:
            # Older offline inputs have archived canonical labels but no internal IDs.
            for recent in (material.get("recent", {}), material.get("verification_recent", {})):
                for rows in recent.values():
                    for row in rows:
                        for side in SIDES:
                            name = exact_name(row[side])
                            self.names[name].append((name, {}))
            for side in SIDES:
                name = exact_name(target[side])
                self.names[name].append((name, {}))

    def teams(self, label, league, urls, *, kickoff=None):
        if not isinstance(label, str) or not label.strip():
            return set()
        sources = {_source(url) for url in urls} - {None}
        kickoff = _time(kickoff or self.target["kickoff_utc"])
        return {
            team
            for team, scope in self.names[exact_name(label)]
            if _compatible(scope, league, sources, kickoff)
        }

    def linked_fixtures(self, urls):
        return {
            row["id"]: row
            for source, number in _source_ids(urls, "fixture")
            for row in self.references[f"{source}:{number}"]
        }

    def _linked_teams(self, urls, league):
        return {
            team
            for source, number in _source_ids(urls, "team")
            for team, scope in self.provider[source, number]
            if _compatible(scope, league, {source}, _time(self.target["kickoff_utc"]))
        }

    def fixture_pair(self, row):
        fixture = self.fixtures.get(row["match_id"])
        if fixture:
            return tuple(fixture[f"{side}_team_id"] for side in SIDES)
        return tuple(exact_name(row[side]) for side in SIDES)

    def _observe(self, claim):
        target, urls = self.target, claim.get("urls", [])
        issues = []

        def issue(field, reason, *, conflict=False, **detail):
            issues.append({"field": field, "reason": reason, "conflict": conflict, **detail})

        if claim.get("explicit_mismatch"):
            issue("raw_report", "explicit_mismatch", conflict=True)
        target_time = _time(target["kickoff_utc"])
        target_date = target_time.astimezone(BEIJING).date().isoformat()
        if claim.get("date_beijing") and claim["date_beijing"] != target_date:
            issue("date_beijing", "date_conflict", conflict=True)
        if claim.get("kickoff_utc") and _time(claim["kickoff_utc"]) != target_time:
            issue("kickoff_utc", "kickoff_conflict", conflict=True)
        if claim.get("league") and exact_name(claim["league"]) != exact_name(target["league"]):
            known = exact_name(claim["league"]) in self.leagues
            issue("league", "league_conflict" if known else "unverified_league", conflict=known)

        linked = self.linked_fixtures(urls)
        for key, other in linked.items():
            if any(
                (
                    _time(other[field]) != target_time
                    if field == "kickoff_utc"
                    else other[field] != target[field]
                )
                for field in FIXTURE_FIELDS
            ):
                issue("source_fixture", "source_fixture_conflict", conflict=True, match_id=key)
        # A URL by itself is insufficient when the report has no current fixture assertion.
        fixture_proved = target["match_id"] in linked and any(
            claim.get(key) for key in (*SIDES, "kickoff_utc")
        )
        if not claim.get("kickoff_utc") and not fixture_proved:
            issue("kickoff_utc", "missing_verified_kickoff")
        if not fixture_proved and claim.get("kickoff_utc"):
            other_jc = [
                key
                for key, row in self.fixtures.items()
                if key.startswith("jc:")
                and key != target["match_id"]
                and _time(row["kickoff_utc"]) == target_time
                and all(
                    row[field] == target[field]
                    for field in FIXTURE_FIELDS
                    if field != "kickoff_utc"
                )
            ]
            if other_jc:
                issue(
                    "match_id",
                    "ambiguous_fixture",
                    candidates=sorted([target["match_id"], *other_jc]),
                )

        resolved, resolution = {}, {}
        for side in SIDES:
            label, expected = claim.get(side), target[f"{side}_team_id"]
            candidates = self.teams(label, target["league"], urls)
            ids = self._linked_teams(claim.get("team_urls", {}).get(side, ()), target["league"])
            method = "archive_name"
            if len(ids) > 1:
                issue(side, "ambiguous_source_team", candidates=sorted(ids, key=str))
            elif ids and candidates:
                if not candidates & ids:
                    issue(side, "name_and_source_team_conflict", conflict=True)
                else:
                    candidates &= ids
                    method = "source_team"
            elif ids:
                source_team = next(iter(ids))
                if label and squad_markers(label) != squad_markers(
                    self.canonical_names[source_team]
                ):
                    issue(side, "team_squad_conflict", conflict=True, name=label)
                else:
                    candidates = ids
                    method = "source_team"
            if not candidates and not label and fixture_proved:
                candidates = {expected}
                method = "source_fixture"
            resolved[side] = next(iter(candidates)) if len(candidates) == 1 else None
            resolution[side] = method if resolved[side] is not None else None
            if len(candidates) == 1 and expected not in candidates:
                issue(side, "team_conflict", conflict=True, name=label, team_id=resolved[side])
            elif len(candidates) > 1:
                issue(side, "ambiguous_team", name=label, candidates=sorted(candidates, key=str))
            elif not candidates:
                issue(side, "unapproved_name" if label else "missing_name", name=label)
        return {
            **claim,
            "resolved": resolved,
            "resolution": resolution,
            "linked_matches": sorted(linked),
        }, issues

    def verify_report(self, claims):
        observations, issues = [], []
        for claim in claims or [{}]:
            observation, findings = self._observe(claim)
            observations.append(observation)
            issues.extend(findings)
        result = "matched"
        if issues:
            result = "conflict" if any(item["conflict"] for item in issues) else "unverified"
        return {
            "target": {key: self.target[key] for key in ("match_id", *FIXTURE_FIELDS, *SIDES)},
            "identity_snapshot_utc": self.snapshot_utc,
            "identity_snapshot_hashes": self.snapshot_hashes,
            "observations": observations,
            "issues": issues,
            "result": result,
        }
