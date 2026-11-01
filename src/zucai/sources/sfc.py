"""Store one complete SFC issue and its conservative cross-source mapping."""

import csv
import hashlib
import logging
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, select

from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, SfcIssue, SfcIssueMatch, Team
from zucai.sources.c500 import C500FixtureBridge, C500Sfc, SfcSnapshot
from zucai.sources.teams import normalize_team, upsert_team

LOG = logging.getLogger(__name__)


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


def _map_fixture(session, fixture, bridges):
    own_id = "c500:" + fixture.fixture_id
    own = session.get(Match, own_id)
    bridge = bridges.get(fixture.fixture_id)
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
            datetime.fromisoformat(bridge["kickoff_utc"]) != fixture.kickoff_utc
        ):
            raise ValueError(f"{own_id} ID对照主客球队或开球冲突")
        target = session.get(Match, bridge["jc_id"])
        if target is not None:
            linked.append(target)
        evidence = {"basis": "provider_fixture_and_team_ids", **bridge}
    linked = list({m.match_id: m for m in linked}.values())
    if len(linked) > 1:
        raise ValueError(f"{own_id} ID对照指向多个比赛")
    if linked:
        match = linked[0]
        if (
            _league_key(match.league) != _league_key(fixture.league)
            or datetime.fromisoformat(match.kickoff_utc) != fixture.kickoff_utc
        ):
            raise ValueError(f"{own_id} ID对照与比赛联赛或开球冲突")
        return match, "matched", [match.match_id], evidence
    home_name = bridge["home"] if bridge else fixture.home
    away_name = bridge["away"] if bridge else fixture.away
    home, away = _find_team(session, home_name), _find_team(session, away_name)
    if own and (
        own.league != fixture.league
        or datetime.fromisoformat(own.kickoff_utc) != fixture.kickoff_utc
        or home is None
        or away is None
        or own.home_team_id != home.id
        or own.away_team_id != away.id
    ):
        raise ValueError(f"已有比赛{own_id}身份冲突，须人工核对")
    candidates = []
    if home and away:
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
    if len(candidates) == 1:
        return candidates[0], "matched", [candidates[0].match_id], evidence
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


def ingest_issue(
    session, issue: SfcSnapshot, *, observed: datetime, url: str, raw_sha256: str, bridges=()
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
    for row in (record.snapshot or []) if record else []:
        saved = row.get("mapping_evidence", {})
        if saved.get("basis") != "provider_fixture_and_team_ids":
            continue
        fid = row["fixture_id"]
        current = bridge_map.get(fid)
        if current and any(current[k] != saved[k] for k in ("jc_id", "home_id", "away_id")):
            raise ValueError(f"{fid} 新旧比赛ID对照冲突，须人工核对")
        if current is None:
            bridge_map[fid] = dict(saved, reused_from_issue_snapshot=True)
    snapshot = []
    for fixture in issue.fixtures:
        match, mapping, candidates, evidence = _map_fixture(session, fixture, bridge_map)
        home, away = session.get(Team, match.home_team_id), session.get(Team, match.away_team_id)
        snapshot.append(
            dict(
                seq=fixture.seq,
                fixture_id=fixture.fixture_id,
                home=fixture.home,
                away=fixture.away,
                league=fixture.league,
                kickoff_utc=fixture.kickoff_utc.isoformat(),
                source_ended=fixture.ended,
                match_id=match.match_id,
                mapping=mapping,
                candidate_ids=candidates,
                mapping_evidence=evidence,
                provider_home_id=fixture.home_id,
                provider_away_id=fixture.away_id,
                matched_identity=dict(
                    home=home.name,
                    away=away.name,
                    league=match.league,
                    source=match.source,
                    kickoff_utc=fixture.kickoff_utc.isoformat(),
                ),
            )
        )
    if len(snapshot) != 14 or len({x["match_id"] for x in snapshot}) != 14:
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
