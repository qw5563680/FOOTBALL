"""Timestamped proof of explicitly bound API identities, independent of results."""

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, inspect, or_, select
from sqlalchemy.orm import Session

from zucai.models import Match, ResultSourceIdentity, Team
from zucai.result_source_links import _canonical_team, _source, _utc

RESULT_IDENTITY_FILE = "state/result_source_identities.jsonl"
RESULT_IDENTITY_COLUMNS = tuple(column.name for column in ResultSourceIdentity.__table__.columns)
_SNAPSHOT_COLUMNS = tuple(key for key in RESULT_IDENTITY_COLUMNS if key != "verified_utc")
_MATCH_COLUMNS = tuple(key for key in _SNAPSHOT_COLUMNS if not key.endswith("_provider_binding"))
_SOURCES = {"apif", "fdorg"}


def _row(model, columns):
    return {key: getattr(model, key) for key in columns}


def _binding(team, source, league):
    if not _canonical_team(team["name"]):
        raise ValueError("赛果身份需要非临时规范球队")
    aliases = team["aliases"]
    if not isinstance(aliases, list) or any(not isinstance(alias, str) for alias in aliases):
        raise ValueError("赛果身份球队绑定格式无效")
    prefix = f"{source}:{league}:team:"
    candidates = {alias for alias in aliases if alias.startswith(prefix)}
    if len(candidates) != 1:
        raise ValueError("赛果身份需要唯一的联赛内供应商球队绑定")
    binding = next(iter(candidates))
    if not re.fullmatch(r"[1-9][0-9]*", binding[len(prefix) :]):
        raise ValueError("赛果身份供应商球队 ID 无效")
    return binding


def _owners(teams):
    owners = defaultdict(set)
    for team_id, team in teams.items():
        aliases = team.get("aliases", [])
        if not isinstance(aliases, list):
            continue
        for label in (team["name"], *aliases):
            if isinstance(label, str):
                owners[label.casefold()].add(team_id)
    return owners


def _validate_bindings(row, teams, owners):
    for side in ("home", "away"):
        team_id = row[f"{side}_team_id"]
        binding = _binding(teams[team_id], row["source"], row["league"])
        if row[f"{side}_provider_binding"] != binding:
            raise ValueError("赛果身份供应商绑定已变更，须重新核验")
        if owners[binding.casefold()] != {team_id}:
            raise ValueError("赛果身份供应商绑定存在多个规范球队")


def _validate_row(row, matches, teams):
    if set(row) != set(RESULT_IDENTITY_COLUMNS):
        raise ValueError("赛果身份行字段无效")
    if not isinstance(row["source"], str) or row["source"] not in _SOURCES:
        raise ValueError("赛果身份仅支持 APIF/FDORG")
    if _source(row["match_id"]) != row["source"]:
        raise ValueError("赛果身份来源与比赛 ID 不符")
    match = matches.get(row["match_id"])
    if match is None or match["source"] != row["source"]:
        raise ValueError("赛果身份比赛引用或来源无效")
    if not isinstance(row["league"], str) or not row["league"].strip():
        raise ValueError("赛果身份联赛无效")
    for side in ("home_team_id", "away_team_id"):
        team_id = row[side]
        if (
            type(team_id) is not int
            or team_id not in teams
            or not _canonical_team(teams[team_id]["name"])
        ):
            raise ValueError("赛果身份球队引用或规范身份无效")
    if row["home_team_id"] == row["away_team_id"]:
        raise ValueError("赛果身份主客球队相同")
    prefix = f"{row['source']}:{row['league']}:team:"
    for side in ("home", "away"):
        binding = row[f"{side}_provider_binding"]
        if not isinstance(binding, str) or not re.fullmatch(
            re.escape(prefix) + r"[1-9][0-9]*", binding
        ):
            raise ValueError("赛果身份供应商绑定快照无效")
    if row["home_provider_binding"] == row["away_provider_binding"]:
        raise ValueError("赛果身份主客供应商绑定相同")
    for key in ("kickoff_utc", "verified_utc"):
        if not isinstance(row[key], str) or _utc(row[key]) != row[key]:
            raise ValueError("赛果身份时间须为规范 UTC 字符串")


def _validate_rows(rows, matches, teams):
    snapshots, keys = set(), set()
    for row in rows:
        _validate_row(row, matches, teams)
        snapshot = tuple(row[key] for key in _SNAPSHOT_COLUMNS)
        primary_key = row["match_id"], row["verified_utc"]
        if snapshot in snapshots or primary_key in keys:
            raise ValueError("赛果身份快照或核验主键重复")
        snapshots.add(snapshot)
        keys.add(primary_key)


def record_result_identity(session: Session, match: Match, verified_utc: datetime) -> bool:
    """Observe current explicit bindings; never infer a historical verification time."""
    verified = _utc(verified_utc)
    if not isinstance(match, Match) or match.source not in _SOURCES:
        raise ValueError("赛果身份需要已存在的 API 比赛")
    session.flush()
    current = (
        session.execute(
            select(*(getattr(Match, key) for key in _MATCH_COLUMNS)).where(
                Match.match_id == match.match_id
            )
        )
        .mappings()
        .one_or_none()
    )
    observed = _row(match, _MATCH_COLUMNS)
    observed["kickoff_utc"] = _utc(observed["kickoff_utc"])
    if current is None or dict(current) != observed:
        raise ValueError("赛果身份观察与当前已存比赛不符")
    teams = {
        team.id: _row(team, ("name", "aliases"))
        for team in session.scalars(
            select(Team).where(Team.id.in_([match.home_team_id, match.away_team_id]))
        )
    }
    if set(teams) != {match.home_team_id, match.away_team_id}:
        raise ValueError("赛果身份球队引用无效")
    bindings_by_side = {
        f"{side}_provider_binding": _binding(
            teams[getattr(match, f"{side}_team_id")], match.source, match.league
        )
        for side in ("home", "away")
    }
    row = dict(observed, verified_utc=verified, **bindings_by_side)
    _validate_row(row, {match.match_id: current}, teams)
    bindings = list(bindings_by_side.values())
    aliases = func.json_each(Team.aliases).table_valued("value")
    bound_alias = (
        select(1)
        .select_from(aliases)
        .where(func.lower(aliases.c.value).in_([binding.casefold() for binding in bindings]))
        .exists()
    )
    for team in session.scalars(
        select(Team).where(
            or_(
                bound_alias,
                *(Team.name == binding for binding in bindings),
            )
        )
    ):
        teams[team.id] = _row(team, ("name", "aliases"))
    _validate_bindings(row, teams, _owners(teams))
    ResultSourceIdentity.__table__.create(session.connection(), checkfirst=True)
    for previous in session.scalars(
        select(ResultSourceIdentity).where(ResultSourceIdentity.match_id == match.match_id)
    ):
        if all(getattr(previous, key) == row[key] for key in _SNAPSHOT_COLUMNS):
            return False
        if previous.verified_utc == verified:
            raise ValueError("赛果身份同一核验时刻禁止覆盖不同快照")
    session.add(ResultSourceIdentity(**row))
    session.flush()
    return True


def load_result_identities(session: Session) -> dict[str, str]:
    """Return first verification only for currently matching, still bound identities."""
    if not inspect(session.connection()).has_table("result_source_identities"):
        return {}
    rows = list(
        session.scalars(
            select(ResultSourceIdentity)
            .join(Match, Match.match_id == ResultSourceIdentity.match_id)
            .where(
                *(
                    getattr(Match, key) == getattr(ResultSourceIdentity, key)
                    for key in _MATCH_COLUMNS
                )
            )
            .order_by(ResultSourceIdentity.match_id, ResultSourceIdentity.verified_utc)
        )
    )
    if not rows:
        return {}
    teams = {team.id: _row(team, ("name", "aliases")) for team in session.scalars(select(Team))}
    owners = _owners(teams)
    result = {}
    for model in rows:
        row = _row(model, RESULT_IDENTITY_COLUMNS)
        try:
            _validate_row(row, {model.match_id: row}, teams)
            _validate_bindings(row, teams, owners)
        except ValueError:
            continue
        result.setdefault(model.match_id, model.verified_utc)
    return result


def _stored_rows(db):
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='result_source_identities'"
    ).fetchone():
        return []
    return [
        dict(zip(RESULT_IDENTITY_COLUMNS, row, strict=True))
        for row in db.execute(
            f"SELECT {','.join(RESULT_IDENTITY_COLUMNS)} FROM result_source_identities "
            "ORDER BY match_id,verified_utc"
        )
    ]


def _archive_context(db):
    matches = {
        row[0]: {"source": row[1]} for row in db.execute("SELECT match_id,source FROM matches")
    }
    teams = {row[0]: {"name": row[1]} for row in db.execute("SELECT id,name FROM teams")}
    return matches, teams


def export_result_identities(db: sqlite3.Connection, archive: Path) -> dict | None:
    """Export immutable proof, retaining snapshots that no longer match live identities."""
    rows = _stored_rows(db)
    if not rows:
        return None
    _validate_rows(rows, *_archive_context(db))
    target = archive / RESULT_IDENTITY_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(
                json.dumps(
                    [row[key] for key in RESULT_IDENTITY_COLUMNS],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
    return {"version": 1, "columns": list(RESULT_IDENTITY_COLUMNS), "count": len(rows)}


def validate_result_identities_manifest(meta) -> bool:
    if not isinstance(meta, dict) or not isinstance(meta.get("files"), dict):
        raise ValueError("赛果身份 manifest 文件清单无效")
    block = meta.get("result_source_identities")
    listed = RESULT_IDENTITY_FILE in meta["files"]
    if "result_source_identities" not in meta and not listed:
        return False
    if (
        not listed
        or not isinstance(block, dict)
        or set(block) != {"version", "columns", "count"}
        or type(block.get("version")) is not int
        or block["version"] != 1
        or block.get("columns") != list(RESULT_IDENTITY_COLUMNS)
        or type(block.get("count")) is not int
        or block["count"] < 1
        or not isinstance(meta["files"][RESULT_IDENTITY_FILE], str)
        or not re.fullmatch(r"[0-9a-f]{64}", meta["files"][RESULT_IDENTITY_FILE])
    ):
        raise ValueError("赛果身份 manifest 字段、文件、哈希或行数无效")
    return True


def restore_result_identities(db: sqlite3.Connection, archive: Path, meta) -> None:
    """Validate all rows before insertion; the caller owns atomic database replacement."""
    if not validate_result_identities_manifest(meta):
        return
    target = archive / RESULT_IDENTITY_FILE
    if (
        target.is_symlink()
        or not target.resolve().is_relative_to(archive.resolve())
        or not target.is_file()
    ):
        raise ValueError("赛果身份归档文件缺失或路径无效")
    raw = target.read_bytes()
    if hashlib.sha256(raw).hexdigest() != meta["files"][RESULT_IDENTITY_FILE]:
        raise ValueError("赛果身份归档哈希校验失败")
    try:
        values = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
    except (ValueError, UnicodeError) as exc:
        raise ValueError("赛果身份行 JSON 无效") from exc
    if any(not isinstance(row, list) or len(row) != len(RESULT_IDENTITY_COLUMNS) for row in values):
        raise ValueError("赛果身份行字段无效")
    if len(values) != meta["result_source_identities"]["count"]:
        raise ValueError("赛果身份行数校验失败")
    rows = [dict(zip(RESULT_IDENTITY_COLUMNS, row, strict=True)) for row in values]
    _validate_rows([*_stored_rows(db), *rows], *_archive_context(db))
    names = ",".join(RESULT_IDENTITY_COLUMNS)
    placeholders = ",".join("?" for _ in RESULT_IDENTITY_COLUMNS)
    db.execute("SAVEPOINT restore_result_identities")
    try:
        db.executemany(
            f"INSERT INTO result_source_identities ({names}) VALUES ({placeholders})", values
        )
    except (sqlite3.DatabaseError, TypeError) as exc:
        db.execute("ROLLBACK TO restore_result_identities")
        raise ValueError("赛果身份行类型、引用或唯一性无效") from exc
    finally:
        db.execute("RELEASE restore_result_identities")
