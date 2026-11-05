"""Content-versioned injuries and compact pointers to complete compressed receipts."""

import gzip
import hashlib
import json
from collections import defaultdict
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from zucai.models import ApifFixtureIndex, Match, MatchInjury
from zucai.sources.api_football import LEAGUES
from zucai.sources.apif_injuries import INJURY_TYPES, parse_injuries
from zucai.sources.apif_stats import identifier, instant
from zucai.sources.base import _atomic_write
from zucai.sources.football_data import read_json
from zucai.stats_storage import load_raw, read_audit, transaction, write_audit

AUDIT_NAME = "injuries_ingest_audit.csv"
RECEIPT_VERSION = "t38-injury-capture-v3"
CAPTURE_DIRECTORY = "state/injuries_captures"


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def injury_content_sha256(raw_value, identity):
    return hashlib.sha256(_encoded(dict(raw_value=raw_value, **identity))).hexdigest()


def injury_record_key(player_id, raw_value):
    # An absent ID is never bound by player name or merged with another unknown player.
    value = (
        ["player", player_id]
        if player_id is not None
        else ["unidentified", hashlib.sha256(_encoded(raw_value)).hexdigest()]
    )
    return _encoded(value).decode()


def read_capture_receipt(directory, detail):
    """Verify a local compressed capture before treating its membership as complete."""
    sha = detail.get("receipt_sha256", "")
    if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise ValueError("伤停捕获回执 SHA256 无效")
    relative = f"{CAPTURE_DIRECTORY}/{sha}.json.gz"
    if detail.get("receipt_path") != relative:
        raise ValueError("伤停捕获回执路径无效")
    path = Path(directory) / relative
    raw = path.read_bytes()
    if type(detail.get("receipt_bytes")) is not int or (
        len(raw) != detail["receipt_bytes"] or hashlib.sha256(raw).hexdigest() != sha
    ):
        raise ValueError("伤停捕获回执校验失败")
    payload = json.loads(gzip.decompress(raw))
    if (
        not isinstance(payload, dict)
        or payload.get("receipt_version") != RECEIPT_VERSION
        or payload.get("captured_utc") != detail["captured_utc"]
        or payload.get("raw_sha256") != detail["raw_sha256"]
        or not isinstance(payload.get("fixtures"), dict)
        or len(payload["fixtures"]) != detail["fixture_count"]
        or len(payload.get("page_errors", [])) != detail["page_errors"]
    ):
        raise ValueError("伤停捕获回执范围或计数不符")
    snapshots = payload["fixtures"].values()
    if (
        any(
            not isinstance(snapshot, dict)
            or set(snapshot) != {"entries", "rejected", "conflicts", "unresolved"}
            or any(not isinstance(values, list) for values in snapshot.values())
            for snapshot in snapshots
        )
        or sum(len(snapshot["entries"]) for snapshot in snapshots) != detail["parsed_rows"]
    ):
        raise ValueError("伤停捕获回执成员不完整")
    return payload


def verify_capture_row(row, payload):
    if any(payload[key] != row[key] for key in ("captured_utc", "raw_sha256")) or (
        payload["capture_record_key"] != row["record_key"]
    ):
        raise ValueError("伤停捕获回执与审计时钟或键不符")


def capture_receipt_paths(directory):
    """Return all verified local capture dependencies named by the compact CSV."""
    paths = set()
    for row in read_audit(Path(directory) / "state/audit" / AUDIT_NAME):
        detail = json.loads(row["detail"])
        if isinstance(detail, dict) and detail.get("receipt_version") == RECEIPT_VERSION:
            verify_capture_row(row, read_capture_receipt(directory, detail))
            paths.add(Path(directory) / detail["receipt_path"])
    return sorted(paths)


def _write_capture(directory, payload, created):
    raw = gzip.compress(_encoded(payload), mtime=0)
    sha = hashlib.sha256(raw).hexdigest()
    relative = f"{CAPTURE_DIRECTORY}/{sha}.json.gz"
    path = Path(directory) / relative
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError("伤停捕获回执内容地址冲突")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        created.append(path)
        _atomic_write(path, raw)
    return dict(receipt_path=relative, receipt_sha256=sha, receipt_bytes=len(raw))


def _link(session, injury, league):
    index = session.get(ApifFixtureIndex, injury.provider_fixture_id)
    empty = dict(match_id=None, team_id=None, side=None)
    if index is None:
        return empty, "fixture_index_missing"
    match = session.get(Match, index.match_id)
    if (
        index.provider_league_id != injury.provider_league_id
        or index.season != injury.provider_season
        or index.league != league
        or abs(instant(index.kickoff_utc) - injury.fixture_kickoff_utc) > timedelta(hours=3)
        or match is None
        or (match.league, match.home_team_id, match.away_team_id)
        != (index.league, index.home_team_id, index.away_team_id)
        or abs(instant(match.kickoff_utc) - injury.fixture_kickoff_utc) > timedelta(hours=3)
    ):
        return empty, "fixture_index_mismatch"
    for side in ("home", "away"):
        if injury.provider_team_id == getattr(index, side + "_provider_id"):
            return dict(
                match_id=index.match_id, team_id=getattr(index, side + "_team_id"), side=side
            ), None
    return empty, "provider_team_id_not_in_fixture"


def ingest_injuries(database, raw_file, *, league_id, season, daily=False, raw_location=None):
    """Replay one verified capture; unchanged later content only extends its last clock."""
    identifier(league_id)
    identifier(season)
    config = read_json(LEAGUES).get(str(league_id))
    if not config:
        raise ValueError("伤停联赛不在既有 API-Football 配置中")
    league = config["league"]
    raw, metadata = load_raw(
        raw_file,
        endpoint="injuries",
        parameters={"league": league_id, "season": season},
        location=raw_location,
    )
    capture_key = f"capture:{league_id}:{season}:{metadata['captured_utc']}:{int(daily)}"
    base = dict(
        provider_fixture_id="", endpoint="injuries", league=league, season=season, **metadata
    )
    created = []
    try:
        with transaction(database, AUDIT_NAME) as (session, audit):
            for receipt in read_audit(audit):
                if (
                    receipt["record_key"] == capture_key
                    and receipt["raw_sha256"] == metadata["raw_sha256"]
                    and receipt["outcome"] == "ingested"
                ):
                    read_capture_receipt(Path(database).parent, json.loads(receipt["detail"]))
                    return dict(inserted=0, rejected=0, conflicts=0, unlinked=0, duplicate=True)
            return _ingest_capture(
                session,
                audit,
                database,
                raw,
                metadata,
                base,
                capture_key,
                created,
                league_id=league_id,
                season=season,
                league=league,
                daily=daily,
            )
    except BaseException:
        # CSV and DB roll back together in transaction(); uncommitted payloads do too.
        for path in created:
            path.unlink(missing_ok=True)
        raise


def _ingest_capture(
    session,
    audit,
    database,
    raw,
    metadata,
    base,
    capture_key,
    created,
    *,
    league_id,
    season,
    league,
    daily,
):
    rejected, invalid = [], False
    try:
        injuries = parse_injuries(
            raw,
            league_id=league_id,
            season=season,
            captured_utc=metadata["captured_utc"],
            daily=daily,
            rejected=rejected,
        )
    except ValueError as exc:
        injuries, invalid = [], True
        rejected.append(dict(provider_fixture_id=None, reason=str(exc)))
    fixtures = defaultdict(lambda: dict(entries=[], rejected=[], conflicts=[], unresolved=[]))
    inserted, unlinked, page_errors = 0, 0, []
    for item in rejected:
        if item["provider_fixture_id"]:
            fixtures[item["provider_fixture_id"]]["rejected"].append(item)
        else:
            page_errors.append(item)
    versions = defaultdict(list)
    for row in session.scalars(
        select(MatchInjury).where(
            MatchInjury.provider == "apif",
            MatchInjury.league == league,
            MatchInjury.provider_season == season,
        )
    ):
        versions[row.provider_fixture_id, row.record_key].append(row)
    for injury in injuries:
        identity, link_reason = _link(session, injury, league)
        reasons = [link_reason] if link_reason else []
        if injury.player_id is None:
            reasons.append("player_id_missing")
        if injury.injury_type not in INJURY_TYPES:
            reasons.append("unknown_injury_type")
        key = injury_record_key(injury.player_id, injury.raw_value)
        content_sha = injury_content_sha256(injury.raw_value, identity)
        history = versions[injury.provider_fixture_id, key]
        eligible = [row for row in history if instant(row.captured_utc) <= injury.captured_utc]
        previous = max(
            eligible, key=lambda row: (instant(row.captured_utc), row.version), default=None
        )
        if previous is not None and previous.content_sha256 == content_sha:
            previous.last_captured_utc = max(
                instant(previous.last_captured_utc), injury.captured_utc
            )
        else:
            fields = asdict(injury)
            fields.pop("provider_league_id")
            fields.update(
                provider="apif",
                endpoint="injuries",
                league=league,
                **identity,
                **metadata,
                last_captured_utc=metadata["captured_utc"],
                record_key=key,
                content_sha256=content_sha,
                version=max((row.version for row in history), default=0) + 1,
            )
            previous = MatchInjury(**fields)
            session.add(previous)
            session.flush()
            history.append(previous)
            inserted += 1
        snapshot = fixtures[injury.provider_fixture_id]
        snapshot["entries"].append(
            dict(
                row_id=previous.id,
                record_key=key,
                raw_sha256=previous.raw_sha256,
                version=previous.version,
                outcome="observed",
            )
        )
        if reasons:
            unlinked += 1
            snapshot["unresolved"].append(dict(record_key=key, row_id=previous.id, reasons=reasons))
    payload = dict(
        receipt_version=RECEIPT_VERSION,
        capture_record_key=capture_key,
        captured_utc=metadata["captured_utc"],
        raw_sha256=metadata["raw_sha256"],
        fixtures=dict(fixtures),
        page_errors=page_errors,
    )
    pointer = _write_capture(Path(database).parent, payload, created)
    total_rejected = len(rejected) + unlinked
    detail = dict(
        receipt_version=RECEIPT_VERSION,
        provider="apif",
        provider_league_id=league_id,
        provider_season=season,
        daily=bool(daily),
        **metadata,
        empty=not injuries and not rejected,
        parsed_rows=len(injuries),
        inserted=inserted,
        rejected=total_rejected,
        unresolved=unlinked,
        page_errors=len(page_errors),
        fixture_count=len(fixtures),
        **pointer,
    )
    write_audit(
        audit,
        [
            dict(
                base,
                record_key=capture_key,
                outcome="rejected" if invalid else "ingested",
                reason="invalid_page" if invalid else "",
                detail=detail,
            )
        ],
    )
    result = dict(
        inserted=inserted,
        rejected=total_rejected,
        conflicts=0,
        unlinked=unlinked,
        empty=detail["empty"],
        duplicate=False,
    )
    if invalid:
        result["invalid"] = True
    return result
