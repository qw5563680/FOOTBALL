"""Explicit aliases, official-first identity checks and auditable FD ingestion."""

import csv
import hashlib
import json
import logging
import math
import re
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import IntegrityError

from zucai.db import get_engine, init_db, session_scope
from zucai.models import ClosingOdds, Match, Result, Team
from zucai.sources.base import RawStore
from zucai.sources.football_data import (
    ALIASES_PATH,
    LEAGUES_PATH,
    FootballDataCoUk,
    csv_url,
    read_json,
    season_start,
    verified_league,
)
from zucai.sources.results import GameResult, apply_result
from zucai.sources.teams import _record_unknown, normalize_team, upsert_team

LOG = logging.getLogger(__name__)
WINDOW = timedelta(hours=3)
COUNT_KEYS = (
    "inserted",
    "corrected",
    "linked_existing",
    "unchanged",
    "assumed",
    "rejected_rows",
    "suspected_duplicates",
    "closing_groups",
)


def _slug(value):
    plain = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().casefold()
    slug = re.sub(r"[^a-z0-9]+", "-", plain).strip("-")
    if not slug:
        raise ValueError("队名无法产生非空slug")
    return slug


def fd_match_id(game):
    return (
        f"fd:{game.div_code}:{game.kickoff_utc.astimezone(UTC):%Y%m%d}:"
        f"{_slug(game.home_raw)}:{_slug(game.away_raw)}"
    )


def _canonical(aliases, league, raw):
    entry = aliases.get(league, {}).get(raw)
    if entry is None:
        return None
    if not isinstance(entry, dict) or entry.get("status") not in {
        "approved",
        "auto_verified",
        "proposed",
    }:
        raise ValueError(f"别名状态无效：{league}/{raw}")
    if entry["status"] == "proposed":
        return None
    if not isinstance(entry.get("canonical"), str) or not entry["canonical"].strip():
        raise ValueError(f"生效别名缺少canonical：{raw}")
    return normalize_team(entry["canonical"])


def _lookup(session, canonical):
    if canonical is None:
        return None
    found = [
        t
        for t in session.scalars(select(Team))
        if t.name.casefold() == canonical.casefold()
        or canonical.casefold() in {normalize_team(a).casefold() for a in t.aliases}
    ]
    if len(found) > 1:
        raise ValueError(f"规范队名存在歧义：{canonical}")
    return found[0] if found else None


def _team(session, raw, canonical, league):
    if canonical is not None:
        team = upsert_team(session, canonical, "fd")
        other = session.scalar(select(Team).where(Team.name == normalize_team(raw)))
        collision = (
            other is not None
            and other.id != team.id
            and session.scalar(
                select(Match.match_id)
                .where(
                    Match.league == league,
                    or_(Match.home_team_id == other.id, Match.away_team_id == other.id),
                )
                .limit(1)
            )
            is not None
        )
        if collision:
            raise ValueError(f"{raw}已有独立球队身份，须先人工核对后合并")
        owners = [
            t
            for t in session.scalars(select(Team))
            if t.id != team.id
            and normalize_team(raw).casefold() in {normalize_team(a).casefold() for a in t.aliases}
        ]
        if owners:
            raise ValueError(f"{raw}的现有全局别名指向另一球队")
        if raw != team.name and raw not in team.aliases:
            team.aliases.append(raw)
        session.flush()
        return team
    # Proposed/unlisted names must not activate an unrelated global alias.
    name = normalize_team(raw)
    team = session.scalar(select(Team).where(Team.name == name))
    if team is None:
        team = Team(name=name, aliases=[])
        session.add(team)
        session.flush()
    return team


def _validate(game, seen):
    if game.kickoff_utc.utcoffset() is None or seen.utcoffset() is None or seen <= game.kickoff_utc:
        raise ValueError("完赛采集须晚于带时区开球时刻")
    for score in (game.full, game.half):
        if score is None and game.full is not None:
            continue
        if (
            not isinstance(score, tuple)
            or len(score) != 2
            or any(type(v) is not int or not 0 <= v <= 99 for v in score)
        ):
            raise ValueError("无效常规时间或半场比分")
    if game.half and any(a > b for a, b in zip(game.half, game.full, strict=True)):
        raise ValueError("半场比分大于全场比分")
    if normalize_team(game.home_raw).casefold() == normalize_team(game.away_raw).casefold():
        raise ValueError("主客队不能相同")


def _closing(session, key, groups):
    count = 0
    for book, values in groups.items():
        if book not in {"PS", "B365", "Avg", "Max"}:
            continue
        if len(values) != 3 or not all(
            isinstance(v, (int, float)) and math.isfinite(v) and v > 1 for v in values
        ):
            continue
        statement = insert(ClosingOdds).values(
            match_id=key,
            bookmaker=book,
            pool="had",
            h=values[0],
            d=values[1],
            a=values[2],
            basis="publisher_closing",
        )
        session.execute(
            statement.on_conflict_do_update(
                index_elements=[ClosingOdds.match_id, ClosingOdds.bookmaker, ClosingOdds.pool],
                set_={k: getattr(statement.excluded, k) for k in ("h", "d", "a", "basis")},
            )
        )
        count += 1
    return count


def import_fd_games(session, games, *, seen, assume, aliases, leagues):
    if not isinstance(assume, timedelta) or not timedelta(0) < assume <= timedelta(days=14):
        raise ValueError("必须显式指定--assume-known-after，正延时且不超过14天")
    counts = dict.fromkeys(COUNT_KEYS, 0)
    rejected, suspected, links, outcomes, unmatched = [], [], [], [], set()
    for game in games:
        audit = dict(
            div_code=game.div_code,
            row_number=game.row_number,
            raw_sha256=game.raw_sha256,
            home=game.home_raw,
            away=game.away_raw,
            kickoff_utc=game.kickoff_utc.isoformat(),
        )
        try:
            with session.begin_nested():
                _validate(game, seen)
                league = leagues[game.div_code]["league"]
                key = fd_match_id(game)
                names = [_canonical(aliases, league, raw) for raw in (game.home_raw, game.away_raw)]
                known = [_lookup(session, name) for name in names]
                official = list(
                    session.scalars(
                        select(Match).where(
                            Match.source == "jc",
                            Match.league == league,
                            Match.kickoff_utc >= game.kickoff_utc - WINDOW,
                            Match.kickoff_utc <= game.kickoff_utc + WINDOW,
                        )
                    )
                )
                matches = [
                    m
                    for m in official
                    if all(known) and (m.home_team_id, m.away_team_id) == (known[0].id, known[1].id)
                ]
                if len(matches) > 1:
                    raise ValueError("多个官方候选比赛，不猜测链接")
                if matches:
                    match = matches[0]
                    if session.get(Match, key) is not None:
                        raise ValueError("已有fd场次后来出现官方重复，须核对存量关联")
                    _team(session, game.home_raw, names[0], league)
                    _team(session, game.away_raw, names[1], league)
                    if match.counterpart_id is None:
                        match.counterpart_id = key
                    closing_groups = _closing(session, match.match_id, game.closing)
                    session.flush()
                    counts["closing_groups"] += closing_groups
                    counts["linked_existing"] += 1
                    links.append(dict(audit, fd_id=key, jc_id=match.match_id))
                    outcomes.append(dict(audit, match_id=match.match_id, outcome="linked_existing"))
                    continue
                reversed_pair = all(known) and any(
                    (m.home_team_id, m.away_team_id) == (known[1].id, known[0].id) for m in official
                )
                if official and (not all(names) or reversed_pair):
                    suspected.append(
                        dict(
                            audit,
                            candidates=[m.match_id for m in official],
                            reason="同联赛时间窗口存在官方比赛，身份未齐或主客冲突",
                        )
                    )
                    counts["suspected_duplicates"] += 1
                    continue
                teams = [
                    _team(session, raw, name, league)
                    for raw, name in zip((game.home_raw, game.away_raw), names, strict=True)
                ]
                existing = session.get(Match, key)
                old_result = session.get(Result, key)
                if existing and (
                    existing.league != league
                    or (existing.home_team_id, existing.away_team_id) != (teams[0].id, teams[1].id)
                ):
                    raise ValueError("fd主键碰撞或球队身份变化，不覆盖旧记录")
                older = bool(
                    existing
                    and existing.result_checked_utc
                    and seen < datetime.fromisoformat(existing.result_checked_utc)
                )
                changed = bool(
                    old_result
                    and (
                        (old_result.home_goals, old_result.away_goals) != game.full
                        or (
                            game.half is not None
                            and (old_result.ht_home_goals, old_result.ht_away_goals) != game.half
                        )
                    )
                )
                observed = GameResult(
                    key,
                    league,
                    None,
                    teams[0].name,
                    teams[1].name,
                    game.kickoff_utc.date(),
                    "finished",
                    game.full,
                    game.half,
                    kickoff_utc=game.kickoff_utc,
                )
                result = apply_result(session, observed, seen, assume)
                closing_groups = _closing(session, key, game.closing) if not older else 0
                # Force row constraints before publishing counters or external identity audits.
                session.flush()
                counts["closing_groups"] += closing_groups
                for raw, name in zip((game.home_raw, game.away_raw), names, strict=True):
                    if name is None:
                        unmatched.add((league, raw))
                        _record_unknown("fd", raw, normalize_team(raw))
                outcome = (
                    "unchanged" if result == "unchanged" else "corrected" if changed else "inserted"
                )
                counts[outcome] += 1
                counts["assumed"] += int(result == "assumed")
                outcomes.append(dict(audit, match_id=key, outcome=outcome))
        except (ValueError, KeyError, TypeError, IntegrityError) as exc:
            counts["rejected_rows"] += 1
            rejected.append(dict(audit, reason=str(exc)))
    return dict(
        counts,
        unmatched_names=[{"league": league, "raw": raw} for league, raw in sorted(unmatched)],
        rejected=rejected,
        suspected=suspected,
        links=links,
        outcomes=outcomes,
    )


def _append_audit(path, rows):
    if not rows:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = (
        "div_code",
        "row_number",
        "raw_sha256",
        "home",
        "away",
        "kickoff_utc",
        "date",
        "season",
        "reason",
        "candidates",
    )
    with path.open("a", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        if stream.tell() == 0:
            writer.writeheader()
        writer.writerows(
            {**row, "candidates": json.dumps(row.get("candidates", []))} for row in rows
        )


def fetch_fd(
    *,
    div=None,
    seasons=None,
    country=None,
    from_season=None,
    to_season=None,
    assume=None,
    db=None,
    leagues_path=LEAGUES_PATH,
    aliases_path=ALIASES_PATH,
    raw_file=None,
    raw_root="data/raw",
    audit_dir="data",
):
    if assume is None:
        raise ValueError("必须显式指定--assume-known-after")
    if bool(div) == bool(country):
        raise ValueError("指定一种来源：--div或--country")
    if div and (not seasons or from_season is not None or to_season is not None):
        raise ValueError("div须给出至少一个--season，不使用from/to-season")
    if country and (seasons or from_season is None or to_season is None or from_season > to_season):
        raise ValueError("country须给出有效--from-season和--to-season")
    if raw_file and div and len(seasons) != 1:
        raise ValueError("单一原件回放只接受一个season")
    leagues, aliases = read_json(leagues_path), read_json(aliases_path)
    code = div or country
    verified_league(leagues, code)
    source = FootballDataCoUk(store=RawStore(raw_root), leagues=leagues, div_code=code)
    total = dict.fromkeys((*COUNT_KEYS, "downloaded_files", "processed_files"), 0)
    audits, names = [], set()
    engine = get_engine(db)
    try:
        init_db(engine)
        for season in seasons if div else [None]:
            source.season = season
            expected_url = csv_url(div=div, season=season, country=country)
            if raw_file:
                path = Path(raw_file).resolve()
                raw = path.read_bytes()
                metadata = read_json(path.with_suffix(path.suffix + ".meta.json"))
                if (
                    metadata.get("status_code") != 200
                    or metadata.get("sha256") != hashlib.sha256(raw).hexdigest()
                    or metadata.get("url")
                    not in {expected_url, expected_url.replace("//www.", "//")}
                ):
                    raise ValueError("回放原件须有匹配URL、HTTP200、时间及SHA256 sidecar")
            else:
                path = source.fetch_raw(div=div, season=season, country=country)
                total["downloaded_files"] += 1
                raw, metadata = path.read_bytes(), source.store.load_metadata(path)
            seen = datetime.fromisoformat(metadata["fetched_at_utc"])
            if seen.utcoffset() is None:
                raise ValueError("原件采集时间须含UTC偏移")
            games = source.parse(raw)
            parser_rejected = source.rejected
            if country:
                games = [g for g in games if from_season <= season_start(g.season) <= to_season]
                parser_rejected = []
                for row in source.rejected:
                    try:
                        within_scope = from_season <= season_start(row["season"]) <= to_season
                    except ValueError:
                        within_scope = True  # Malformed season cannot be silently filtered away.
                    if within_scope:
                        parser_rejected.append(row)
            with session_scope(engine) as session:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                result = import_fd_games(
                    session, games, seen=seen, assume=assume, aliases=aliases, leagues=leagues
                )
            result["rejected_rows"] += len(parser_rejected)
            rejected = parser_rejected + result["rejected"]
            result["rejected"] = rejected
            _append_audit(Path(audit_dir) / "fd_rejected_rows.csv", rejected)
            _append_audit(Path(audit_dir) / "fd_suspected_duplicates.csv", result["suspected"])
            for key in COUNT_KEYS:
                total[key] += result[key]
            total["processed_files"] += 1
            names.update((n["league"], n["raw"]) for n in result["unmatched_names"])
            audits.append(
                dict(
                    path=str(path),
                    url=metadata["url"],
                    raw_sha256=hashlib.sha256(raw).hexdigest(),
                    seen_utc=seen.isoformat(),
                    **result,
                )
            )
    finally:
        source.session.close()
        engine.dispose()
    total["unmatched_teams"] = len(names)
    LOG.info(
        "FD下载%d文件，处理%d，入库%d，linked_existing=%d，拒绝%d，可疑重复%d，未对齐%d，assumed=%d",
        total["downloaded_files"],
        total["processed_files"],
        total["inserted"],
        total["linked_existing"],
        total["rejected_rows"],
        total["suspected_duplicates"],
        total["unmatched_teams"],
        total["assumed"],
    )
    return dict(total, files=audits)
