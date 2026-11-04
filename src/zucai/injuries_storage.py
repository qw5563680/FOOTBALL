"""Append injuries and complete page receipts without creating football identities."""

import json
from collections import defaultdict
from dataclasses import asdict
from datetime import timedelta

from sqlalchemy import func, select

from zucai.models import ApifFixtureIndex, Match, MatchInjury
from zucai.sources.api_football import LEAGUES
from zucai.sources.apif_injuries import INJURY_TYPES, parse_injuries
from zucai.sources.apif_stats import identifier, instant
from zucai.sources.football_data import read_json
from zucai.stats_storage import load_raw, read_audit, transaction, write_audit

AUDIT_NAME = "injuries_ingest_audit.csv"
RECEIPT_VERSION = "t38-injury-capture-v2"
SNAPSHOT_VERSION = "t38-injury-fixture-v1"
SNAPSHOT_FIELD_LIMIT = 100_000  # Below Python CSV's default 128 KiB field ceiling.


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


def _record_key(injury):
    return json.dumps(
        [injury.provider_fixture_id, injury.player_id, injury.injury_type, injury.captured_day],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _snapshot_receipts(base, capture_key, fixtures):
    """Bound every JSON field while preserving every member of a complete page."""
    receipts = []
    for fixture_id, snapshot in fixtures.items():
        chunks, chunk, size = [], [], 0
        for kind, values in snapshot.items():
            for value in values:
                item = dict(kind=kind, value=value)
                encoded_size = len(json.dumps(item, ensure_ascii=False, sort_keys=True).encode())
                if encoded_size > SNAPSHOT_FIELD_LIMIT - 2048:
                    raise ValueError("单条伤停审计证据超过有界字段限制")
                if chunk and size + encoded_size + 2 > SNAPSHOT_FIELD_LIMIT - 2048:
                    chunks.append(chunk)
                    chunk, size = [], 0
                chunk.append(item)
                size += encoded_size + 2
        if chunk:
            chunks.append(chunk)
        for number, items in enumerate(chunks):
            detail = dict(
                receipt_version=SNAPSHOT_VERSION,
                capture_record_key=capture_key,
                chunk=number,
                chunks=len(chunks),
                items=items,
            )
            if (
                len(json.dumps(detail, ensure_ascii=False, sort_keys=True).encode())
                > SNAPSHOT_FIELD_LIMIT
            ):
                raise ValueError("伤停审计字段超过有界限制")
            receipts.append(
                dict(
                    base,
                    provider_fixture_id=fixture_id,
                    record_key=f"{capture_key}:fixture:{fixture_id}:chunk:{number}",
                    outcome="snapshot",
                    reason="",
                    detail=detail,
                )
            )
    return receipts


def ingest_injuries(database, raw_file, *, league_id, season, daily=False, raw_location=None):
    """Replay one verified league/season page; retain same-day conflicts in its receipt."""
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
        provider_fixture_id="",
        endpoint="injuries",
        league=league,
        season=season,
        **metadata,
    )
    with transaction(database, AUDIT_NAME) as (session, audit):
        for receipt in read_audit(audit):
            if (
                receipt["record_key"] == capture_key
                and receipt["raw_sha256"] == metadata["raw_sha256"]
                and receipt["outcome"] == "ingested"
            ):
                return dict(inserted=0, rejected=0, conflicts=0, unlinked=0, duplicate=True)
        rejected = []
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
            write_audit(
                audit,
                [
                    dict(
                        base,
                        record_key=capture_key,
                        outcome="rejected",
                        reason="invalid_page",
                        detail=str(exc),
                    )
                ],
            )
            return dict(inserted=0, rejected=1, conflicts=0, unlinked=0, invalid=True)

        fixtures = defaultdict(lambda: dict(entries=[], rejected=[], conflicts=[], unresolved=[]))
        receipts, inserted, conflicts, unlinked = [], 0, 0, 0
        page_errors = []
        for item in rejected:
            if item["provider_fixture_id"]:
                fixtures[item["provider_fixture_id"]]["rejected"].append(item)
            else:
                page_errors.append(item)
            receipts.append(
                dict(
                    base,
                    provider_fixture_id=item["provider_fixture_id"] or "",
                    record_key=f"{capture_key}:row:{item['row_number']}",
                    outcome="rejected",
                    reason="parse_rejected",
                    detail=item,
                )
            )
        for number, injury in enumerate(injuries, 1):
            identity, link_reason = _link(session, injury, league)
            reasons = [link_reason] if link_reason else []
            if injury.player_id is None:
                reasons.append("player_id_missing")
            if injury.injury_type not in INJURY_TYPES:
                reasons.append("unknown_injury_type")
            fields = asdict(injury)
            fields.pop("provider_league_id")
            fields.update(
                provider="apif", endpoint="injuries", league=league, **identity, **metadata
            )
            key = _record_key(injury)
            scope = [
                MatchInjury.provider == "apif",
                MatchInjury.provider_fixture_id == injury.provider_fixture_id,
                MatchInjury.player_id == injury.player_id,
            ]
            previous = session.scalar(
                select(MatchInjury).where(
                    *scope,
                    MatchInjury.injury_type == injury.injury_type,
                    MatchInjury.captured_day == injury.captured_day,
                )
            )
            conflict = previous is not None and (
                previous.raw_value != injury.raw_value
                or any(getattr(previous, name) != value for name, value in identity.items())
            )
            snapshot = fixtures[injury.provider_fixture_id]
            if previous is None:
                version = (
                    session.scalar(select(func.max(MatchInjury.version)).where(*scope)) or 0
                ) + 1
                previous = MatchInjury(**fields, version=version)
                session.add(previous)
                session.flush()
                inserted += 1
            snapshot["entries"].append(
                dict(
                    row_id=previous.id,
                    record_key=key,
                    raw_sha256=previous.raw_sha256,
                    version=previous.version,
                    outcome="conflict" if conflict else "observed",
                )
            )
            if conflict:
                conflicts += 1
                detail = dict(
                    record_key=key,
                    preserved_row_id=previous.id,
                    preserved_raw_sha256=previous.raw_sha256,
                    preserved_raw_location=previous.raw_location,
                    observed=injury.raw_value,
                )
                snapshot["conflicts"].append(detail)
                receipts.append(
                    dict(
                        base,
                        provider_fixture_id=injury.provider_fixture_id,
                        record_key=f"{capture_key}:entry:{number}",
                        outcome="conflict",
                        reason="same_day_content_conflict",
                        detail=detail,
                    )
                )
            if reasons:
                unlinked += 1
                detail = dict(record_key=key, row_id=previous.id, reasons=reasons)
                snapshot["unresolved"].append(detail)
                receipts.append(
                    dict(
                        base,
                        provider_fixture_id=injury.provider_fixture_id,
                        record_key=f"{capture_key}:entry:{number}",
                        outcome="unresolved",
                        reason=";".join(reasons),
                        detail=detail,
                    )
                )

        total_rejected = len(rejected) + conflicts + unlinked
        snapshots = _snapshot_receipts(base, capture_key, fixtures)
        receipts.extend(snapshots)
        detail = dict(
            receipt_version=RECEIPT_VERSION,
            provider="apif",
            provider_league_id=league_id,
            provider_season=season,
            daily=bool(daily),
            captured_utc=metadata["captured_utc"],
            raw_sha256=metadata["raw_sha256"],
            raw_location=metadata["raw_location"],
            empty=not injuries and not rejected,
            parsed_rows=len(injuries),
            inserted=inserted,
            rejected=total_rejected,
            page_errors=len(page_errors),
            fixture_count=len(fixtures),
            snapshot_chunks=len(snapshots),
        )
        receipts.append(
            dict(base, record_key=capture_key, outcome="ingested", reason="", detail=detail)
        )
        write_audit(audit, receipts)
    return dict(
        inserted=inserted,
        rejected=total_rejected,
        conflicts=conflicts,
        unlinked=unlinked,
        empty=detail["empty"],
        duplicate=False,
    )
