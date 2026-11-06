"""Replay archived technical data into attached tables, with atomic audit receipts."""

import csv
import hashlib
import io
import json
from collections import Counter
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from sqlalchemy import select, text

from zucai.db import get_engine, session_scope
from zucai.fd_stats_links import (
    FD_LINK_COLUMNS,
    _model_dict,
    _session_context,
    _validate_link,
    canonical_fd_sides,
    fd_link_authorizes,
    resolve_fd_match,
)
from zucai.models import ApifFixtureIndex, FdStatsLink, Match, MatchEvent, MatchLineup, MatchStat
from zucai.sources.apif_stats import envelope, fixture_identity, instant, parse_endpoint
from zucai.sources.base import _atomic_write
from zucai.sources.fd_ingest import fd_match_id
from zucai.sources.football_data import (
    ALIASES_PATH,
    LEAGUES_PATH,
    FootballDataCoUk,
    csv_url,
    read_json,
)
from zucai.sources.teams import normalize_team
from zucai.stats_schema import migrate_stats

AUDIT_FIELDS = (
    "provider_fixture_id",
    "endpoint",
    "record_key",
    "league",
    "season",
    "outcome",
    "reason",
    "detail",
    "candidates",
    "captured_utc",
    "raw_sha256",
    "raw_location",
)
AUDIT_KEY = ("provider_fixture_id", "endpoint", "record_key", "raw_sha256", "outcome", "reason")
MODELS = {
    "csv": MatchStat,
    "statistics": MatchStat,
    "events": MatchEvent,
    "lineups": MatchLineup,
    "players": MatchLineup,
}


def load_raw(path, *, endpoint, parameters, location=None):
    path = Path(path)
    raw = path.read_bytes()
    meta = json.loads(path.with_suffix(path.suffix + ".meta.json").read_bytes())
    sha = hashlib.sha256(raw).hexdigest()
    if sha != meta.get("sha256"):
        raise ValueError("技术数据原件哈希与 sidecar 不符")
    if meta.get("status_code") != 200:
        raise ValueError("技术数据原件须为 HTTP 200；错误原件保留供审计")
    captured = instant(meta.get("fetched_at_utc"))
    url = urlparse(meta.get("url") or "")
    if url.scheme != "https" or url.username or url.password or url.fragment:
        raise ValueError("技术数据原件 URL 无效")
    if endpoint == "csv":
        expected = urlparse(csv_url(div=parameters["div"], season=parameters["season"]))
        if (
            url.netloc not in {"www.football-data.co.uk", "football-data.co.uk"}
            or url.path != expected.path
            or url.query
        ):
            raise ValueError("fd 原件路径与联赛季不符")
    elif (
        url.netloc != "v3.football.api-sports.io"
        or url.path != "/" + endpoint
        or parse_qs(url.query) != {k: [str(v)] for k, v in parameters.items()}
    ):
        raise ValueError("apif 原件路径或请求范围不符")
    return raw, dict(
        captured_utc=captured.isoformat(), raw_sha256=sha, raw_location=location or path.as_posix()
    )


def read_audit(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        if reader.fieldnames != list(AUDIT_FIELDS):
            raise ValueError("技术数据审计列头不符")
        rows = list(reader)
    if any(None in row or any(value is None for value in row.values()) for row in rows):
        raise ValueError("技术数据审计行不完整")
    return rows


def write_audit(path, rows):
    previous = read_audit(path)
    seen = {tuple(row[key] for key in AUDIT_KEY) for row in previous}
    records = previous.copy()
    for row in rows:
        normalized = {}
        for key in AUDIT_FIELDS:
            value = row.get(key, "")
            normalized[key] = (
                json.dumps(value, ensure_ascii=False, sort_keys=True)
                if isinstance(value, (dict, list))
                else str(value)
            )
        key = tuple(normalized[field] for field in AUDIT_KEY)
        if key not in seen:
            seen.add(key)
            records.append(normalized)
    if records == previous:
        return
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, AUDIT_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(records)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, stream.getvalue().encode())


@contextmanager
def transaction(database, audit_name):
    """Match reconcile's DB/CSV rollback boundary, including commit-time failures."""
    database = Path(database)
    if not database.is_file():
        raise ValueError("技术数据需要已有数据库")
    audit = database.parent / "state/audit" / audit_name
    previous = audit.read_bytes() if audit.exists() else None
    engine = get_engine(database)
    try:
        migrate_stats(engine)
        with session_scope(engine) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            # Snapshot after acquiring the database lock, so concurrent replays serialize.
            previous = audit.read_bytes() if audit.exists() else None
            yield session, audit
            session.flush()
    except BaseException:
        if previous is None:
            audit.unlink(missing_ok=True)
        elif not audit.exists() or audit.read_bytes() != previous:
            audit.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(audit, previous)
        raise
    finally:
        engine.dispose()


def _preserve(new, old):
    if new is None or new == "" or new == [] or new == {}:
        return old
    if isinstance(new, dict) and isinstance(old, dict):
        if new.get("id") is not None and new.get("id") != old.get("id"):
            return new  # A changed coach/player/assist is a different entity, not missing fields.
        return {key: _preserve(new.get(key), old.get(key)) for key in old.keys() | new.keys()}
    if isinstance(new, list) and isinstance(old, list) and len(new) == len(old):
        return [_preserve(n, o) for n, o in zip(new, old, strict=True)]
    return new


def observation_clocks(audit):
    """Unchanged valid observations advance freshness without inventing a new value version."""
    clocks = {}
    for receipt in read_audit(audit):
        if receipt["outcome"] != "ingested":
            continue
        detail = json.loads(receipt["detail"])
        for key in detail.get("valid_keys", []):
            identity = (receipt["provider_fixture_id"], receipt["endpoint"], key)
            stamp = instant(receipt["captured_utc"])
            clocks[identity] = max(clocks.get(identity, stamp), stamp)
    return clocks


def _receipt_detail(rows, inserted, rejected=0):
    return dict(
        inserted=inserted,
        empty=not rows,
        rejected=rejected,
        record_keys=[row["record_key"] for row in rows],
        valid_keys=[row["record_key"] for row in rows if row["value"] is not None],
    )


def store_rows(session, rows, metadata, *, clocks=None, daily=False):
    """Append corrections per logical record; never mutate an earlier observation."""
    inserted = 0
    validated_links = {}
    keys = [(r["provider"], r["provider_fixture_id"], r["endpoint"], r["record_key"]) for r in rows]
    if len(set(keys)) != len(keys):
        raise ValueError("技术数据逻辑行键重复")
    for row in rows:
        observed = (clocks or {}).get(
            (row["provider_fixture_id"], row["endpoint"], row["record_key"])
        )
        if observed is not None and instant(metadata["captured_utc"]) < observed:
            continue
        fields = dict(row, **metadata, available_utc=None, availability_basis="backfilled")
        model = MODELS[row["endpoint"]]
        match = session.get(Match, row["match_id"])
        if not match or row["team_id"] != getattr(match, row["side"] + "_team_id"):
            raise ValueError("技术数据主客身份与比赛不符")
        if (
            daily
            and match.status == "finished"
            and instant(metadata["captured_utc"]) > instant(match.kickoff_utc) + timedelta(hours=2)
        ):
            fields.update(availability_basis="captured", available_utc=metadata["captured_utc"])
        if row["provider"] == "apif":
            index = session.get(ApifFixtureIndex, row["provider_fixture_id"])
            if (
                not index
                or index.match_id != match.match_id
                or row["provider_team_id"] != getattr(index, row["side"] + "_provider_id")
            ):
                raise ValueError("技术数据缺少一致的 fixture 索引")
        else:
            legacy = (match.source == "fd" and row["provider_fixture_id"] == match.match_id) or (
                match.source == "jc" and match.counterpart_id == row["provider_fixture_id"]
            )
            link = session.get(FdStatsLink, row["provider_fixture_id"])
            if link is not None:
                if link.provider_fixture_id not in validated_links:
                    teams, matches = _session_context(session)
                    validated_links[link.provider_fixture_id] = _validate_link(
                        _model_dict(link, FD_LINK_COLUMNS), teams, matches
                    )
                if not fd_link_authorizes(
                    validated_links[link.provider_fixture_id], match_id=match.match_id, **metadata
                ):
                    raise ValueError("fd 技术数据缺少一致的原件关联观察")
            elif not legacy:
                raise ValueError("fd 技术数据须附着既有 fd 行、jc counterpart 或一致统计关联")
        scope = [
            getattr(model, key) == row[key]
            for key in ("provider", "provider_fixture_id", "endpoint", "record_key")
        ]
        if session.scalar(
            select(model.id).where(*scope, model.raw_sha256 == metadata["raw_sha256"]).limit(1)
        ):
            continue
        old = session.scalar(select(model).where(*scope).order_by(model.version.desc()).limit(1))
        if old is not None:
            if instant(metadata["captured_utc"]) < instant(old.captured_utc):
                continue  # An older previously unseen snapshot cannot supersede a later capture.
            same_entity = row["team_id"] == old.team_id
            if model is MatchEvent:
                same_entity = same_entity and all(
                    row.get(key) == getattr(old, key)
                    for key in ("event_type", "elapsed", "extra", "player_id")
                )
            value = _preserve(row["value"], old.value) if same_entity else row["value"]
            if value == old.value and all(
                row.get(k) == getattr(old, k) for k in ("period_basis", "team_id")
            ):
                continue
            if value != row["value"]:
                fields["missing_reason"] = "partial_null_retained"
                # Retained historical values are not observations in this response.
                fields.update(availability_basis="backfilled", available_utc=None)
            fields["value"] = value
            if model is not MatchStat:
                for key in (
                    "formation",
                    "minutes",
                    "rating",
                    "position",
                    "event_type",
                    "detail",
                    "elapsed",
                    "extra",
                    "player_id",
                    "assist_id",
                ):
                    if key in fields and fields[key] is None and same_entity:
                        fields[key] = getattr(old, key)
        fields["version"] = 1 if old is None else old.version + 1
        session.add(model(**fields))
        inserted += 1
    session.flush()
    return inserted


def link_archive(database, raw_file, *, league_id, season, aliases_path, raw_location=None):
    from zucai.stats_link import link_fixtures

    raw, meta = load_raw(
        raw_file,
        endpoint="fixtures",
        parameters={"league": league_id, "season": season},
        location=raw_location,
    )
    with transaction(database, "stats_link_audit.csv") as (session, audit):
        result = link_fixtures(
            session,
            raw,
            league_id=league_id,
            season=season,
            aliases=read_json(aliases_path),
            metadata=meta,
        )
        write_audit(audit, [dict(row, endpoint="fixtures") for row in result["audit"]])
    return {k: v for k, v in result.items() if k != "audit"}


def _record_duplicate(audit, provider_fixture_id, endpoint, metadata):
    previous = [
        row
        for row in read_audit(audit)
        if row["provider_fixture_id"] == str(provider_fixture_id)
        and row["endpoint"] == endpoint
        and row["raw_sha256"] == metadata["raw_sha256"]
        and row["outcome"] == "ingested"
    ]
    if not previous:
        return False
    latest = max(previous, key=lambda row: instant(row["captured_utc"]))
    if instant(metadata["captured_utc"]) > instant(latest["captured_utc"]):
        # A fresh capture of identical bytes is no new data row, but is new clock evidence.
        # Replaying that same capture again leaves both database and audit bytes unchanged.
        write_audit(
            audit, [dict(latest, **metadata, record_key=f"capture:{metadata['captured_utc']}")]
        )
    return True


def ingest_apif(database, raw_file, *, fixture_file=None, endpoint, raw_location=None, daily=False):
    if endpoint not in {"statistics", "events", "lineups", "players"}:
        raise ValueError("不支持的技术端点")
    # Read the sidecar's requested ID, then independently verify URL, envelope and index.
    sidecar = json.loads(
        Path(raw_file).with_suffix(Path(raw_file).suffix + ".meta.json").read_bytes()
    )
    query = parse_qs(urlparse(sidecar.get("url") or "").query)
    values = query.get("fixture", [])
    if len(values) != 1 or not values[0].isdecimal() or int(values[0]) <= 0:
        raise ValueError("端点原件缺少 fixture ID")
    fixture_id = values[0]
    raw, meta = load_raw(
        raw_file,
        endpoint=f"fixtures/{endpoint}",
        parameters={"fixture": fixture_id},
        location=raw_location,
    )
    if fixture_file is None:
        # T32 retains the original fixture/league/teams observation in the verified
        # index; no synthetic fixture response or additional API request is needed.
        engine = get_engine(database)
        try:
            with session_scope(engine) as session:
                index = session.get(ApifFixtureIndex, fixture_id)
                context = index.evidence.get("fixture") if index else None
                if not isinstance(context, dict):
                    raise ValueError("索引缺少已归档的 fixture 上下文")
                scope = {"league": str(index.provider_league_id), "season": str(index.season)}
                found = [context]
        finally:
            engine.dispose()
    else:
        fixture_meta = json.loads(
            Path(fixture_file).with_suffix(Path(fixture_file).suffix + ".meta.json").read_bytes()
        )
        scope = parse_qs(urlparse(fixture_meta.get("url") or "").query)
        if set(scope) != {"league", "season"} or any(
            len(v) != 1 or not v[0].isdecimal() for v in scope.values()
        ):
            raise ValueError("fixture 原件范围须为 league + season")
        scope = {k: v[0] for k, v in scope.items()}
        fixture_raw, _ = load_raw(fixture_file, endpoint="fixtures", parameters=scope)
        fixtures = envelope(fixture_raw, "fixtures", scope)
        found = [row for row in fixtures if str(row.get("fixture", {}).get("id")) == fixture_id]
    if len(found) != 1:
        raise ValueError("fixture 原件未唯一包含指定比赛")
    identity = fixture_identity(found[0])
    if identity["provider_league_id"] != int(scope["league"]) or identity["season"] != int(
        scope["season"]
    ):
        raise ValueError("fixture 的联赛季不一致")
    audit_row = dict(
        provider_fixture_id=fixture_id, endpoint=endpoint, season=identity["season"], **meta
    )
    with transaction(database, "stats_ingest_audit.csv") as (session, audit):
        if _record_duplicate(audit, fixture_id, endpoint, meta):
            return dict(inserted=0, rejected=0, duplicate=True)
        index = session.get(ApifFixtureIndex, fixture_id)
        if (
            not index
            or any(
                getattr(index, key) != identity[key]
                for key in ("provider_league_id", "season", "home_provider_id", "away_provider_id")
            )
            or abs(instant(index.kickoff_utc) - identity["kickoff_utc"]) > timedelta(hours=3)
        ):
            write_audit(
                audit,
                [
                    dict(
                        audit_row,
                        outcome="rejected",
                        reason="候选不全",
                        detail="缺少一致的关联索引",
                    )
                ],
            )
            return dict(inserted=0, rejected=1)
        audit_row["league"] = index.league
        rejected = []
        try:
            rows = parse_endpoint(
                raw,
                endpoint=endpoint,
                fixture=found[0],
                captured_utc=meta["captured_utc"],
                rejected=rejected,
            )
        except ValueError as exc:
            write_audit(
                audit, [dict(audit_row, outcome="rejected", reason="响应无效", detail=str(exc))]
            )
            return dict(inserted=0, rejected=1)
        for row in rows:
            row.update(match_id=index.match_id, team_id=getattr(index, row["side"] + "_team_id"))
        inserted = store_rows(session, rows, meta, clocks=observation_clocks(audit), daily=daily)
        receipts = [
            dict(
                audit_row,
                outcome="rejected",
                reason="解析拒绝",
                record_key=str(r["row_number"]),
                detail=r["reason"],
            )
            for r in rejected
        ]
        receipts.append(
            dict(
                audit_row,
                outcome="ingested",
                reason="",
                detail=_receipt_detail(rows, inserted, len(rejected)),
            )
        )
        write_audit(audit, receipts)
    return dict(inserted=inserted, rejected=len(rejected), empty=not rows)


def ingest_fd(
    database,
    raw_file,
    *,
    div,
    season,
    leagues_path=None,
    raw_location=None,
    daily=False,
    aliases_path=None,
):
    raw, meta = load_raw(
        raw_file, endpoint="csv", parameters={"div": div, "season": season}, location=raw_location
    )
    leagues = read_json(leagues_path or LEAGUES_PATH)
    adapter = FootballDataCoUk(leagues=leagues, div_code=div, season=season)
    games = adapter.parse_statistics(raw)
    league = leagues[div]["league"]
    aliases = read_json(aliases_path or ALIASES_PATH)
    rejected, inserted, receipts = len(adapter.rejected), 0, []
    base = dict(endpoint="csv", league=league, season=season, **meta)
    for row in adapter.rejected:
        receipts.append(
            dict(
                base,
                provider_fixture_id=f"fd:{div}:{season}:row:{row['row_number']}",
                outcome="rejected",
                reason="解析拒绝",
                detail=row["reason"],
            )
        )
    ids = Counter(fd_match_id(game) for game in games)
    with transaction(database, "stats_ingest_audit.csv") as (session, audit):
        clocks = observation_clocks(audit)
        # Plan the whole raw response before writing any proof. Competing aliases,
        # reversed pairs and shared-team games reject all peers, not only the last one.
        plans, failures, incoming = {}, {}, {}
        context = _session_context(session)
        for position, game in enumerate(games):
            try:
                incoming[position] = canonical_fd_sides(
                    context[0], aliases, league, game.home_raw, game.away_raw
                )
            except ValueError:
                pass  # Legacy pointers need no alias; unresolved names cannot grant identity.
            try:
                if ids[fd_match_id(game)] != 1:
                    raise ValueError("同一原件 provider fixture ID 重复")
                plans[position] = resolve_fd_match(
                    session, game, league, meta, aliases, persist=False, context=context
                )
                incoming[position] = (plans[position].home_team_id, plans[position].away_team_id)
            except ValueError as exc:
                failures[position] = str(exc)
        raw_sides = [
            {normalize_team(g.home_raw).casefold(), normalize_team(g.away_raw).casefold()}
            for g in games
        ]
        for position, game in enumerate(games):
            for other_position in range(position):
                if (
                    abs(game.kickoff_utc - games[other_position].kickoff_utc) <= timedelta(hours=3)
                    and raw_sides[position] & raw_sides[other_position]
                ):
                    failures[position] = "同窗 FD 原队名冲突"
                    failures[other_position] = "同窗 FD 原队名冲突"
        for position, pair in incoming.items():
            for other_position, other in incoming.items():
                if position == other_position:
                    continue
                if abs(
                    games[position].kickoff_utc - games[other_position].kickoff_utc
                ) <= timedelta(hours=3) and (set(pair) & set(other)):
                    failures[position] = "整批 FD 原件存在多对一、反向或共享球队冲突"
        for position, game in enumerate(games):
            match_id = fd_match_id(game)
            receipt = dict(base, provider_fixture_id=match_id)
            if position in failures:
                receipts.append(
                    dict(receipt, outcome="rejected", reason="候选不全", detail=failures[position])
                )
                rejected += 1
                continue
            # Validate identities even on a duplicate; an old receipt is not authority.
            match = resolve_fd_match(session, game, league, meta, aliases, context=context)
            if _record_duplicate(audit, match_id, "csv", meta):
                continue
            rows = [
                dict(
                    row,
                    provider_fixture_id=match_id,
                    match_id=match.match_id,
                    team_id=getattr(match, row["side"] + "_team_id"),
                )
                for row in game.statistics
            ]
            added = store_rows(session, rows, meta, clocks=clocks, daily=daily)
            inserted += added
            receipts.append(
                dict(receipt, outcome="ingested", reason="", detail=_receipt_detail(rows, added))
            )
        write_audit(audit, receipts)
    return dict(inserted=inserted, rejected=rejected)
