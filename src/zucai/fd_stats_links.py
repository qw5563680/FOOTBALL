"""Read-only canonical identity checks and durable FD statistics attachment proofs."""

import hashlib
import json
import math
import re
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import select

from zucai.models import FdStatsLink, Match, Team
from zucai.result_source_links import _archive_context, _stored_rows, _validate_rows
from zucai.sources.apif_stats import instant
from zucai.sources.fd_ingest import fd_match_id
from zucai.sources.teams import normalize_team

FD_LINK_FILE = "state/fd_stats_links.jsonl"
FD_LINK_COLUMNS = tuple(column.name for column in FdStatsLink.__table__.columns)
WINDOW = timedelta(hours=3)
FD_LEAGUES = {
    "E0": "英超",
    "E1": "英冠",
    "SP1": "西甲",
    "I1": "意甲",
    "D1": "德甲",
    "F1": "法甲",
    "F2": "法乙",
    "N1": "荷甲",
    "P1": "葡超",
}


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _formal(entry):
    if not isinstance(entry, dict) or entry.get("status") not in {"approved", "auto_verified"}:
        raise ValueError("FD 关联需要正式 approved/auto_verified 别名")
    name = entry.get("canonical")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("FD 正式别名缺少 canonical")
    if entry["status"] == "approved":
        if not all(
            isinstance(entry.get(k), str) and entry[k].strip()
            for k in ("approved_by", "approved_utc", "approval_pr")
        ):
            raise ValueError("FD 批准别名缺少批准证据")
        instant(entry["approved_utc"])
        if not entry["approval_pr"].startswith("https://"):
            raise ValueError("FD 别名批准位置无效")
    else:
        inference = entry.get("inference")
        if (
            not isinstance(inference, dict)
            or type(inference.get("unique_fd_candidates")) is not int
            or inference.get("unique_fd_candidates") != 1
            or type(inference.get("unique_jc_candidates")) is not int
            or inference.get("unique_jc_candidates") != 1
            or not isinstance(inference.get("witnesses"), list)
            or not inference.get("witnesses")
            or not all(isinstance(row, dict) for row in inference["witnesses"])
            or not isinstance(entry.get("evidence"), list)
            or not entry.get("evidence")
            or not all(isinstance(row, dict) for row in entry["evidence"])
        ):
            raise ValueError("FD 自动核验别名缺少双向唯一证据")
        for row in entry["evidence"]:
            if (
                not isinstance(row.get("raw_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", row["raw_sha256"])
                or row.get("div_code") not in FD_LEAGUES
                or type(row.get("fd_row")) is not int
                or row["fd_row"] < 2
                or not isinstance(row.get("jc_match_id"), str)
                or not row["jc_match_id"].startswith("jc:")
            ):
                raise ValueError("FD 自动核验原件证据无效")
        evidence_pairs = {(r["fd_row"], r["jc_match_id"]) for r in entry["evidence"]}
        fd_rows, jc_ids = set(), set()
        for witness in inference["witnesses"]:
            fd_row, jc_id = witness.get("fd_row"), witness.get("jc_match_id")
            difference = witness.get("difference_minutes")
            if (
                type(fd_row) is not int
                or fd_row < 2
                or not isinstance(jc_id, str)
                or not re.fullmatch(r"jc:[0-9]+", jc_id)
                or type(difference) not in (int, float)
                or not math.isfinite(difference)
                or not 0 <= difference <= 180
                or type(witness.get("home_known_before")) is not bool
                or type(witness.get("away_known_before")) is not bool
                or (fd_row, jc_id) not in evidence_pairs
                or fd_row in fd_rows
                or jc_id in jc_ids
            ):
                raise ValueError("FD 自动核验 witness 身份、时间或双向唯一证据无效")
            fd_rows.add(fd_row)
            jc_ids.add(jc_id)
    return normalize_team(name)


def _canonical_fd_team(teams, entry):
    name = _formal(entry)
    candidates = [
        t
        for t in teams
        if normalize_team(t["name"]).casefold() == name.casefold()
        or name.casefold() in {normalize_team(a).casefold() for a in t["aliases"]}
    ]
    if len(candidates) != 1 or candidates[0]["name"].startswith(("fd:", "fdorg:", "apif:")):
        raise ValueError("FD 正式别名未唯一映射到既有规范球队")
    # A separate historical English name is not a provider fixture ID binding.
    # The league-scoped formal canonical name supplies the identity evidence.
    return candidates[0]["id"]


def canonical_fd_sides(teams, aliases, league, home_raw, away_raw):
    """Resolve only formal names to existing identities, without adding global aliases."""
    if not isinstance(aliases, dict) or not isinstance(aliases.get(league), dict):
        raise ValueError("FD 联赛正式别名缺失")
    ids = [_canonical_fd_team(teams, aliases[league].get(raw)) for raw in (home_raw, away_raw)]
    if ids[0] == ids[1]:
        raise ValueError("FD 主客身份相同")
    return tuple(ids)


def _unique_match(matches, league, kickoff, pair, *, fd_counterpart=None):
    window = [
        m
        for m in matches
        if m["league"] == league and abs(instant(m["kickoff_utc"]) - instant(kickoff)) <= WINDOW
    ]
    related = [m for m in window if set(pair) & {m["home_team_id"], m["away_team_id"]}]
    direct = [m for m in related if (m["home_team_id"], m["away_team_id"]) == pair]
    targets = [m for m in direct if m["source"] == "jc"]
    if len(targets) != 1 or len(related) != len(direct):
        raise ValueError("FD 同联赛三小时内主客反向、共享球队或候选不唯一")
    match = targets[0]
    members = match.get("result_group", {match["match_id"]})
    if any(
        m["match_id"] != match["match_id"]
        and (m["source"] not in {"apif", "fdorg"} or m["match_id"] not in members)
        for m in related
    ):
        raise ValueError("FD 同联赛三小时内主客反向、共享球队或候选不唯一")
    pointer = match.get("counterpart_id")
    if pointer:
        if pointer == fd_counterpart:
            if any(
                other["match_id"] == fd_counterpart
                or (
                    other["match_id"] != match["match_id"]
                    and other.get("counterpart_id") == fd_counterpart
                )
                for other in matches
            ):
                raise ValueError("FD counterpart 声明不唯一")
        elif not pointer.startswith("fdorg:"):
            raise ValueError("FD 目标已有其他来源 counterpart 声明")
    return match


def _model_dict(model, columns):
    return {key: getattr(model, key) for key in columns}


def _attach_result_groups(db, matches):
    """Read complete valid proof components; stale snapshots grant no identity."""
    rows = _stored_rows(db)
    if not rows:
        return
    current, teams = _archive_context(db)
    _validate_rows(rows, current, teams)
    groups = {}
    for row in rows:
        left, right = row["provider_match_id"], row["counterpart_id"]
        if any(
            current[key][field] != row[field]
            for key in (left, right)
            for field in ("league", "home_team_id", "away_team_id")
        ) or any(
            current[key]["kickoff_utc"] != row[field]
            for key, field in (
                (left, "provider_kickoff_utc"),
                (right, "counterpart_kickoff_utc"),
            )
        ):
            continue
        members = groups.get(left, frozenset({left})) | groups.get(right, frozenset({right}))
        for member in members:
            groups[member] = members
    for match in matches:
        match["result_group"] = groups.get(match["match_id"], frozenset({match["match_id"]}))


def _session_context(session):
    teams = [_model_dict(t, ("id", "name", "aliases")) for t in session.scalars(select(Team))]
    matches = [
        _model_dict(
            m,
            (
                "match_id",
                "source",
                "league",
                "kickoff_utc",
                "home_team_id",
                "away_team_id",
                "counterpart_id",
            ),
        )
        for m in session.scalars(select(Match))
    ]
    _attach_result_groups(session.connection().connection.driver_connection, matches)
    return teams, matches


def fd_link_authorizes(link, *, match_id, raw_sha256, raw_location, captured_utc):
    """A link authorizes only an observation independently checked by the resolver."""
    evidence = link["evidence"] if isinstance(link, dict) else link.evidence
    target = link["match_id"] if isinstance(link, dict) else link.match_id
    try:
        return target == match_id and any(
            o["raw_sha256"] == raw_sha256
            and o["raw_location"] == raw_location
            and instant(o["captured_utc"]) == instant(captured_utc)
            for o in evidence["observations"]
        )
    except (KeyError, TypeError, ValueError):
        return False


def _validate_link(link, teams, matches):
    try:
        evidence = link["evidence"]
        if isinstance(evidence, str):
            evidence = json.loads(evidence)
            link["evidence"] = evidence
        if not isinstance(evidence, dict) or evidence.get("version") != 1:
            raise ValueError("FD 关联证据版本无效")
        aliases = evidence["aliases"]
        if _digest(aliases) != link["aliases_sha256"]:
            raise ValueError("FD 别名证据哈希不符")
        pair = canonical_fd_sides(
            teams, aliases, link["league"], link["home_raw"], link["away_raw"]
        )
        if pair != (link["home_team_id"], link["away_team_id"]):
            raise ValueError("FD 关联主客规范身份不符")
        kickoff = instant(link["kickoff_utc"])
        season = link["season"]
        if (
            not isinstance(season, str)
            or not re.fullmatch(r"[0-9]{4}", season)
            or int(season[2:]) != (int(season[:2]) + 1) % 100
            or kickoff.year - (kickoff.month < 7) != 2000 + int(season[:2])
        ):
            raise ValueError("FD 关联赛季不符")
        game = SimpleNamespace(
            div_code=link["div_code"],
            kickoff_utc=kickoff,
            home_raw=link["home_raw"],
            away_raw=link["away_raw"],
        )
        if FD_LEAGUES.get(link["div_code"]) != link["league"]:
            raise ValueError("FD 关联 division 无效")
        if fd_match_id(game) != link["provider_fixture_id"]:
            raise ValueError("FD 关联 provider fixture ID 不符")
        original_pointer = evidence["counterpart_id"]
        # A later result import may fill an empty pointer with this exact FD
        # identity. Keep the original evidence; never replace a prior fdorg link.
        match = _unique_match(
            matches,
            link["league"],
            kickoff,
            pair,
            fd_counterpart=link["provider_fixture_id"] if original_pointer is None else None,
        )
        if (
            match["match_id"] != link["match_id"]
            or match["counterpart_id"] != original_pointer
            and not (
                original_pointer is None and match["counterpart_id"] == link["provider_fixture_id"]
            )
        ):
            raise ValueError("FD 关联目标或原 counterpart 不符")
        observations = evidence["observations"]
        if not isinstance(observations, list) or not observations:
            raise ValueError("FD 关联原件观察缺失")
        for obs in observations:
            if (
                not isinstance(obs, dict)
                or set(obs) != {"raw_sha256", "raw_location", "captured_utc"}
                or not isinstance(obs["raw_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", obs["raw_sha256"])
                or not isinstance(obs["raw_location"], str)
                or not obs["raw_location"].strip()
                or instant(obs["captured_utc"]).utcoffset() != timedelta(0)
            ):
                raise ValueError("FD 关联原件观察证据无效")
        if not fd_link_authorizes(
            link,
            match_id=link["match_id"],
            raw_sha256=link["raw_sha256"],
            raw_location=link["raw_location"],
            captured_utc=link["captured_utc"],
        ):
            raise ValueError("FD 关联初始原件证据缺失")
    except (KeyError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        raise ValueError("FD 关联证据缺失或格式无效") from exc
    return link


def validate_fd_links(db):
    """Validate every attachment using SQLite only; missing legacy table is allowed."""
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "fd_stats_links" not in tables:
        return {}

    def rows(table, columns):
        return [
            dict(zip(columns, r, strict=True))
            for r in db.execute(f"SELECT {','.join(columns)} FROM {table}")
        ]

    links = rows("fd_stats_links", FD_LINK_COLUMNS)
    if not links:
        return {}
    teams = rows("teams", ("id", "name", "aliases"))
    for team in teams:
        try:
            team["aliases"] = json.loads(team["aliases"])
        except (ValueError, TypeError) as exc:
            raise ValueError("FD 关联球队别名格式无效") from exc
    matches = rows(
        "matches",
        (
            "match_id",
            "source",
            "league",
            "kickoff_utc",
            "home_team_id",
            "away_team_id",
            "counterpart_id",
        ),
    )
    _attach_result_groups(db, matches)
    result, targets = {}, set()
    for link in links:
        _validate_link(link, teams, matches)
        if link["provider_fixture_id"] in result or link["match_id"] in targets:
            raise ValueError("FD 关联双向唯一约束失败")
        result[link["provider_fixture_id"]] = link
        targets.add(link["match_id"])
    return result


def _validate_legacy_aliases(session, match, game, league, aliases, context):
    """Legacy pointers remain usable, but cannot override existing formal conflicts."""
    entries = aliases.get(league, {}) if isinstance(aliases, dict) else {}
    if not isinstance(entries, dict):
        return
    teams = None
    for side in ("home", "away"):
        raw = getattr(game, side + "_raw")
        entry = entries.get(raw)
        if not isinstance(entry, dict) or entry.get("status") not in {"approved", "auto_verified"}:
            continue
        if teams is None:
            teams = context[0] if context is not None else _session_context(session)[0]
        team_id = _canonical_fd_team(teams, entry)
        if team_id != getattr(match, side + "_team_id"):
            raise ValueError("FD 既有比赛主客身份与正式别名矛盾")


def resolve_fd_match(session, game, league, metadata, aliases, *, persist=True, context=None):
    """Prefer legacy attachments, otherwise prove a new canonical JC stats link."""
    if FD_LEAGUES.get(game.div_code) != league:
        raise ValueError("FD division 与联赛不符")
    provider_id = fd_match_id(game)
    old = session.get(FdStatsLink, provider_id)
    direct = session.get(Match, provider_id)
    if direct is not None:
        if old is not None:
            raise ValueError("FD 统计关联与既有 FD 比赛冲突")
        if (
            direct.source != "fd"
            or direct.league != league
            or instant(direct.kickoff_utc) != game.kickoff_utc
        ):
            raise ValueError("FD 既有比赛身份或时间不符")
        _validate_legacy_aliases(session, direct, game, league, aliases, context)
        return direct
    counterparts = list(session.scalars(select(Match).where(Match.counterpart_id == provider_id)))
    if counterparts:
        if (
            len(counterparts) != 1
            or counterparts[0].source != "jc"
            or counterparts[0].league != league
            or abs(instant(counterparts[0].kickoff_utc) - game.kickoff_utc) > WINDOW
        ):
            raise ValueError("FD 已知 counterpart 不唯一或联赛时间不符")
        _validate_legacy_aliases(session, counterparts[0], game, league, aliases, context)
        if old is None:
            return counterparts[0]
        # An existing proof still owns its observation history after a result
        # import fills the pointer; the legacy shortcut must not bypass it.
    teams, matches = context if context is not None else _session_context(session)
    pair = canonical_fd_sides(teams, aliases, league, game.home_raw, game.away_raw)
    target = _unique_match(
        matches,
        league,
        game.kickoff_utc,
        pair,
        fd_counterpart=provider_id if old is not None else None,
    )
    claimed = session.scalar(select(FdStatsLink).where(FdStatsLink.match_id == target["match_id"]))
    if claimed is not None and claimed.provider_fixture_id != provider_id:
        raise ValueError("FD 目标已由另一 provider fixture 占用")
    if old is not None:
        previous = _validate_link(_model_dict(old, FD_LINK_COLUMNS), teams, matches)
        if (
            old.match_id != target["match_id"]
            or old.home_team_id != pair[0]
            or old.away_team_id != pair[1]
            or instant(old.kickoff_utc) != game.kickoff_utc
            or old.home_raw != game.home_raw
            or old.away_raw != game.away_raw
            or old.league != league
            or old.div_code != game.div_code
            or old.season != game.season
        ):
            raise ValueError("FD 既有统计关联禁止变更身份或来源")
        evidence = dict(previous["evidence"])
    else:
        evidence = dict(
            version=1, aliases=aliases, counterpart_id=target["counterpart_id"], observations=[]
        )
    observation = {k: metadata[k] for k in ("captured_utc", "raw_sha256", "raw_location")}
    if observation not in evidence["observations"]:
        evidence["observations"] = [*evidence["observations"], observation]
    if persist:
        if old is None:
            old = FdStatsLink(
                provider_fixture_id=provider_id,
                match_id=target["match_id"],
                league=league,
                div_code=game.div_code,
                season=game.season,
                kickoff_utc=game.kickoff_utc,
                home_raw=game.home_raw,
                away_raw=game.away_raw,
                home_team_id=pair[0],
                away_team_id=pair[1],
                aliases_sha256=_digest(aliases),
                evidence=evidence,
                **metadata,
            )
            _validate_link(_model_dict(old, FD_LINK_COLUMNS), teams, matches)
            session.add(old)
        else:
            old.evidence = evidence
        session.flush()
    return session.get(Match, target["match_id"])
