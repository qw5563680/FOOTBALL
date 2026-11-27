"""Store one complete SFC issue and its conservative cross-source mapping."""

import copy
import csv
import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path

import requests
from lxml.etree import ParserError
from sqlalchemy import delete, or_, select

from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, SfcIssue, SfcIssueMatch, Team
from zucai.nations import LEAGUE as NATIONS_LEAGUE
from zucai.nations import UnknownSfcTeam, sfc_team
from zucai.sfc_club_names import club_name
from zucai.sfc_okooo_names import PROVIDER as OKOOO_PROVIDER
from zucai.sfc_okooo_names import approved_name, is_okooo, name_evidence
from zucai.sfc_sequence import known_official, period_windows, sequence, verification_at
from zucai.sfc_verification import compare_issue, pending_amendments, resolve_notices
from zucai.sources.c500 import C500FixtureBridge, C500Sfc, SfcFixture, SfcSnapshot
from zucai.sources.okooo_sfc import OkoooSfc
from zucai.sources.sfc_official import UnreadableAmendments, fetch_official_notices
from zucai.sources.sporttery import BEIJING
from zucai.sources.teams import normalize_team, upsert_team

LOG = logging.getLogger(__name__)
_SOURCE_ERRORS = (OSError, ValueError, ParserError, requests.RequestException)


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _league_key(name):
    # Verified on 2026-11-01: c500 fixture 1362837 links to jc:2042440.
    # Keep source labels in snapshots; never infer arbitrary competition aliases.
    return {"瑞典超": "瑞超"}.get(name, name)


def _find_team(session, name):
    key = normalize_team(name).casefold()
    candidates = [
        t
        for t in session.scalars(select(Team))
        if t.name.casefold() == key or any(normalize_team(a).casefold() == key for a in t.aliases)
    ]
    if len(candidates) > 1:
        raise ValueError(f"球队别名存在歧义：{name}")
    return candidates[0] if candidates else None


def _national_team_names(session, fixture, match=None, *, original_names=False):
    """Check known metadata and sides before downgrading missing national identities."""
    own_id = "c500:" + fixture.fixture_id
    conflict = (
        f"已有比赛{own_id}身份冲突，须人工核对"
        if original_names
        else f"{own_id} 欧国联的球队、主客或原始开球时间不一致"
    )
    if match and (
        match.league != fixture.league
        or datetime.fromisoformat(match.kickoff_utc) != fixture.kickoff_utc
    ):
        raise ValueError(conflict)
    names, missing = [], []
    for side, provider_id, label in (
        ("home", fixture.home_id, fixture.home),
        ("away", fixture.away_id, fixture.away),
    ):
        try:
            name = sfc_team(provider_id, label)
        except UnknownSfcTeam as exc:
            missing.append(exc)
            continue
        names.append(name)
        if match:
            team = _find_team(session, name)
            if original_names:
                # Existing manual records may still use the exact printed short name.
                team = _find_team(session, label) or team
            if team is None or team.id != getattr(match, side + "_team_id"):
                raise ValueError(conflict)
    if missing:
        error = missing[0]
        error.missing_teams = [team for exc in missing for team in exc.missing_teams]
        raise error
    return names


def _preferred_candidate(candidates):
    """Collapse proven same-fixture rows; never break a source-priority tie."""
    if not candidates:
        return None
    priorities = {"jc": 0, "fd": 1, "fdorg": 2}
    ordered = sorted(candidates, key=lambda m: priorities.get(m.source, 3))
    if len(ordered) == 1:
        return ordered[0]
    if priorities.get(ordered[0].source, 3) == priorities.get(ordered[1].source, 3):
        return None

    def equivalent(a, b):
        return (
            a.counterpart_id == b.match_id
            or b.counterpart_id == a.match_id
            or _same_stored_fixture(a, b)
        )

    group, pending = [ordered[0]], ordered[1:]
    while pending:
        joined = [m for m in pending if any(equivalent(m, other) for other in group)]
        if not joined:
            return None
        group.extend(joined)
        pending = [m for m in pending if m not in joined]
    return ordered[0]


def _same_stored_fixture(a, b):
    return (a.league, a.home_team_id, a.away_team_id) == (
        b.league,
        b.home_team_id,
        b.away_team_id,
    ) and datetime.fromisoformat(_iso(a.kickoff_utc)) == datetime.fromisoformat(_iso(b.kickoff_utc))


def _plays_in(session, team, league):
    """The stored identity already has a fixture of this league from a regular source."""
    return (
        session.scalar(
            select(Match.match_id)
            .where(
                Match.league == league,
                Match.source != "c500",
                or_(Match.home_team_id == team.id, Match.away_team_id == team.id),
            )
            .limit(1)
        )
        is not None
    )


def _map_fixture(session, fixture, bridges, *, mapped_only=False, own_fixtures=False):
    own_id = "c500:" + fixture.fixture_id
    own = session.get(Match, own_id)
    bridge = bridges.get(fixture.fixture_id)
    missing_national_target = False
    evidence = {"basis": "exact_team_names"}
    linked = list(session.scalars(select(Match).where(Match.counterpart_id == own_id)))
    if own and own.counterpart_id:
        target = session.get(Match, own.counterpart_id)
        if target is not None:
            linked.append(target)
    if linked:
        evidence = {"basis": "verified_database_counterpart", "provider_match_id": own_id}
    if bridge:
        if (bridge["home_id"], bridge["away_id"]) != (fixture.home_id, fixture.away_id) or (
            not mapped_only and datetime.fromisoformat(bridge["kickoff_utc"]) != fixture.kickoff_utc
        ):
            raise ValueError(f"{own_id} ID对照主客球队或开球冲突")
        target = session.get(Match, bridge["jc_id"])
        missing_national_target = target is None and fixture.league == NATIONS_LEAGUE
        if missing_national_target and (
            _league_key(bridge["league"]) != _league_key(fixture.league)
            or datetime.fromisoformat(bridge["kickoff_utc"]) != fixture.kickoff_utc
        ):
            raise ValueError(f"{own_id} 失效欧国联ID对照与原始联赛或开球冲突")
        if own and own.counterpart_id and own.counterpart_id != bridge["jc_id"]:
            previous = session.get(Match, own.counterpart_id)
            if (
                previous is None
                or target is None
                or _preferred_candidate([previous, target]) is None
            ):
                raise ValueError(f"{own_id} 已存与新增比赛ID对照冲突")
        if target is not None:
            linked.append(target)
        evidence = {"basis": "provider_fixture_and_team_ids", **bridge}
    linked = list({m.match_id: m for m in linked}.values())
    if linked:
        match = _preferred_candidate(linked)
        if match is None:
            raise ValueError(f"{own_id} ID对照指向多个比赛")
        # Cloud ID bridging checks both source pages' league labels when the
        # official league label differs; it never creates a reusable name alias.
        source_league_confirmed = (
            mapped_only
            and bridge
            and match.match_id == bridge["jc_id"]
            and _league_key(bridge["league"]) == _league_key(fixture.league)
        )
        if fixture.league == NATIONS_LEAGUE:
            _national_team_names(session, fixture, match)
        if (
            _league_key(match.league) != _league_key(fixture.league) and not source_league_confirmed
        ) or (not mapped_only and datetime.fromisoformat(match.kickoff_utc) != fixture.kickoff_utc):
            raise ValueError(f"{own_id} ID对照与比赛联赛或开球冲突")
        return match, "matched", [m.match_id for m in linked], evidence
    home_name = bridge["home"] if bridge else fixture.home
    away_name = bridge["away"] if bridge else fixture.away
    national_fallback = fixture.league == NATIONS_LEAGUE and (not bridge or missing_national_target)
    if national_fallback:
        # A provider's JC number is not a database binding until its target exists.
        # Retry the established national path using the original SFC identities;
        # an existing target has already passed (or failed) the strict checks above.
        home_name, away_name = _national_team_names(session, fixture, own, original_names=True)
        evidence = {
            "basis": "national_team_ids_and_exact_kickoff",
            "home_id": fixture.home_id,
            "away_id": fixture.away_id,
        }
        if bridge:
            evidence.update(
                bridge=copy.deepcopy(bridge), bridge_fallback_reason="target_match_missing"
            )
    home, away = _find_team(session, home_name), _find_team(session, away_name)
    if not bridge and fixture.league != NATIONS_LEAGUE and (home is None or away is None):
        # No ID bridge (JC does not sell the fixture): an owner-approved club short name,
        # keyed by the source's team ID and its printed label, may stand in for the name.
        approved = {}
        for side, team, provider_id, label in (
            ("home", home, fixture.home_id, fixture.home),
            ("away", away, fixture.away_id, fixture.away),
        ):
            canonical = None if team is not None else club_name(fixture.league, provider_id, label)
            if canonical is not None:
                approved[side] = dict(
                    provider_id=str(provider_id), label=label, canonical=canonical
                )
        if approved:
            if "home" in approved:
                home = _find_team(session, approved["home"]["canonical"])
            if "away" in approved:
                away = _find_team(session, approved["away"]["canonical"])
            if home is not None and away is not None:
                evidence = {"basis": "approved_club_short_names", "names": approved}
    # An earlier SFC-only record may retain the source's abbreviated names.
    # Check that original identity before considering the bridge's longer names;
    # adding an ID link must not rename/merge teams or invent a JC match.
    own_home = (_find_team(session, fixture.home) or home) if own else home
    own_away = (_find_team(session, fixture.away) or away) if own else away
    if (
        own
        and own_fixtures
        and own.source == "c500"
        and own.status == "scheduled"
        and own.league == fixture.league
        and own_home is not None
        and own_away is not None
        and (own.home_team_id, own.away_team_id) == (own_home.id, own_away.id)
        and datetime.fromisoformat(own.kickoff_utc) != fixture.kickoff_utc
    ):
        # The list moved the kick-off of its own fixture: follow the source at collection.
        own.kickoff_utc = fixture.kickoff_utc
        session.flush()
        session.refresh(own)
    if own and (
        own.league != fixture.league
        or datetime.fromisoformat(own.kickoff_utc) != fixture.kickoff_utc
        or own_home is None
        or own_away is None
        or own.home_team_id != own_home.id
        or own.away_team_id != own_away.id
    ):
        raise ValueError(f"已有比赛{own_id}身份冲突，须人工核对")
    if own and bridge and not mapped_only:
        own.counterpart_id = bridge["jc_id"]
    candidates = []
    if home and away and (not bridge or national_fallback):
        candidates = list(
            session.scalars(
                select(Match).where(
                    Match.home_team_id == home.id,
                    Match.away_team_id == away.id,
                    Match.league == fixture.league,
                    Match.kickoff_utc == fixture.kickoff_utc,
                    Match.match_id != own_id,
                )
            )
        )
    preferred = _preferred_candidate(candidates)
    if preferred is not None:
        return preferred, "matched", [m.match_id for m in candidates], evidence
    if mapped_only:
        # T78: an own fixture still needs two stored, distinct teams of this league,
        # no regular fixture and no JC bridge. Record why a team's evidence fell short.
        allowed = False
        if not bridge and not candidates and fixture.league != NATIONS_LEAGUE:
            gaps = {}
            for side, team in (("home", home), ("away", away)):
                if team is None:
                    gaps[side] = "team_missing"
                elif not _plays_in(session, team, fixture.league):
                    gaps[side] = "league_history_missing"
            if gaps:
                evidence["own_fixture_team_gaps"] = gaps
            else:
                allowed = home.id != away.id
        if own is None and own_fixtures and allowed:
            own = Match(
                match_id=own_id,
                source="c500",
                status="scheduled",
                league=fixture.league,
                home_team_id=home.id,
                away_team_id=away.id,
                kickoff_utc=fixture.kickoff_utc,
            )
            session.add(own)
            session.flush()
            session.refresh(own)
        if own is not None and own.source == "c500" and allowed:
            names = (
                evidence.get("names") if evidence["basis"] == "approved_club_short_names" else None
            )
            evidence = {
                "basis": "sfc_own_fixture",
                "home_id": fixture.home_id,
                "away_id": fixture.away_id,
            }
            if names:
                evidence["names"] = names
            return own, "matched", [], evidence
        return (
            None,
            "ambiguous" if candidates else "unmatched",
            [m.match_id for m in candidates],
            evidence,
        )
    if own is None:
        home = home or upsert_team(session, home_name, "c500-sfc")
        away = away or upsert_team(session, away_name, "c500-sfc")
        own = Match(
            match_id=own_id,
            source="c500",
            status="scheduled",
            league=fixture.league,
            home_team_id=home.id,
            away_team_id=away.id,
            kickoff_utc=fixture.kickoff_utc,
            counterpart_id=bridge["jc_id"] if bridge else None,
            jc_num=bridge.get("jc_num") or None if bridge else None,
        )
        session.add(own)
        session.flush()
    return (
        own,
        "ambiguous" if candidates else "unmatched",
        [m.match_id for m in candidates],
        evidence,
    )


def _conflict_evidence(session, fixture, bridge, error):
    """Keep both sides of failed ID evidence, without inferring a new identity."""
    own_id = "c500:" + fixture.fixture_id
    own = session.get(Match, own_id)
    ids = {bridge["jc_id"]} if bridge else set()
    if own and own.counterpart_id:
        ids.add(own.counterpart_id)
    ids.update(session.scalars(select(Match.match_id).where(Match.counterpart_id == own_id)))
    candidates = []
    for mid in sorted(ids):
        match = session.get(Match, mid)
        if match:
            candidates.append(
                dict(
                    match_id=mid,
                    home_team_id=match.home_team_id,
                    away_team_id=match.away_team_id,
                    home=session.get(Team, match.home_team_id).name,
                    away=session.get(Team, match.away_team_id).name,
                    league=match.league,
                    kickoff_utc=_iso(match.kickoff_utc),
                )
            )
    return dict(
        basis="conflicting_identity",
        detail=str(error),
        bridge=bridge,
        source_identity=dict(
            home_id=fixture.home_id,
            away_id=fixture.away_id,
            home=fixture.home,
            away=fixture.away,
            league=fixture.league,
            kickoff_utc=fixture.kickoff_utc.isoformat(),
        ),
        candidate_identities=candidates,
    )


def _placeholder(issue, fixture):
    """A midnight kick-off on a day of its own: the source has not printed the time yet."""
    local = fixture.kickoff_utc.astimezone(BEIJING)
    return local.time().isoformat() == "00:00:00" and any(
        f.kickoff_utc.astimezone(BEIJING).date() != local.date()
        for f in issue.fixtures
        if f.seq != fixture.seq
    )


def _map_okooo_fixture(session, fixture, observed):
    """Use exact or approved names without consulting 500 IDs or general aliases."""
    evidence = dict(
        basis="okooo_exact_names_and_kickoff",
        provider=OKOOO_PROVIDER,
        provider_fixture_id=fixture.fixture_id,
    )
    teams, approved = {}, {}
    for side, label in (("home", fixture.home), ("away", fixture.away)):
        entry = approved_name(fixture.league, label, observed)
        canonical = entry["canonical"] if entry else label
        team = session.scalar(select(Team).where(Team.name == canonical))
        if team is not None and team.name == canonical:
            teams[side] = team.id
        if entry:
            approved[side] = name_evidence(entry, label)
    if approved:
        evidence["names"] = approved
    if len(teams) != 2:
        evidence["missing_names"] = [side for side in ("home", "away") if side not in teams]
        return None, "unmatched", [], evidence
    candidates = list(
        session.scalars(
            select(Match)
            .where(
                Match.league == fixture.league,
                Match.home_team_id == teams["home"],
                Match.away_team_id == teams["away"],
                Match.kickoff_utc == fixture.kickoff_utc,
            )
            .order_by(Match.match_id)
        )
    )
    match = _preferred_candidate(candidates)
    mapping = "matched" if match is not None else "ambiguous" if candidates else "unmatched"
    return match, mapping, [row.match_id for row in candidates], evidence


def _snapshot_rows(
    session,
    issue,
    bridge_map,
    *,
    mapped_only,
    bridge_conflicts=(),
    own_fixtures=False,
    okooo_observed=None,
):
    snapshot = []
    for fixture in issue.fixtures:
        placeholder = okooo_observed is None and _placeholder(issue, fixture)
        try:
            if fixture.fixture_id in bridge_conflicts:
                raise ValueError(f"{fixture.fixture_id} 新旧比赛ID对照冲突，须人工核对")
            if okooo_observed is not None:
                match, mapping, candidates, evidence = _map_okooo_fixture(
                    session, fixture, okooo_observed
                )
            else:
                match, mapping, candidates, evidence = _map_fixture(
                    session,
                    fixture,
                    bridge_map,
                    mapped_only=mapped_only,
                    # Never from a placeholder time: the fixture would carry a made-up kick-off.
                    own_fixtures=own_fixtures and not placeholder,
                )
        except UnknownSfcTeam as exc:
            # An unreviewed identity leaves one row unresolved, including manual imports.
            match, mapping, candidates = None, "unmatched", []
            evidence = dict(
                basis="national_team_name_missing",
                home_id=fixture.home_id,
                away_id=fixture.away_id,
                missing_teams=exc.missing_teams,
            )
            if fixture.fixture_id in bridge_map:
                evidence["bridge"] = copy.deepcopy(bridge_map[fixture.fixture_id])
        except ValueError as exc:
            if not mapped_only or okooo_observed is not None:
                raise
            match, mapping = None, "ambiguous"
            evidence = _conflict_evidence(session, fixture, bridge_map.get(fixture.fixture_id), exc)
            if fixture.fixture_id in bridge_conflicts:
                evidence["previous_bridge"] = bridge_conflicts[fixture.fixture_id]
            candidates = [c["match_id"] for c in evidence["candidate_identities"]]
        source_time = fixture.kickoff_utc.isoformat()
        if mapped_only and match is not None and match.source == "c500" and placeholder:
            # An own fixture stored earlier does not stand while its time is a placeholder.
            # (The manual local import keeps its earlier behaviour: it archives every row.)
            match, mapping = None, "unmatched"
        if fixture.league == NATIONS_LEAGUE and placeholder:
            match, mapping = None, "unmatched"
        kickoff = _iso(match.kickoff_utc) if match else source_time
        snapshot.append(
            dict(
                seq=fixture.seq,
                fixture_id=fixture.fixture_id,
                home=fixture.home,
                away=fixture.away,
                league=fixture.league,
                kickoff_utc=kickoff,
                source_kickoff_utc=source_time,
                kickoff_basis="database" if match else "placeholder" if placeholder else "provider",
                source_ended=fixture.ended,
                match_id=match.match_id if match else None,
                mapping=mapping,
                reason="conflicting_identity"
                if evidence["basis"] == "conflicting_identity"
                else mapping
                if mapping != "matched"
                else None,
                candidate_ids=candidates,
                mapping_evidence=evidence,
                provider_home_id=fixture.home_id,
                provider_away_id=fixture.away_id,
                matched_identity=dict(
                    home=session.get(Team, match.home_team_id).name,
                    away=session.get(Team, match.away_team_id).name,
                    league=match.league,
                    source=match.source,
                    kickoff_utc=kickoff,
                )
                if match
                else None,
            )
        )
    return snapshot


def _same_national_counterpart(session, archived_id, candidate_id):
    """Permit only an explicit, identity-consistent APIF/JC transition."""
    archived, candidate = (session.get(Match, key) for key in (archived_id, candidate_id))
    return bool(
        archived
        and candidate
        and {archived.source, candidate.source} == {"apif", "jc"}
        and (
            archived.counterpart_id == candidate.match_id
            or candidate.counterpart_id == archived.match_id
        )
        and _same_stored_fixture(archived, candidate)
    )


def _replaces_with_jc(session, row, match, candidate):
    """A refreshed candidate may replace a checked, exact non-JC counterpart."""
    original = row.get("matched_identity") or {}
    confirmed = candidate.get("matched_identity") or {}
    if (
        not match
        or match.source == "jc"
        or row.get("mapping") != "matched"
        or row.get("kickoff_basis") == "placeholder"
        or confirmed.get("source") != "jc"
        or original.get("source") != match.source
        or any(original.get(key) != confirmed.get(key) for key in ("league", "home", "away"))
    ):
        return False
    jc = session.get(Match, candidate["match_id"])
    return jc is not None and _same_stored_fixture(match, jc)


def refresh_snapshot(session, issue):
    """Retry unresolved rows against the current DB, without writing any source state."""
    rows = copy.deepcopy(issue.snapshot)
    source = SfcSnapshot(
        issue.issue_no,
        None,
        [
            SfcFixture(
                seq=r["seq"],
                fixture_id=r["fixture_id"],
                home=r["home"],
                away=r["away"],
                league=r["league"],
                kickoff_utc=datetime.fromisoformat(r.get("source_kickoff_utc", r["kickoff_utc"])),
                ended=r.get("source_ended", False),
                home_id=r.get("provider_home_id"),
                away_id=r.get("provider_away_id"),
            )
            for r in rows
        ],
        issue.date_basis,
    )
    if is_okooo(getattr(issue, "source_url", None)):
        return _snapshot_rows(
            session,
            source,
            {},
            mapped_only=True,
            okooo_observed=datetime.fromisoformat(issue.observed_utc),
        )
    bridges = {}
    conflicts = {}
    for row in rows:
        saved = row.get("mapping_evidence", {})
        bridge = (
            saved if saved.get("basis") == "provider_fixture_and_team_ids" else saved.get("bridge")
        )
        if bridge:
            bridges[row["fixture_id"]] = bridge
        if saved.get("previous_bridge"):
            conflicts[row["fixture_id"]] = saved["previous_bridge"]
    refreshed = _snapshot_rows(
        session, source, bridges, mapped_only=True, bridge_conflicts=conflicts
    )
    occupied = {row["match_id"] for row in rows if row["match_id"] is not None}
    for index, row in enumerate(rows):
        candidate = refreshed[index]
        if row["match_id"] is None or row["league"] == NATIONS_LEAGUE:
            # Only refresh this disclosure for snapshots that already recorded it.
            if "own_fixture_team_gaps" not in (row.get("mapping_evidence") or {}):
                candidate["mapping_evidence"].pop("own_fixture_team_gaps", None)
            if row["league"] == NATIONS_LEAGUE and (
                row.get("kickoff_basis") == "placeholder"
                or (
                    row["match_id"]
                    and candidate["match_id"]
                    and row["match_id"] != candidate["match_id"]
                    and not _same_national_counterpart(
                        session, row["match_id"], candidate["match_id"]
                    )
                )
            ):
                candidate.update(
                    match_id=None,
                    matched_identity=None,
                    mapping="ambiguous",
                    reason="期次原场次待复核",
                    kickoff_basis=row.get("kickoff_basis"),
                )
            rows[index] = candidate
            if candidate["match_id"] is not None:
                occupied.add(candidate["match_id"])
            continue
        match = session.get(Match, row["match_id"])
        if candidate["match_id"] not in occupied and _replaces_with_jc(
            session, row, match, candidate
        ):
            candidate["mapping_evidence"]["jc_preferred_over"] = row["match_id"]
            rows[index] = candidate
            occupied.add(candidate["match_id"])
            continue
        # Other bound rows retain identity evidence, refreshing only the database clock.
        if match:
            row.setdefault("source_kickoff_utc", row["kickoff_utc"])
            row.update(kickoff_utc=_iso(match.kickoff_utc), kickoff_basis="database")
            row["matched_identity"]["kickoff_utc"] = _iso(match.kickoff_utc)
    return rows


def ingest_issue(
    session,
    issue: SfcSnapshot,
    *,
    observed: datetime,
    url: str,
    raw_sha256: str,
    bridges=(),
    mapped_only=False,
    own_fixtures=False,
):
    if observed.utcoffset() is None:
        raise ValueError("采集时间必须有时区")
    record = session.get(SfcIssue, issue.issue_no)
    if record and record.observed_utc:
        previous = datetime.fromisoformat(record.observed_utc)
        if observed < previous or (observed == previous and raw_sha256 != record.raw_sha256):
            raise ValueError("旧期次快照不能覆盖新版本")
    okooo = is_okooo(url)
    if okooo:
        bridges = ()
    bridge_map = {b["fixture_id"]: b for b in bridges}
    if len(bridge_map) != len(bridges):
        raise ValueError("重复比赛ID对照")
    # The public current page rolls past old fixtures. Preserve their archived
    # ID evidence, including its original capture time, without inventing aliases.
    bridge_conflicts = {}
    for row in (record.snapshot or []) if record and not okooo else []:
        saved = row.get("mapping_evidence", {})
        successful = saved.get("basis") == "provider_fixture_and_team_ids"
        if saved.get("previous_bridge"):
            saved, successful = saved["previous_bridge"], True
        elif (
            saved.get("basis") == "national_team_ids_and_exact_kickoff"
            and saved.get("bridge_fallback_reason") == "target_match_missing"
            and saved.get("bridge")
        ):
            # Missing target rows do not invalidate the archived provider ID
            # binding, including when the strict fallback is still unresolved.
            saved, successful = saved["bridge"], True
        if not successful:
            saved = saved.get("bridge")
            if not saved:
                continue
        fid = row["fixture_id"]
        current = bridge_map.get(fid)
        if (
            successful
            and current
            and any(current[k] != saved[k] for k in ("jc_id", "home_id", "away_id"))
        ):
            bridge_conflicts[fid] = saved
        if current is None:
            bridge_map[fid] = dict(saved, reused_from_issue_snapshot=True)
    snapshot = _snapshot_rows(
        session,
        issue,
        bridge_map,
        mapped_only=mapped_only,
        bridge_conflicts=bridge_conflicts,
        own_fixtures=own_fixtures,
        okooo_observed=observed if okooo else None,
    )
    if (
        len(snapshot) != 14
        or len({x["fixture_id"] for x in snapshot}) != 14
        or len([x["match_id"] for x in snapshot if x["match_id"]])
        != len({x["match_id"] for x in snapshot if x["match_id"]})
    ):
        raise ValueError("期次映射必须有14场不同的比赛")
    if record is None:
        record = SfcIssue(issue_no=issue.issue_no)
        session.add(record)
    record.sale_end_utc = issue.sale_end_utc.isoformat() if issue.sale_end_utc else None
    record.observed_utc = observed.isoformat()
    if record.raw_sha256 != raw_sha256 or record.snapshot != snapshot:
        record.official_verification = None
    record.source_url, record.raw_sha256 = url, raw_sha256
    record.date_basis, record.snapshot = issue.date_basis, snapshot
    session.flush()
    session.execute(delete(SfcIssueMatch).where(SfcIssueMatch.issue_no == issue.issue_no))
    session.add_all(
        [
            SfcIssueMatch(issue_no=issue.issue_no, seq=x["seq"], match_id=x["match_id"])
            for x in snapshot
            if x["match_id"] is not None
        ]
    )
    session.flush()
    return record


def fetch_sfc(issue=None, *, db=None, matches_out=None):
    output = Path(matches_out) if matches_out else None
    if output is not None and output.exists():
        raise ValueError("比赛清单已存在，请使用新文件")
    source = C500Sfc()
    path = source.fetch_raw(issue=issue)
    raw, meta = source.store.load(path), source.store.load_metadata(path)
    snapshot = source.parse(raw)[0]
    if issue is not None and snapshot.issue_no != issue:
        raise ValueError("返回期号与请求期号不符，未导入")
    bridge_source = C500FixtureBridge()
    bridge_path = bridge_source.fetch_raw()
    bridge_raw = bridge_source.store.load(bridge_path)
    bridge_meta = bridge_source.store.load_metadata(bridge_path)
    bridges = bridge_source.parse(bridge_raw)
    for bridge in bridges:
        bridge["provenance"] = dict(
            url=bridge_meta["url"],
            observed_utc=bridge_meta["fetched_at_utc"],
            raw_sha256=hashlib.sha256(bridge_raw).hexdigest(),
        )
    engine = get_engine(db)
    made = False
    try:
        init_db(engine)
        with session_scope(engine) as session:
            record = ingest_issue(
                session,
                snapshot,
                observed=datetime.fromisoformat(meta["fetched_at_utc"]),
                url=meta["url"],
                raw_sha256=hashlib.sha256(raw).hexdigest(),
                bridges=bridges,
            )
            if output is not None:
                output.parent.mkdir(parents=True, exist_ok=True)
                with output.open("x", encoding="utf-8-sig", newline="") as stream:
                    made = True
                    writer = csv.DictWriter(
                        stream,
                        fieldnames=["match_id", "home", "away", "league", "source", "kickoff_utc"],
                    )
                    writer.writeheader()
                    writer.writerows(
                        dict(match_id=r["match_id"], **r["matched_identity"])
                        for r in record.snapshot
                        if r["match_id"] is not None
                    )
        if output is not None:
            exported = sum(r["match_id"] is not None for r in record.snapshot)
            LOG.info(
                "已导出%d场比赛清单；未解析身份%d场", exported, len(record.snapshot) - exported
            )
        LOG.info(
            "已保存14场期次%s；严格匹配%d/14；原件%s",
            record.issue_no,
            sum(x["mapping"] == "matched" for x in record.snapshot),
            path,
        )
        return record
    except Exception:
        if made:
            output.unlink(missing_ok=True)
        raise
    finally:
        engine.dispose()


def _sequence_time(meta):
    """The moment the period sequence is judged at: when the root page was captured."""
    return datetime.fromisoformat(meta["fetched_at_utc"])


def _resolve_schedule(result, notices, previous_schedule, known, checked_at, failures):
    prior_checks = {
        row["issue_no"]: verification_at(row["official_verification"], checked_at)
        for row in known
        if not row["observed_utc"] or datetime.fromisoformat(row["observed_utc"]) <= checked_at
    }
    pending = pending_amendments(failures, notices, checked_at)
    result["pending_amendments"] = pending
    archived = [(number, known_official(check)) for number, check in prior_checks.items()]
    archived.extend((previous_schedule or {}).items())
    if result.get("official_error") and not result["official_unavailable"]:
        # A separate source error still blocks fresh verification. Preserve the
        # last readable receipt alongside an identified outstanding adjustment.
        archived = [(n, receipt) for n, receipt in archived if _pending_for(n, pending)]
    # A body without its table cannot resolve its own earlier failed read.
    unreadable_urls = {p["source_url"] for p in pending}
    selected = resolve_notices(
        [n for n in notices if n.get("source_url") not in unreadable_urls], archived, checked_at
    )
    for number, notice in selected.items():
        applicable = _pending_for(number, pending)
        if applicable:
            notice["pending_amendments"] = applicable
    result.update(
        official_checked_at=checked_at.isoformat(),
        official_reused_schedule=[
            n
            for n, notice in selected.items()
            if notice.get("from_archive") or notice.get("pending_amendments")
        ],
        official_schedule={
            number: {
                key: notice.get(key)
                for key in (
                    "source_url",
                    "title",
                    "kind",
                    "notice_date",
                    "published_at",
                    "raw_sha256",
                    "sale_start_utc",
                    "sale_end_utc",
                    "reason",
                    "versions",
                    "pending_amendments",
                    "observed_utc",
                )
                if notice.get(key) is not None
            }
            for number, notice in selected.items()
        },
    )
    for summary in result["official_schedule"].values():
        summary.setdefault("observed_utc", checked_at.isoformat())
    return selected, prior_checks


def _pending_for(number, pending):
    return [entry for entry in pending if entry.get("issue_hint") in (None, number)]


def _check_archived_issues(session, selected, checked_at, numbers, result, prior_checks):
    """An amendment may affect an already archived period other than the root or due page."""
    for record in session.scalars(select(SfcIssue)):
        previous = prior_checks.get(record.issue_no) or {}
        previous_end = known_official(previous).get("sale_end_utc") or record.sale_end_utc
        if not record.snapshot:
            continue
        if (
            record.issue_no not in numbers
            and not previous.get("pending_amendments")
            and (not previous_end or datetime.fromisoformat(previous_end) <= checked_at)
        ):
            continue
        notice = selected.get(record.issue_no)
        if notice is None:
            notice = {
                "pending_amendments": _pending_for(record.issue_no, result["pending_amendments"])
            }
        check = compare_issue(
            record.issue_no,
            record.sale_end_utc,
            record.snapshot,
            notice,
            checked_at,
            source_error=result.get("official_error"),
            provider=OKOOO_PROVIDER if is_okooo(record.source_url) else "500.com",
        )
        if (
            previous
            and check.get("last_known_official")
            and all(
                known_official(previous).get(key) == notice.get(key)
                for key in ("source_url", "raw_sha256")
            )
        ):
            check["last_official_check_at"] = (
                previous.get("last_official_check_at") or previous["checked_at"]
            )
        check["provider_raw_sha256"] = record.raw_sha256
        record.official_verification = check


def _capture_500(source, *, issue=None):
    path = (
        source.fetch_raw(issue=issue, single_attempt=True)
        if issue
        else source.fetch_raw(current_only=True)
    )
    raw = source.store.load(path)
    (snapshot,) = source.parse(raw)
    if issue and snapshot.issue_no != issue:
        raise ValueError(f"来源返回期号 {snapshot.issue_no}")
    return snapshot, raw, source.store.load_metadata(path)


def _capture_okooo(number, audit, provider_windows, official_schedule):
    audit.update(status="unavailable", issue_no=number)
    if not number:
        audit["reason"] = "按原期次顺序无法确认当前应展示期号"
        return None
    source = OkoooSfc()
    try:
        path = source.fetch_raw(issue=number)
        raw, meta = source.store.load(path), source.store.load_metadata(path)
        (issue,) = source.parse(raw)
        observed = _sequence_time(meta)
        state = sequence(
            period_windows(provider_windows, official_schedule, observed).values(), observed
        )
        if issue.issue_no != number or state["current"] != number:
            raise ValueError("澳客返回的期号在实际取数时不是当前应展示期")
        if not issue.sale_end_utc or issue.sale_end_utc <= observed:
            raise ValueError("澳客名单已到停售时间")
        meta["response_sha256"] = source.response_sha256
        return issue, raw, meta
    except _SOURCE_ERRORS as exc:
        audit["reason"] = str(exc)[:200] or type(exc).__name__
        return None
    finally:
        audit.update(
            requests=source.requests,
            robots_requests=source.robots_requests,
            page_requests=source.page_requests,
        )
        if source.robots_sha256:
            audit["robots_sha256"] = source.robots_sha256
        source.session.close()


def _collect_500_bridge(source, *, jc_root=None):
    # The 500-only identity bridge shares the session and at-least-one-second limiter.
    bridge_source = C500FixtureBridge(store=source.store, session=source.session)
    bridge_source.limiter = source.limiter
    if jc_root is None:
        path = bridge_source.fetch_raw()
        raw, meta = source.store.load(path), source.store.load_metadata(path)
    else:
        raw, meta = jc_root.snapshot(bridge_source)
    bridges = bridge_source.parse(raw)
    raw_sha256 = meta.get("raw_sha256") or hashlib.sha256(raw).hexdigest()
    for bridge in bridges:
        bridge["provenance"] = dict(
            url=meta["url"],
            observed_utc=meta["fetched_at_utc"],
            raw_sha256=raw_sha256,
        )
    return bridges, _sequence_time(meta)


def collect_current_sfc(db, *, previous_schedule=None, previous_pending=(), jc_root=None):
    """Read current SFC and JC root pages without changing match identities."""
    import json

    result = dict(
        status="partial",
        inserted=0,
        linked_existing=0,
        rejected_rows=0,
        requests=0,
        errors=[],
        issue_no=None,
        mapped=0,
        unmatched=0,
        okooo=dict(
            status="not_needed",
            issue_no=None,
            reason=None,
            requests=0,
            robots_requests=0,
            page_requests=0,
            request_limit=OkoooSfc.request_limit,
        ),
    )
    source = engine = None
    backup = None
    audit = Path(db).parent / "state/audit/sfc_unmatched.csv"
    previous_audit = None
    published = False
    temporary = audit.with_suffix(".tmp")
    try:
        source = C500Sfc()
        if jc_root is not None:
            source.limiter = jc_root.limiter
        engine = get_engine(db)
        init_db(engine)
        source_error = None
        unavailable = False
        with session_scope(engine) as session:
            known = [
                dict(
                    issue_no=row.issue_no,
                    observed_utc=row.observed_utc,
                    sale_end_utc=row.sale_end_utc,
                    official_verification=row.official_verification,
                )
                for row in session.scalars(select(SfcIssue))
            ]
        failures = list(previous_pending)
        for summary in (previous_schedule or {}).values():
            failures.extend(summary.get("pending_amendments", ()))
        for row in known:
            failures.extend((row["official_verification"] or {}).get("pending_amendments", ()))
        retry = pending_amendments(failures, [], datetime.now(UTC))
        try:
            notices = fetch_official_notices(retry) if retry else fetch_official_notices()
        except UnreadableAmendments as exc:
            failures.extend(exc.pending)
            notices = exc.notices
            source_error = exc.weekly_error
        except (OSError, ValueError, requests.RequestException) as exc:
            notices = []
            source_error = f"{type(exc).__name__}: {exc}"[:200]
            unavailable = isinstance(exc, (OSError, requests.RequestException)) and not isinstance(
                exc, ValueError
            )
        if not notices and (source_error or not failures):
            result["official_error"] = source_error or "本次没有取得官方期次公告"
        result["official_unavailable"] = unavailable
        _resolve_schedule(result, notices, previous_schedule, known, datetime.now(UTC), failures)
        result["requests_scope"] = "500.com"
        result["requests"] += 1
        root_capture, root_error = None, None
        try:
            root_capture = _capture_500(source)
            observed = _sequence_time(root_capture[2])
        except _SOURCE_ERRORS as exc:
            root_capture = None
            root_error = exc
            observed = datetime.now(UTC)
            result["primary_error"] = f"{type(exc).__name__}: {exc}"[:200]
        root = root_capture[0] if root_capture else None
        # Periods are followed in order, one at a time (zucai.sfc_sequence). The root page
        # is taken unless it shows a period that is not in line yet; when it is not the
        # period that is due, that one is asked for by number with one more GET.
        selected, prior_checks = _resolve_schedule(
            result, notices, previous_schedule, known, observed, failures
        )
        provider_windows = [
            dict(issue_no=row["issue_no"], sale_end_utc=row["sale_end_utc"])
            for row in known
            if (not root or row["issue_no"] != root.issue_no) and row["issue_no"] in prior_checks
        ]
        if root:
            provider_windows.append(dict(issue_no=root.issue_no, sale_end_utc=root.sale_end_utc))
        state = sequence(
            period_windows(provider_windows, result["official_schedule"], observed).values(),
            observed,
        )
        captures = []
        if root:
            result["root_issue_no"] = root.issue_no
            if root.issue_no in state["ahead"]:
                result["skipped_ahead_issue_no"] = root.issue_no
            else:
                captures.append(root_capture)
        wanted = [state["current"]] if state["current"] else list(state["expected"][:1])
        if (
            root
            and wanted
            and wanted[0] != root.issue_no
            and root.issue_no not in state["expected"]
        ):
            result["due_issue_no"] = wanted[0]
            result["requests"] += 1
            try:
                captures.append(_capture_500(source, issue=wanted[0]))
                result["due_status"] = "collected"
            except _SOURCE_ERRORS as exc:
                # Not published yet (or unreadable this time): nothing is invented and no
                # other period stands in for it.
                result["due_status"] = "unavailable"
                result["due_detail"] = (f"{type(exc).__name__}: {exc}")[:200]
        if root_error or (state["current"] and result.get("due_status") == "unavailable"):
            backup = _capture_okooo(
                state["current"], result["okooo"], provider_windows, result["official_schedule"]
            )
        if root_error and not backup:
            raise root_error
        bridges, bridge_observed = (), observed
        if root:
            result["requests"] += int(jc_root is None or not jc_root.attempted)
            try:
                if jc_root is None:
                    bridges, bridge_observed = _collect_500_bridge(source)
                else:
                    bridges, bridge_observed = _collect_500_bridge(source, jc_root=jc_root)
            except _SOURCE_ERRORS as exc:
                if not backup:
                    raise
                result["bridge_error"] = f"{type(exc).__name__}: {exc}"[:200]
                captures = []
        if backup:
            captures.append(backup)
        with session_scope(engine) as session:
            rows, primary = [], None
            for issue, issue_raw, issue_meta in captures:
                okooo = is_okooo(issue_meta["url"])
                record = ingest_issue(
                    session,
                    issue,
                    observed=datetime.fromisoformat(issue_meta["fetched_at_utc"]),
                    url=issue_meta["url"],
                    raw_sha256=issue_meta.get("response_sha256")
                    or hashlib.sha256(issue_raw).hexdigest(),
                    bridges=() if okooo else bridges,
                    mapped_only=True,
                    # T78: a row no regular source carries stands as its own fixture.
                    own_fixtures=not okooo,
                )
                unmatched = [
                    dict(
                        issue_no=issue.issue_no,
                        seq=r["seq"],
                        fixture_id=r["fixture_id"],
                        reason=r["reason"],
                        candidates=json.dumps(r["candidate_ids"], sort_keys=True),
                        provider_home_id=r["provider_home_id"],
                        provider_away_id=r["provider_away_id"],
                        conflicting_identities=json.dumps(
                            r["mapping_evidence"], ensure_ascii=False, sort_keys=True
                        ),
                        raw_sha256=record.raw_sha256,
                    )
                    for r in record.snapshot
                    if not r["match_id"]
                ]
                rows.extend(unmatched)
                # The last capture is the due period when one was fetched by number.
                primary = (issue.issue_no, len(unmatched), OKOOO_PROVIDER if okooo else "500.com")
            # Fixture/team ID bridges can establish a name match only after their own capture.
            checked_at = max(
                observed,
                bridge_observed,
                *(_sequence_time(info) for _, _, info in captures),
            )
            _check_archived_issues(
                session,
                selected,
                checked_at,
                {n["issue_no"] for n in notices if "issue_no" in n}
                | {p.issue_no for p, _, _ in captures},
                result,
                prior_checks,
            )
            if primary:
                result.update(
                    issue_no=primary[0],
                    mapped=14 - primary[1],
                    unmatched=primary[1],
                    linked_existing=14 - primary[1],
                    rejected_rows=primary[1],
                    source_provider=primary[2],
                )
            audit.parent.mkdir(parents=True, exist_ok=True)
            fields = [
                "issue_no",
                "seq",
                "fixture_id",
                "reason",
                "candidates",
                "provider_home_id",
                "provider_away_id",
                "conflicting_identities",
                "raw_sha256",
            ]
            previous_audit = audit.read_bytes() if audit.exists() else None
            existing = []
            if audit.exists():
                with audit.open(encoding="utf-8", newline="") as stream:
                    existing = list(csv.DictReader(stream))
            # Capture timestamps/hashes in evidence change every sync; identity
            # deduplication still uses the original five-column logical row key.
            seen = {tuple(str(r[k]) for k in fields[:5]) for r in existing}
            for row in rows:
                key = tuple(str(row[k]) for k in fields[:5])
                if key not in seen:
                    existing.append(row)
                    seen.add(key)
            with temporary.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows(existing)
            temporary.replace(audit)
            published = True
        result["status"] = "complete"
        if backup:
            result["okooo"].update(
                status="collected",
                source_url=backup[2]["url"],
                response_sha256=backup[2]["response_sha256"],
                observed_utc=backup[2]["fetched_at_utc"],
            )
            if result.get("due_status") == "unavailable":
                result["due_status"] = "collected"
    except Exception as exc:
        if published:
            if previous_audit is None:
                audit.unlink(missing_ok=True)
            else:
                from zucai.sources.base import _atomic_write

                _atomic_write(audit, previous_audit)
        result.update(inserted=0, linked_existing=0, rejected_rows=0, mapped=0, unmatched=0)
        detail = f"{type(exc).__name__}: {exc}"[:200] if str(exc) else type(exc).__name__
        if backup:
            result["okooo"]["reason"] = "备用名单归档失败：" + detail
        result["errors"] = ["SFC collection failed: " + detail]
        LOG.warning("SFC collection failed (%s)", detail)
    finally:
        temporary.unlink(missing_ok=True)
        if source is not None:
            source.session.close()
        if engine is not None:
            engine.dispose()
    return result
