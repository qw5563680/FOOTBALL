"""Close archived JC candidates when approved evidence already agrees with the database."""

import json
import re
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from zucai.sources import sporttery_aliases
from zucai.sources.apif_aliases import binding_scope_allows, validate_approvals
from zucai.sources.base import _atomic_write
from zucai.sources.football_data import CONFIG_ROOT, read_json
from zucai.sources.teams import normalize_team

QUEUE = Path("state/alias_candidates/jc.json")
ALIAS_FILES = {
    "apif": "api_football.json",
    "fdorg": "football_data_org.json",
    "jc": "sporttery.json",
    "fd": "football_data_co_uk.json",
}
APPROVAL_FIELDS = ("approval_pr", "approved_by", "approved_utc", "approval_source")


def _name_key(name):
    return normalize_team(name).casefold()


def _stamp(value):
    try:
        timestamp = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return timestamp if timestamp.utcoffset() is not None else None


def _observed_matches(connection, league, entry, recorded):
    """Return the same official matches, with their current home/away owners."""
    observations = entry.get("evidence")
    if not isinstance(observations, list) or not observations:
        return []
    matches = []
    for observation in observations:
        if not isinstance(observation, dict):
            return []
        match_id = observation.get("match_id")
        side = observation.get("side")
        if (
            side not in {"home", "away"}
            or not isinstance(match_id, str)
            or not match_id.startswith("jc:")
        ):
            return []
        captured = _stamp(observation.get("captured_utc"))
        kickoff = _stamp(observation.get("kickoff_utc"))
        if captured is None or captured > recorded or kickoff is None:
            return []
        match = connection.execute(
            "SELECT match_id,league,home_team_id,away_team_id,kickoff_utc,counterpart_id "
            "FROM matches WHERE match_id=? AND source='jc' AND league=?",
            (match_id, league),
        ).fetchone()
        if match is None or _stamp(match["kickoff_utc"]) != kickoff:
            return []
        matches.append((side, match))
    return matches


def _proof(basis, source, approval, **details):
    return dict(
        basis=basis,
        source=source,
        **{key: approval[key] for key in APPROVAL_FIELDS if key in approval},
        **details,
    )


def _numeric_proof(aliases, league, team, labels, owners, matches, recorded):
    for source in ("apif", "fdorg"):
        prefix = f"{source}:{league}:team:"
        bindings = [label for label in labels if label.startswith(prefix)]
        if len(bindings) != 1:
            continue
        binding = bindings[0]
        identifier = binding.removeprefix(prefix)
        if not re.fullmatch(r"[1-9][0-9]*", identifier) or owners[_name_key(binding)] != {
            team["id"]
        }:
            continue
        approval = aliases.get(source, {}).get(league, {}).get(identifier)
        if (
            not approval
            or approval.get("status") != "approved"
            or approval.get("approved_by") != "qw5563680"
            or approval.get("canonical") not in labels
            or owners[_name_key(approval["canonical"])] != {team["id"]}
        ):
            continue
        try:
            validate_approvals({league: {identifier: approval}}, require_metadata=True)
            if datetime.fromisoformat(approval["approved_utc"]) > recorded or not all(
                binding_scope_allows(approval, league, kickoff_utc=match["kickoff_utc"])
                for _, match in matches
            ):
                continue
        except (ValueError, TypeError, AttributeError):
            continue
        return _proof("approved_api_binding", source, approval, binding=binding)
    return None


def _jc_rekeyed(connection, previous_id, team_id, labels, matches, approval):
    """T54 can rebind all fixtures without deleting the now-unused old team row."""
    if previous_id == team_id:
        return False
    league = matches[0][1]["league"]
    if connection.execute(
        "SELECT 1 FROM matches WHERE league=? AND (home_team_id=? OR away_team_id=?) LIMIT 1",
        (league, previous_id, previous_id),
    ).fetchone():
        return False
    proofs = approval.get("evidence", [])
    if not isinstance(proofs, list):
        return False
    for side, match in matches:
        opposite = "away" if side == "home" else "home"
        other_team_id = match[opposite + "_team_id"]
        provider_match = connection.execute(
            "SELECT source,kickoff_utc,home_team_id,away_team_id "
            "FROM matches WHERE league=? AND match_id=?",
            (league, match["counterpart_id"]),
        ).fetchone()
        if (
            provider_match is None
            or provider_match["source"] not in {"apif", "fdorg"}
            or provider_match[side + "_team_id"] != team_id
            or provider_match[opposite + "_team_id"] != other_team_id
        ):
            return False
        kickoff = _stamp(match["kickoff_utc"])
        if _stamp(provider_match["kickoff_utc"]) != kickoff:
            return False
        prefix = f"{provider_match['source']}:{league}:team:"
        if not any(
            isinstance(proof, dict)
            and proof.get("jc_match_id") == match["match_id"]
            and proof.get("provider_match_id") == match["counterpart_id"]
            and proof.get("league") == league
            and proof.get("side") == side
            and proof.get("raw_team_id") == previous_id
            and proof.get("canonical_team_id") == team_id
            and proof.get("opponent_team_id") == other_team_id
            and isinstance(proof.get("canonical_binding"), str)
            and proof["canonical_binding"].startswith(prefix)
            and proof.get("canonical_binding") in labels
            and _stamp(proof.get("kickoff_utc")) == kickoff
            for proof in proofs
        ):
            return False
    return True


def _alias_proof(connection, aliases, league, raw_name, original, team, labels, owners, matches):
    legacy = aliases.get("jc", {})
    try:
        canonical = sporttery_aliases.approved_canonical(legacy, league, raw_name)
    except ValueError:
        return None  # A conflicting approved alias cannot clear the candidate.
    if (
        canonical
        and canonical != raw_name
        and canonical in labels
        and owners[_name_key(canonical)] == {team["id"]}
    ):
        for approved_league, names in legacy.items():
            approval = names.get(raw_name, {})
            if (
                approval.get("status") == "approved"
                and approval.get("canonical") == canonical
                and (
                    approved_league == league
                    or (
                        approval.get("scope") == "national_team"
                        and league in sporttery_aliases.NATIONAL_TEAM_COMPETITIONS
                    )
                )
                and (
                    (raw_name in labels and owners[_name_key(raw_name)] == {team["id"]})
                    or _jc_rekeyed(connection, original, team["id"], labels, matches, approval)
                )
            ):
                return _proof("approved_alias_merged", "jc", approval, alias=raw_name)
    for label, approval in aliases.get("fd", {}).get(league, {}).items():
        canonical = approval.get("canonical")
        if (
            approval.get("status") == "approved"
            and label != canonical
            and raw_name in {label, canonical}
            and {label, canonical} <= labels
            and owners[_name_key(label)] == owners[_name_key(canonical)] == {team["id"]}
        ):
            return _proof("approved_alias_merged", "fd", approval, alias=label)
    return None


def resolve_pending(database, run_id, *, recorded=None, aliases=None):
    """Write queue resolutions under the same lock as ingestion and manual review."""
    database = Path(database)
    queue_path = database.parent / QUEUE
    if queue_path.is_symlink():
        raise ValueError("候选队列不能为符号链接")
    if not queue_path.is_file():
        return 0
    recorded = datetime.now(UTC) if recorded is None else recorded
    if recorded.utcoffset() is None:
        raise ValueError("自动出队时间须包含明确时区")
    if aliases is None:
        aliases = {
            source: read_json(CONFIG_ROOT / "team_aliases" / path)
            for source, path in ALIAS_FILES.items()
        }
    changed = 0
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=rw", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        queue = json.loads(queue_path.read_bytes())
        if not any(
            row.get("status") == "pending" for names in queue.values() for row in names.values()
        ):
            return 0
        teams, labels, owners = {}, {}, defaultdict(set)
        for team in db.execute("SELECT id,name,aliases FROM teams"):
            team_id = team["id"]
            teams[team_id] = team
            labels[team_id] = {team["name"], *json.loads(team["aliases"] or "[]")}
            for label in labels[team_id]:
                owners[_name_key(label)].add(team_id)
        for league, names in queue.items():
            for raw_name, entry in names.items():
                if entry.get("source") != "jc" or entry.get("status") != "pending":
                    continue
                matches = _observed_matches(db, league, entry, recorded)
                current_owners = {match[side + "_team_id"] for side, match in matches}
                if len(current_owners) != 1:
                    continue
                team_id = next(iter(current_owners))
                team = teams[team_id]
                original = entry.get("team_id")
                if type(original) is not int or original <= 0:
                    continue
                proof = None
                if original == team_id and raw_name in labels[team_id]:
                    proof = _numeric_proof(
                        aliases, league, team, labels[team_id], owners, matches, recorded
                    )
                if proof is None:
                    proof = _alias_proof(
                        db,
                        aliases,
                        league,
                        raw_name,
                        original,
                        team,
                        labels[team_id],
                        owners,
                        matches,
                    )
                if proof:
                    entry["status"] = "resolved"
                    entry["resolution"] = dict(
                        proof,
                        run_id=run_id,
                        recorded_utc=recorded.astimezone(UTC).isoformat(),
                        canonical=team["name"],
                        canonical_team_id=team_id,
                    )
                    changed += 1
        if changed:
            _atomic_write(
                queue_path,
                (json.dumps(queue, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
            )
        db.rollback()  # Only the queue changed.
    return changed
