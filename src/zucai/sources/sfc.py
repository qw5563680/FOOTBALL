"""Store one complete SFC issue and its conservative cross-source mapping."""

import copy
import csv
import hashlib
import logging
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, select

from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, SfcIssue, SfcIssueMatch, Team
from zucai.nations import LEAGUE as NATIONS_LEAGUE
from zucai.nations import sfc_team
from zucai.sources.c500 import C500FixtureBridge, C500Sfc, SfcFixture, SfcSnapshot
from zucai.sources.sporttery import BEIJING
from zucai.sources.teams import normalize_team, upsert_team

LOG = logging.getLogger(__name__)


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
            or (
                (a.league, a.home_team_id, a.away_team_id)
                == (b.league, b.home_team_id, b.away_team_id)
                and datetime.fromisoformat(a.kickoff_utc) == datetime.fromisoformat(b.kickoff_utc)
            )
        )

    group, pending = [ordered[0]], ordered[1:]
    while pending:
        joined = [m for m in pending if any(equivalent(m, other) for other in group)]
        if not joined:
            return None
        group.extend(joined)
        pending = [m for m in pending if m not in joined]
    return ordered[0]


def _map_fixture(session, fixture, bridges, *, mapped_only=False):
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
            names = (
                sfc_team(fixture.home_id, fixture.home),
                sfc_team(fixture.away_id, fixture.away),
            )
            teams = [_find_team(session, name) for name in names]
            if (
                match.league != NATIONS_LEAGUE
                or datetime.fromisoformat(match.kickoff_utc) != fixture.kickoff_utc
                or [team.id if team else None for team in teams]
                != [match.home_team_id, match.away_team_id]
            ):
                raise ValueError(f"{own_id} 欧国联的球队、主客或原始开球时间不一致")
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
        home_name = sfc_team(fixture.home_id, fixture.home)
        away_name = sfc_team(fixture.away_id, fixture.away)
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
    # An earlier SFC-only record may retain the source's abbreviated names.
    # Check that original identity before considering the bridge's longer names;
    # adding an ID link must not rename/merge teams or invent a JC match.
    own_home = (_find_team(session, fixture.home) or home) if own else home
    own_away = (_find_team(session, fixture.away) or away) if own else away
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


def _snapshot_rows(session, issue, bridge_map, *, mapped_only, bridge_conflicts=()):
    snapshot = []
    for fixture in issue.fixtures:
        try:
            if fixture.fixture_id in bridge_conflicts:
                raise ValueError(f"{fixture.fixture_id} 新旧比赛ID对照冲突，须人工核对")
            match, mapping, candidates, evidence = _map_fixture(
                session, fixture, bridge_map, mapped_only=mapped_only
            )
        except ValueError as exc:
            if not mapped_only:
                raise
            match, mapping = None, "ambiguous"
            evidence = _conflict_evidence(session, fixture, bridge_map.get(fixture.fixture_id), exc)
            if fixture.fixture_id in bridge_conflicts:
                evidence["previous_bridge"] = bridge_conflicts[fixture.fixture_id]
            candidates = [c["match_id"] for c in evidence["candidate_identities"]]
        source_time = fixture.kickoff_utc.isoformat()
        local = fixture.kickoff_utc.astimezone(BEIJING)
        placeholder = local.time().isoformat() == "00:00:00" and any(
            f.kickoff_utc.astimezone(BEIJING).date() != local.date()
            for f in issue.fixtures
            if f.seq != fixture.seq
        )
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
    for index, row in enumerate(rows):
        if row["match_id"] is None or row["league"] == NATIONS_LEAGUE:
            candidate = refreshed[index]
            if row["league"] == NATIONS_LEAGUE and (
                row.get("kickoff_basis") == "placeholder"
                or (
                    row["match_id"]
                    and candidate["match_id"]
                    and row["match_id"] != candidate["match_id"]
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
            continue
        # Previously linked rows (including manual T07 c500 rows) keep their
        # identity evidence. Only the clock is refreshed; the report still checks
        # all other identity fields against the DB and the frozen forecast.
        match = session.get(Match, row["match_id"])
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
):
    if observed.utcoffset() is None:
        raise ValueError("采集时间必须有时区")
    record = session.get(SfcIssue, issue.issue_no)
    if record and record.observed_utc:
        previous = datetime.fromisoformat(record.observed_utc)
        if observed < previous or (observed == previous and raw_sha256 != record.raw_sha256):
            raise ValueError("旧期次快照不能覆盖新版本")
    bridge_map = {b["fixture_id"]: b for b in bridges}
    if len(bridge_map) != len(bridges):
        raise ValueError("重复比赛ID对照")
    # The public current page rolls past old fixtures. Preserve their archived
    # ID evidence, including its original capture time, without inventing aliases.
    bridge_conflicts = {}
    for row in (record.snapshot or []) if record else []:
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
        session, issue, bridge_map, mapped_only=mapped_only, bridge_conflicts=bridge_conflicts
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


def collect_current_sfc(db):
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
    )
    source = engine = None
    audit = Path(db).parent / "state/audit/sfc_unmatched.csv"
    previous_audit = None
    published = False
    temporary = audit.with_suffix(".tmp")
    try:
        source = C500Sfc()
        engine = get_engine(db)
        result["requests"] += 1
        path = source.fetch_raw(current_only=True)
        raw, meta = source.store.load(path), source.store.load_metadata(path)
        issue = source.parse(raw)[0]
        # Share the no-retry session and limiter: one GET per root, spaced >=1s.
        bridge_source = C500FixtureBridge(store=source.store, session=source.session)
        bridge_source.limiter = source.limiter
        result["requests"] += 1
        bridge_path = bridge_source.fetch_raw()
        bridge_raw = source.store.load(bridge_path)
        bridge_meta = source.store.load_metadata(bridge_path)
        bridges = bridge_source.parse(bridge_raw)
        for bridge in bridges:
            bridge["provenance"] = dict(
                url=bridge_meta["url"],
                observed_utc=bridge_meta["fetched_at_utc"],
                raw_sha256=hashlib.sha256(bridge_raw).hexdigest(),
            )
        init_db(engine)
        with session_scope(engine) as session:
            record = ingest_issue(
                session,
                issue,
                observed=datetime.fromisoformat(meta["fetched_at_utc"]),
                url=meta["url"],
                raw_sha256=hashlib.sha256(raw).hexdigest(),
                bridges=bridges,
                mapped_only=True,
            )
            rows = [
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
            result.update(
                issue_no=issue.issue_no,
                mapped=14 - len(rows),
                unmatched=len(rows),
                linked_existing=14 - len(rows),
                rejected_rows=len(rows),
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
    except Exception as exc:
        if published:
            if previous_audit is None:
                audit.unlink(missing_ok=True)
            else:
                from zucai.sources.base import _atomic_write

                _atomic_write(audit, previous_audit)
        result.update(inserted=0, linked_existing=0, rejected_rows=0, mapped=0, unmatched=0)
        detail = f"{type(exc).__name__}: {exc}"[:200] if str(exc) else type(exc).__name__
        result["errors"] = ["SFC collection failed: " + detail]
        LOG.warning("SFC collection failed (%s)", detail)
    finally:
        temporary.unlink(missing_ok=True)
        if source is not None:
            source.session.close()
        if engine is not None:
            engine.dispose()
    return result
