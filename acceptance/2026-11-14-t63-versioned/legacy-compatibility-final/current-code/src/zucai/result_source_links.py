"""Optional, immutable API identity snapshots for result-source selection."""

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import inspect, or_, select, text
from sqlalchemy.orm import Session, aliased

from zucai.models import Match, ResultSourceLink, Team

RESULT_LINK_FILE = "state/result_source_links.jsonl"
RESULT_LINK_COLUMNS = tuple(column.name for column in ResultSourceLink.__table__.columns)
_SNAPSHOT_COLUMNS = tuple(key for key in RESULT_LINK_COLUMNS if key != "verified_utc")
_MATCH_COLUMNS = ("match_id", "source", "league", "home_team_id", "away_team_id", "kickoff_utc")
_COUNTERPART_SOURCES = {"jc", "fd", "fdorg"}
_ALLOWED_TARGETS = {"apif": _COUNTERPART_SOURCES, "fdorg": {"jc", "fd"}}
_RESERVED_PREFIXES = ("apif:", "fdorg:", "fd:", "jc:")
_WINDOW = timedelta(hours=3)


def _utc(value):
    instant = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(instant, datetime) or instant.utcoffset() is None:
        raise ValueError("赛果关联时间必须包含明确时区")
    return instant.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _source(match_id):
    if not isinstance(match_id, str) or ":" not in match_id:
        raise ValueError("赛果关联比赛 ID 无效")
    source, identifier = match_id.split(":", 1)
    if not identifier.strip():
        raise ValueError("赛果关联比赛 ID 无效")
    return source


def _canonical_team(name):
    return (
        isinstance(name, str)
        and bool(name.strip())
        and not name.strip().casefold().startswith(_RESERVED_PREFIXES)
    )


def _model_row(model, columns):
    return {key: getattr(model, key) for key in columns}


def _validate_row(row, matches, teams):
    """Validate the saved proof, allowing subsequent changes to current matches."""
    if set(row) != set(RESULT_LINK_COLUMNS):
        raise ValueError("赛果关联行字段无效")
    provider, counterpart = row["provider_match_id"], row["counterpart_id"]
    if _source(counterpart) not in _ALLOWED_TARGETS.get(_source(provider), set()):
        raise ValueError("赛果关联来源方向无效")
    for key in (provider, counterpart):
        if key not in matches or matches[key]["source"] != _source(key):
            raise ValueError("赛果关联比赛引用或来源无效")
    if not isinstance(row["league"], str) or not row["league"].strip():
        raise ValueError("赛果关联联赛无效")
    for key in ("home_team_id", "away_team_id"):
        team_id = row[key]
        if type(team_id) is not int or team_id not in teams or not _canonical_team(teams[team_id]):
            raise ValueError("赛果关联需要已存在的规范球队身份")
    if row["home_team_id"] == row["away_team_id"]:
        raise ValueError("赛果关联主客身份相同")
    for key in ("verified_utc", "provider_kickoff_utc", "counterpart_kickoff_utc"):
        if not isinstance(row[key], str) or _utc(row[key]) != row[key]:
            raise ValueError("赛果关联时间必须为规范 UTC 字符串")
    difference = datetime.fromisoformat(row["provider_kickoff_utc"]) - datetime.fromisoformat(
        row["counterpart_kickoff_utc"]
    )
    if abs(difference) > _WINDOW:
        raise ValueError("赛果关联开球快照超过三小时")


def _validate_claims(pairs):
    parents, identities = {}, {}

    def root(match_id):
        if match_id not in parents:
            parents[match_id] = match_id
            identities[match_id] = {_source(match_id): match_id}
        while parents[match_id] != match_id:
            parents[match_id] = parents[parents[match_id]]
            match_id = parents[match_id]
        return match_id

    for provider, counterpart in pairs:
        source, target_source = _source(provider), _source(counterpart)
        if target_source not in _ALLOWED_TARGETS.get(source, set()):
            raise ValueError("赛果关联来源方向无效")
        left, right = root(provider), root(counterpart)
        if left == right:
            continue
        if identities[left].keys() & identities[right].keys():
            raise ValueError("赛果关联组件存在同来源的多个比赛")
        parents[right] = left
        identities[left].update(identities.pop(right))


def _connected_pairs(session, provider_id, counterpart_id):
    """Read both endpoint components, including paths through other API providers."""
    return list(
        session.execute(
            text("""
        WITH RECURSIVE linked_nodes(match_id) AS (
            SELECT :provider_id
            UNION SELECT :counterpart_id
            UNION SELECT r.counterpart_id FROM result_source_links r
                  JOIN linked_nodes n ON r.provider_match_id=n.match_id
            UNION SELECT r.provider_match_id FROM result_source_links r
                  JOIN linked_nodes n ON r.counterpart_id=n.match_id
        )
        SELECT DISTINCT r.provider_match_id,r.counterpart_id FROM result_source_links r
        JOIN linked_nodes n ON r.provider_match_id=n.match_id
    """),
            {"provider_id": provider_id, "counterpart_id": counterpart_id},
        )
    )


def _validate_rows(rows, matches, teams):
    _validate_claims((row["provider_match_id"], row["counterpart_id"]) for row in rows)
    snapshots, keys = set(), set()
    for row in rows:
        _validate_row(row, matches, teams)
        snapshot = tuple(row[key] for key in _SNAPSHOT_COLUMNS)
        primary_key = tuple(row[key] for key in RESULT_LINK_COLUMNS[:3])
        if snapshot in snapshots or primary_key in keys:
            raise ValueError("赛果关联快照或核验主键重复")
        snapshots.add(snapshot)
        keys.add(primary_key)


def record_result_link(
    session: Session, provider: Match, target: Match, verified_utc: datetime
) -> bool:
    """Append a verified identity snapshot, leaving repeated observations unchanged."""
    verified = _utc(verified_utc)
    if not isinstance(provider, Match) or not isinstance(target, Match):
        raise ValueError("赛果关联需要两侧已存在的比赛")
    if target.source not in _ALLOWED_TARGETS.get(provider.source, set()):
        raise ValueError("赛果关联来源方向无效")
    session.flush()
    matches = {
        row.match_id: dict(row._mapping)
        for row in session.execute(
            select(*(getattr(Match, key) for key in _MATCH_COLUMNS)).where(
                Match.match_id.in_([provider.match_id, target.match_id])
            )
        )
    }
    for match in (provider, target):
        current = matches.get(match.match_id)
        observed = _model_row(match, _MATCH_COLUMNS)
        observed["kickoff_utc"] = _utc(observed["kickoff_utc"])
        if current != observed:
            raise ValueError("赛果关联观察与当前已存比赛快照不符")
    if any(
        getattr(provider, key) != getattr(target, key)
        for key in ("league", "home_team_id", "away_team_id")
    ):
        raise ValueError("赛果关联主客身份或联赛不符")
    teams = dict(
        session.execute(
            select(Team.id, Team.name).where(
                Team.id.in_([provider.home_team_id, provider.away_team_id])
            )
        ).all()
    )
    row = dict(
        provider_match_id=provider.match_id,
        counterpart_id=target.match_id,
        verified_utc=verified,
        league=provider.league,
        home_team_id=provider.home_team_id,
        away_team_id=provider.away_team_id,
        provider_kickoff_utc=matches[provider.match_id]["kickoff_utc"],
        counterpart_kickoff_utc=matches[target.match_id]["kickoff_utc"],
    )
    _validate_row(row, matches, teams)
    ResultSourceLink.__table__.create(session.connection(), checkfirst=True)
    existing = list(
        session.scalars(
            select(ResultSourceLink).where(
                or_(
                    ResultSourceLink.provider_match_id == provider.match_id,
                    ResultSourceLink.counterpart_id == target.match_id,
                )
            )
        )
    )
    _validate_claims(
        [
            *_connected_pairs(session, provider.match_id, target.match_id),
            (provider.match_id, target.match_id),
        ]
    )
    for previous in existing:
        if all(getattr(previous, key) == row[key] for key in _SNAPSHOT_COLUMNS):
            return False
        if (
            previous.provider_match_id == provider.match_id
            and previous.counterpart_id == target.match_id
            and previous.verified_utc == verified
        ):
            raise ValueError("赛果关联同一核验时刻禁止覆盖不同快照")
    session.add(ResultSourceLink(**row))
    session.flush()
    return True


def load_result_links(session: Session) -> list[tuple[str, str, str]]:
    """Read only evidence that still exactly describes both current match identities."""
    if not inspect(session.connection()).has_table("result_source_links"):
        return []
    _validate_claims(
        session.execute(select(ResultSourceLink.provider_match_id, ResultSourceLink.counterpart_id))
    )
    provider, counterpart = aliased(Match), aliased(Match)
    home, away = aliased(Team), aliased(Team)
    query = (
        select(ResultSourceLink, home.name, away.name, provider.source, counterpart.source)
        .join(provider, provider.match_id == ResultSourceLink.provider_match_id)
        .join(counterpart, counterpart.match_id == ResultSourceLink.counterpart_id)
        .join(home, home.id == ResultSourceLink.home_team_id)
        .join(away, away.id == ResultSourceLink.away_team_id)
        .where(
            provider.source.in_(_ALLOWED_TARGETS),
            counterpart.source.in_(_COUNTERPART_SOURCES),
            provider.kickoff_utc == ResultSourceLink.provider_kickoff_utc,
            counterpart.kickoff_utc == ResultSourceLink.counterpart_kickoff_utc,
            *(
                getattr(match, key) == getattr(ResultSourceLink, key)
                for match in (provider, counterpart)
                for key in ("league", "home_team_id", "away_team_id")
            ),
        )
        .order_by(
            ResultSourceLink.provider_match_id,
            ResultSourceLink.counterpart_id,
            ResultSourceLink.verified_utc,
        )
    )
    result = []
    for link, home_name, away_name, provider_source, counterpart_source in session.execute(query):
        if not _canonical_team(home_name) or not _canonical_team(away_name):
            continue
        if (
            _source(link.provider_match_id) != provider_source
            or _source(link.counterpart_id) != counterpart_source
            or counterpart_source not in _ALLOWED_TARGETS[provider_source]
        ):
            continue
        if _utc(link.verified_utc) != link.verified_utc:
            raise ValueError("赛果关联核验时间无效")
        result.append((link.provider_match_id, link.counterpart_id, link.verified_utc))
    return result


def _stored_rows(db):
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='result_source_links'"
    ).fetchone():
        return []
    names = ",".join(RESULT_LINK_COLUMNS)
    return [
        dict(zip(RESULT_LINK_COLUMNS, row, strict=True))
        for row in db.execute(
            f"SELECT {names} FROM result_source_links "
            "ORDER BY provider_match_id,counterpart_id,verified_utc"
        )
    ]


def _archive_context(db):
    matches = {
        row[0]: dict(zip(_MATCH_COLUMNS, row, strict=True))
        for row in db.execute(f"SELECT {','.join(_MATCH_COLUMNS)} FROM matches")
    }
    teams = dict(db.execute("SELECT id,name FROM teams"))
    return matches, teams


def export_result_links(db: sqlite3.Connection, archive: Path) -> dict | None:
    """Export nonempty valid snapshots; old databases need no optional descriptor."""
    rows = _stored_rows(db)
    if not rows:
        return None
    _validate_rows(rows, *_archive_context(db))
    target = archive / RESULT_LINK_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(
                json.dumps(
                    [row[key] for key in RESULT_LINK_COLUMNS],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
    return {"version": 1, "columns": list(RESULT_LINK_COLUMNS), "count": len(rows)}


def validate_result_links_manifest(meta) -> bool:
    """Require the optional descriptor and its hash-listed file together."""
    if not isinstance(meta, dict) or not isinstance(meta.get("files"), dict):
        raise ValueError("赛果关联 manifest 文件清单无效")
    block = meta.get("result_source_links")
    listed = RESULT_LINK_FILE in meta["files"]
    if "result_source_links" not in meta and not listed:
        return False
    if (
        not listed
        or not isinstance(block, dict)
        or set(block) != {"version", "columns", "count"}
        or type(block.get("version")) is not int
        or block["version"] != 1
        or block.get("columns") != list(RESULT_LINK_COLUMNS)
        or type(block.get("count")) is not int
        or block["count"] < 1
        or not isinstance(meta["files"][RESULT_LINK_FILE], str)
        or not re.fullmatch(r"[0-9a-f]{64}", meta["files"][RESULT_LINK_FILE])
    ):
        raise ValueError("赛果关联 manifest 字段、文件、哈希或行数无效")
    return True


def restore_result_links(db: sqlite3.Connection, archive: Path, meta) -> None:
    """Validate the entire proof before insertion; the caller owns DB replacement."""
    if not validate_result_links_manifest(meta):
        return
    target = archive / RESULT_LINK_FILE
    if (
        target.is_symlink()
        or not target.resolve().is_relative_to(archive.resolve())
        or not target.is_file()
    ):
        raise ValueError("赛果关联归档文件缺失或路径无效")
    raw = target.read_bytes()
    if hashlib.sha256(raw).hexdigest() != meta["files"][RESULT_LINK_FILE]:
        raise ValueError("赛果关联归档哈希校验失败")
    try:
        values = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
    except (ValueError, UnicodeError) as exc:
        raise ValueError("赛果关联行 JSON 无效") from exc
    if any(not isinstance(row, list) or len(row) != len(RESULT_LINK_COLUMNS) for row in values):
        raise ValueError("赛果关联行字段无效")
    if len(values) != meta["result_source_links"]["count"]:
        raise ValueError("赛果关联行数校验失败")
    rows = [dict(zip(RESULT_LINK_COLUMNS, row, strict=True)) for row in values]
    _validate_rows([*_stored_rows(db), *rows], *_archive_context(db))
    names = ",".join(RESULT_LINK_COLUMNS)
    placeholders = ",".join("?" for _ in RESULT_LINK_COLUMNS)
    db.execute("SAVEPOINT restore_result_links")
    try:
        db.executemany(f"INSERT INTO result_source_links ({names}) VALUES ({placeholders})", values)
    except (sqlite3.DatabaseError, TypeError) as exc:
        db.execute("ROLLBACK TO restore_result_links")
        raise ValueError("赛果关联行类型、引用或唯一性无效") from exc
    finally:
        db.execute("RELEASE restore_result_links")
