"""Capture missing JC schedules from one checked 500 root observation."""

import csv
import json
import re
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path

from lxml.etree import ParserError
from requests import RequestException
from sqlalchemy import select

from zucai.db import get_engine, session_scope, upsert_match
from zucai.models import Match, Team
from zucai.sources.base import _atomic_write
from zucai.sources.c500 import URL
from zucai.sources.sporttery import utc_stamp

STATE_FILE = "state/jc_fixture_backup.json"


def _empty():
    return {"schema_version": 1, "active": {}, "reconciled": []}


def _instant(value):
    instant = datetime.fromisoformat(value)
    if instant.utcoffset() is None:
        raise ValueError("500 网备用赛程缺少时区")
    return instant


def read_state(root):
    path = Path(root) / STATE_FILE
    if path.is_symlink():
        raise ValueError("500 网备用赛程档案不允许符号链接")
    if not path.exists():
        return _empty()
    state = json.loads(path.read_bytes())
    if state.get("schema_version") != 1 or not isinstance(state.get("active"), dict):
        raise ValueError("500 网备用赛程档案格式无效")
    for match_id, row in state["active"].items():
        if (
            not re.fullmatch(r"jc:[1-9][0-9]*", match_id)
            or row.get("url") != URL
            or not re.fullmatch(r"[0-9a-f]{64}", row.get("raw_sha256", ""))
            or _instant(row["observed_utc"]) >= _instant(row["kickoff_utc"])
            or any(
                type(row.get(k)) is not int or row[k] <= 0 for k in ("home_team_id", "away_team_id")
            )
            or row["home_team_id"] == row["away_team_id"]
        ):
            raise ValueError(f"{match_id} 500 网备用赛程证据无效")
        date.fromisoformat(row["sales_date"])
    if not isinstance(state.get("reconciled"), list):
        raise ValueError("500 网备用赛程审计格式无效")
    return state


def matches_proof(current, proof, observed):
    return (
        current is not None
        and current.source == "jc"
        and current.status == "scheduled"
        and observed >= _instant(proof.get("official_excluded_utc") or proof["observed_utc"])
        and _instant(current.kickoff_utc) == _instant(proof["kickoff_utc"])
        and all(
            getattr(current, k) == proof[k]
            for k in ("home_team_id", "away_team_id", "league", "jc_num")
        )
    )


def visible_at(proof, at):
    excluded = proof.get("official_excluded_utc")
    return _instant(proof["observed_utc"]) <= at and (not excluded or at < _instant(excluded))


def officially_excluded(state, catalog, official_clock, at):
    """Keep an official withdrawal until a later successfully collected sale supersedes it."""
    latest = {}
    for event in state.get("reconciled", ()):
        stamp = _instant(event["observed_utc"])
        key = event["match_id"]
        if stamp <= at and (key not in latest or latest[key][0] <= stamp):
            latest[key] = (stamp, event.get("retained"))
    stopped = {key for key, (_, retained) in latest.items() if retained is False}
    if not stopped or not official_clock:
        return stopped
    clock = min(at, _instant(official_clock))
    observed = {}
    for row in catalog or ():
        key = row.get("match_id")
        if key not in stopped:
            continue
        stamp = _instant(row["captured_utc"])
        if latest[key][0] < stamp <= clock and (key not in observed or observed[key][0] <= stamp):
            observed[key] = (stamp, row.get("retained"))
    return stopped - {key for key, (_, retained) in observed.items() if retained == "1"}


def reconciliation(match_id, proof, observed, official, *, identity_changed=False, retained=True):
    changes = {
        key: {"backup": proof.get(key), "official": value}
        for key, value in official.items()
        if (proof.get(key) or None) != (value or None)
    }
    return {
        "match_id": match_id,
        "observed_utc": utc_stamp(observed),
        "backup_observed_utc": proof["observed_utc"],
        "backup_raw_sha256": proof["raw_sha256"],
        "identity_changed": identity_changed,
        "retained": retained,
        "changes": changes,
    }


def reconcile_excluded(session, catalogue, proofs, observed):
    """An official non-selling observation revokes backup sale scope without creating teams."""
    from zucai.sources.sporttery_aliases import approved_canonical, load_aliases
    from zucai.sources.teams import normalize_team

    counts = Counter(row["match_id"] for row in catalogue)
    aliases = load_aliases()
    updates = []
    for row in catalogue:
        key = row["match_id"]
        if key not in proofs or row["retained"] != "0" or counts[key] != 1:
            continue
        proof = proofs[key]
        match = session.get(Match, key)
        if not matches_proof(match, proof, observed):
            continue
        official = {
            "home": row["home"],
            "away": row["away"],
            "league": row["league"],
            "kickoff_utc": row["kickoff_utc"],
            "jc_num": row["jc_num"],
            "sales_date": row["business_date"],
            "sale_status": f"{row['matchStatus']}/{row['sellStatus']}",
        }
        owners = []
        for side in ("home", "away"):
            name = row[side]
            try:
                canonical = approved_canonical(aliases, row["league"], name) or normalize_team(name)
            except ValueError:
                owners.append(None)
                continue
            owners.append(session.scalar(select(Team).where(Team.name == canonical)))
        complete = bool(all(owners) and owners[0].id != owners[1].id and all(official.values()))
        identity_changed = False
        if complete:
            home, away = owners
            identity_changed = (home.id, away.id, official["league"]) != (
                proof["home_team_id"],
                proof["away_team_id"],
                proof["league"],
            )
            upsert_match(
                session,
                match_id=key,
                source="jc",
                league=official["league"],
                home_team_id=home.id,
                away_team_id=away.id,
                kickoff_utc=_instant(official["kickoff_utc"]),
                jc_num=official["jc_num"],
                status="scheduled",
            )
            official.update(home=home.name, away=away.name)
        updates.append(
            dict(
                reconciliation(
                    key,
                    proof,
                    observed,
                    official,
                    identity_changed=identity_changed,
                    retained=False,
                ),
                identity_pending=not complete,
            )
        )
    return updates


def _official_roster(root, captured):
    ids, slots = set(), set()
    if not captured:
        return ids, slots
    moment = _instant(captured)
    directory = root / "state/audit"
    names = (directory / "jc_catalog.csv", directory / "jc_catalog" / f"{moment:%Y-%m}.csv")
    for path in names:
        if path.is_file():
            with path.open(encoding="utf-8", newline="") as stream:
                for row in csv.DictReader(stream, strict=True):
                    if not row.get("captured_utc") or _instant(row["captured_utc"]) != moment:
                        continue
                    if row.get("match_id"):
                        ids.add(row["match_id"])
                    if row.get("business_date") and row.get("jc_num"):
                        slots.add((row["business_date"], row["jc_num"]))
    return ids, slots


def _details(row):
    return {
        "match_id": row.match_id,
        "jc_num": row.jc_num,
        "sales_date": row.sales_date.isoformat() if row.sales_date else None,
        "league": row.league,
        "home": row.home,
        "away": row.away,
        "home_provider_id": row.home_id,
        "away_provider_id": row.away_id,
        "kickoff_utc": utc_stamp(row.kickoff_utc) if row.kickoff_utc else None,
    }


def _save(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(
        path, (json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    )


def collect_backup(database, official, jc_root, *, at=None):
    """Use the existing root sample, with a single lazy GET only after official failure."""
    from zucai.jc_backup_identity import FixtureRejection, resolve_fixture

    at = datetime.now(UTC) if at is None else at
    directory = Path(database).parent
    state = read_state(directory)
    active = state["active"]
    reconciled = official.pop("_backup_reconciliations", [])
    for row in reconciled:
        if row.get("identity_pending") and row["match_id"] in active:
            active[row["match_id"]]["official_excluded_utc"] = row["observed_utc"]
        else:
            active.pop(row["match_id"], None)
    state["reconciled"].extend(reconciled)
    path = directory / STATE_FILE
    prior = path.read_bytes() if path.exists() else None
    schedule_status = official.get("schedule_status")
    failed = schedule_status == "failed"
    summary = dict(
        status="incomplete",
        official_schedule_status=schedule_status,
        observed_utc=utc_stamp(at),
        inserted=0,
        active=0,
        gaps=[],
        reconciled=reconciled,
    )
    try:
        if failed or jc_root.attempted:
            try:
                _, meta = jc_root.snapshot()
                observed = _instant(meta["fetched_at_utc"])
                rows = jc_root.fixtures()
            except (RequestException, OSError, ValueError, ParserError):
                summary.update(status="unavailable", reason="500 网竞彩根页未取得可核实赛程")
                rows = []
        else:
            rows = []
        ids, slots = _official_roster(directory, official.get("schedule_updated_utc"))
        counts = Counter((row.sales_date, row.jc_num) for row in rows)
        engine = get_engine(database)
        try:
            with session_scope(engine) as session:
                gaps = {}
                for row in rows:
                    day = row.sales_date.isoformat() if row.sales_date else None
                    if (
                        row.match_id in ids
                        or (day, row.jc_num) in slots
                        or session.get(Match, row.match_id) is not None
                        or (row.kickoff_utc is not None and row.kickoff_utc <= observed)
                    ):
                        continue
                    detail = _details(row)
                    proof = dict(
                        url=URL, observed_utc=utc_stamp(observed), raw_sha256=meta["raw_sha256"]
                    )
                    try:
                        if counts[row.sales_date, row.jc_num] > 1:
                            raise FixtureRejection(
                                "fixture_conflict", "500 网同一销售日与竞彩编号对应多场比赛"
                            )
                        identity = resolve_fixture(session, row, rows, backup_ids=active)
                    except FixtureRejection as exc:
                        gaps.setdefault(
                            row.match_id,
                            {
                                **detail,
                                **proof,
                                "reason": str(exc),
                                "code": exc.code,
                                "details": exc.details,
                            },
                        )
                        continue
                    fields = {
                        "league": identity.league,
                        "home_team_id": identity.home_team_id,
                        "away_team_id": identity.away_team_id,
                    }
                    upsert_match(
                        session,
                        match_id=row.match_id,
                        source="jc",
                        **fields,
                        jc_num=row.jc_num,
                        kickoff_utc=row.kickoff_utc,
                        status="scheduled",
                    )
                    active[row.match_id] = {
                        **detail,
                        **fields,
                        **proof,
                        "identity": identity.evidence,
                        "home": session.get(Team, identity.home_team_id).name,
                        "away": session.get(Team, identity.away_team_id).name,
                        "sale_status": "未核实",
                    }
                    summary["inserted"] += 1
                summary["gaps"] = list(gaps.values())
                if summary["inserted"] or reconciled:
                    _save(path, state)
        finally:
            engine.dispose()
    except Exception:
        if prior is None:
            path.unlink(missing_ok=True)
        elif not path.exists() or path.read_bytes() != prior:
            _atomic_write(path, prior)
        raise
    summary["active"] = sum(
        match_id not in ids and visible_at(row, at) and _instant(row["kickoff_utc"]) > at
        for match_id, row in active.items()
    )
    if summary["active"]:
        summary["status"] = "used"
    elif reconciled:
        summary["status"] = "reconciled"
    if failed or summary["active"] or summary["gaps"] or reconciled:
        return summary
    return None
