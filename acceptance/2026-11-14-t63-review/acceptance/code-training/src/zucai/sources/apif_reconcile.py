"""Rekey formally verified provider identities without altering importer policy."""

import csv
import hashlib
import io
import json
import logging
from copy import deepcopy
from pathlib import Path

from sqlalchemy import or_, select, text

from zucai.cup_history import PROVISIONAL, coverage_lines, history_metrics
from zucai.db import get_engine, session_scope
from zucai.league_registry import daily_codes, provider_config, provider_entries
from zucai.models import Match, Team
from zucai.sources import jc_counterpart
from zucai.sources.api_football import LEAGUES as APIF_LEAGUES
from zucai.sources.apif_aliases import validate_approvals
from zucai.sources.apif_command import ALIASES
from zucai.sources.apif_ingest import _Teams
from zucai.sources.base import _atomic_write
from zucai.sources.cup_identity import CUP_LEAGUES
from zucai.sources.fd_ingest import WINDOW, _canonical
from zucai.sources.fd_verify import _utc
from zucai.sources.fdorg_verify import DEFAULT_ALIASES as FDORG_ALIASES
from zucai.sources.fdorg_verify import _active
from zucai.sources.football_data import read_json
from zucai.sources.football_data_org import COMPETITIONS_PATH

LOG = logging.getLogger(__name__)
_DEFAULT_CONFIG_PATHS = {"apif": APIF_LEAGUES, "fdorg": COMPETITIONS_PATH}
FIELDS = (
    "run_id",
    "league",
    "team_id",
    "provisional_team_row_id",
    "canonical",
    "matches_rekeyed",
    "alias_status",
    "alias_source",
    "reason",
)


def write_audit(path, rows):
    previous = []
    if path.exists():
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream, strict=True)
            if reader.fieldnames != list(FIELDS):
                raise ValueError("身份合并审计字段无效")
            previous = list(reader)
        if any(None in row or any(v is None for v in row.values()) for row in previous):
            raise ValueError("身份合并审计行无效")
    records = {}
    for row in [*previous, *rows]:
        row = {k: str(row[k]) for k in FIELDS}
        key = tuple(row[k] for k in ("league", "team_id", "canonical"))
        old = records.get(key)
        if old and all(old[k] == row[k] for k in FIELDS if k != "run_id"):
            continue
        records[key] = row
    if list(records.values()) == previous:
        return
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, FIELDS)
    writer.writeheader()
    writer.writerows(records.values())
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, output.getvalue().encode("utf-8"))


def _references(session, team_id, league=None):
    statement = select(Match).where(
        or_(Match.home_team_id == team_id, Match.away_team_id == team_id)
    )
    if league is not None:
        statement = statement.where(Match.league == league)
    return list(session.scalars(statement.order_by(Match.match_id)))


def _coverage(session):
    teams = [dict(id=t.id, name=t.name) for t in session.scalars(select(Team))]
    games = [
        dict(
            match_id=m.match_id,
            source=m.source,
            league=m.league,
            home_team_id=m.home_team_id,
            away_team_id=m.away_team_id,
            kickoff_utc=m.kickoff_utc,
        )
        for m in session.scalars(select(Match))
    ]
    return history_metrics(teams, games)


def _formal_entry(session, aliases, league, key):
    """Only a formal active same-provider ID authorizes cross-league history moves."""
    known = {}
    teams = _Teams(session, aliases)  # Exact names/aliases only; never calls resolve.
    for source_league in sorted(aliases):
        canonical = _canonical(aliases, source_league, key)
        if canonical is None:
            continue
        _active(aliases, source_league)
        if (source_league, key) in teams.collisions:
            raise ValueError("来源联赛同一规范球队对应多个生效的来源球队 ID")
        known[source_league] = aliases[source_league][key]
    if not known:
        return None, []
    names = {_canonical(aliases, origin, key) for origin in known}
    if len(names) != 1:
        raise ValueError("跨联赛的同源数字球队 ID 存在规范名冲突")
    entry = deepcopy(known.get(league, next(iter(known.values()))))
    return entry, list(known)


def _daily_domestic_leagues():
    from zucai.cloud_apis import COMPETITION_CODES

    if APIF_LEAGUES == _DEFAULT_CONFIG_PATHS["apif"]:
        approved = provider_entries("apif", purpose="daily") | provider_entries(
            "apif", modes="nations"
        )
        apif = {code: entry["config"] for code, entry in approved.items()}
    else:
        apif = read_json(APIF_LEAGUES)
    if COMPETITIONS_PATH == _DEFAULT_CONFIG_PATHS["fdorg"]:
        fdorg, fdorg_codes = provider_config("fdorg"), daily_codes("fdorg")
    else:
        fdorg, fdorg_codes = read_json(COMPETITIONS_PATH), COMPETITION_CODES.split(",")
    return {
        "apif": {
            entry["league"]
            for entry in apif.values()
            if not entry.get("identity_only", False) and not entry.get("index_only", False)
        }
        - CUP_LEAGUES["apif"],
        "fdorg": {fdorg[code]["league"] for code in fdorg_codes} - CUP_LEAGUES["fdorg"],
    }


def _jc_links(session, games, existing, temporary_id, target_id):
    """Preflight every move; only a unique, unclaimed exact JC conflict can be linked."""
    links = []
    reason = "同联赛三小时内规范身份已有比赛"
    for game in games:
        conflicts = [
            match
            for match in existing
            if abs(_utc(game.kickoff_utc) - _utc(match.kickoff_utc)) <= WINDOW
        ]
        if not conflicts:
            continue
        if len(conflicts) != 1 or conflicts[0].source != "jc":
            raise ValueError(reason)
        pair = tuple(
            target_id if team == temporary_id else team
            for team in (game.home_team_id, game.away_team_id)
        )
        try:
            candidate = jc_counterpart.late_jc_candidate(session, game, team_ids=pair)
        except jc_counterpart.CounterpartConflict as exc:
            raise ValueError(reason) from exc
        if (
            candidate is None
            or candidate.match_id != conflicts[0].match_id
            or candidate.counterpart_id is not None
        ):
            raise ValueError(reason)
        links.append((game, candidate))
    return links


def reconcile(database, run_id):
    """Apply existing T23 checks in every league, independently for APIF and FDORG."""
    database = Path(database)
    if not database.is_file():
        raise ValueError("待合并数据库不存在")
    paths = {"apif": Path(ALIASES), "fdorg": Path(FDORG_ALIASES)}
    formal = {provider: read_json(path) for provider, path in paths.items()}
    for provider, aliases in formal.items():
        validate_approvals(aliases, require_metadata=provider == "apif")
    hashes = {
        provider: hashlib.sha256(path.read_bytes()).hexdigest() for provider, path in paths.items()
    }
    daily_leagues = _daily_domestic_leagues()
    engine = get_engine(database)
    report = dict(
        status="complete",
        operation="reconcile",
        requests=0,
        merged_teams=0,
        rekeyed_matches=0,
        deleted_teams=0,
        skipped_conflicts=0,
        unresolved_pairs=0,
    )
    merged, rekeyed = set(), set()
    audits = {provider: [] for provider in paths}
    relinks = []
    previous_audits = {}
    try:
        with session_scope(engine) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            report["coverage_before"] = _coverage(session)
            provisional = [
                t
                for t in session.scalars(select(Team).order_by(Team.id))
                if PROVISIONAL.fullmatch(t.name)
            ]
            for temporary in provisional:
                provider, key = PROVISIONAL.fullmatch(temporary.name).groups()
                aliases = formal[provider]
                leagues = sorted({m.league for m in _references(session, temporary.id)})
                for league in leagues:
                    row = dict(
                        run_id=run_id,
                        league=league,
                        team_id=key,
                        provisional_team_row_id=temporary.id,
                        canonical="",
                        matches_rekeyed=0,
                        alias_status="",
                        alias_source="",
                        reason="",
                    )
                    try:
                        entry, origins = _formal_entry(session, aliases, league, key)
                        if entry is None:
                            report["unresolved_pairs"] += 1
                            continue
                        entries = {**aliases.get(league, {}), key: entry}
                        canonical = _canonical({league: entries}, league, key)
                        row.update(
                            canonical=canonical,
                            alias_status=entry["status"],
                            alias_source=json.dumps(
                                dict(
                                    formal_aliases_sha256=hashes[provider],
                                    kind="same_provider_id",
                                    provider=provider,
                                    source_leagues=origins,
                                    source_entries={
                                        origin: aliases[origin][key] for origin in origins
                                    },
                                    evidence=entry.get("evidence", []),
                                    league=league,
                                    team_id=key,
                                ),
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                        )
                        if (
                            league in daily_leagues[provider]
                            and _canonical(aliases, league, key) is None
                        ):
                            raise ValueError("目标联赛正式别名未传播")
                        if canonical.casefold().startswith(("apif:", "fdorg:")):
                            raise ValueError("规范队名使用了来源 ID 保留前缀")
                        resolved = deepcopy(aliases)
                        resolved[league] = entries
                        teams = _Teams(session, resolved)
                        if (league, key) in teams.collisions:
                            raise ValueError("同一规范球队对应多个生效的来源球队 ID")
                        target = teams.one(canonical)
                        if target is None:
                            raise ValueError("规范球队在库中缺失")
                        if target.id == temporary.id or PROVISIONAL.fullmatch(target.name):
                            raise ValueError("规范身份仍为临时球队")
                        if temporary.aliases:
                            raise ValueError("临时身份 aliases 非空")
                        prefix = f"{provider}:{league}:team:"
                        binding = prefix + key
                        owner = teams.one(binding)
                        if owner and owner.id != target.id:
                            raise ValueError("来源 ID 已绑定其他球队")
                        if any(a.startswith(prefix) and a != binding for a in target.aliases):
                            raise ValueError(f"规范球队已有不同的本联赛 {provider.upper()} ID")
                        games = _references(session, temporary.id, league)
                        if any(m.source != provider for m in games):
                            raise ValueError(f"临时身份被非 {provider.upper()} 比赛引用")
                        existing = _references(session, target.id, league)
                        links = _jc_links(session, games, existing, temporary.id, target.id)
                        # All checks precede the first mutation for this league/ID.
                        for game in games:
                            if game.home_team_id == temporary.id:
                                game.home_team_id = target.id
                            if game.away_team_id == temporary.id:
                                game.away_team_id = target.id
                            rekeyed.add(game.match_id)
                        for game, jc in links:
                            jc.counterpart_id = game.match_id
                            relinks.append(
                                dict(
                                    api_id=game.match_id,
                                    jc_id=jc.match_id,
                                    league=game.league,
                                    api_kickoff_utc=game.kickoff_utc,
                                    jc_kickoff_utc=jc.kickoff_utc,
                                    outcome="linked",
                                    reason="reconcile",
                                    candidates=[jc.match_id],
                                )
                            )
                        if binding not in target.aliases:
                            target.aliases.append(binding)
                        session.flush()
                        merged.add(temporary.id)
                        row.update(
                            matches_rekeyed=len(games),
                            reason="merged_and_linked" if links else "merged",
                        )
                    except ValueError as exc:
                        row["reason"] = str(exc)
                        report["skipped_conflicts"] += 1
                        report["status"] = "partial"
                    audits[provider].append(row)
                if temporary.id in merged and not _references(session, temporary.id):
                    session.delete(temporary)
                    report["deleted_teams"] += 1
                    session.flush()
            report["coverage_after"] = _coverage(session)
            for provider, rows in audits.items():
                audit_path = database.parent / f"state/audit/{provider}_identity_merges.csv"
                if not rows and not audit_path.exists():
                    continue
                previous_audits[audit_path] = (
                    audit_path.read_bytes() if audit_path.exists() else None
                )
                write_audit(audit_path, rows)
            if relinks:
                audit_path = database.parent / "state/audit/jc_relink.csv"
                previous_audits[audit_path] = (
                    audit_path.read_bytes() if audit_path.exists() else None
                )
                jc_counterpart._write_audit(audit_path, relinks)
    except Exception:
        # session_scope has rolled back; don't leave a successful audit of a failed commit.
        for audit_path, previous_audit in previous_audits.items():
            try:
                if previous_audit is None:
                    audit_path.unlink(missing_ok=True)
                elif not audit_path.exists() or audit_path.read_bytes() != previous_audit:
                    _atomic_write(audit_path, previous_audit)
            except OSError:
                LOG.exception("数据库已回滚，但恢复身份合并审计失败；本轮不得发布")
        raise
    finally:
        engine.dispose()
    report.update(merged_teams=len(merged), rekeyed_matches=len(rekeyed))
    LOG.info(
        "APIF/FDORG 身份合并 %d 队，重定向 %d 场，冲突跳过 %d",
        len(merged),
        len(rekeyed),
        report["skipped_conflicts"],
    )
    return report


def write_summary(report):
    import os

    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
            stream.write(
                f"### APIF/FDORG 存量身份合并：{report['status']}\n\n"
                f"合并临时球队 {report['merged_teams']}；删除空临时行 {report['deleted_teams']}；"
                f"重定向不同比赛 {report['rekeyed_matches']}；"
                f"冲突跳过 {report['skipped_conflicts']}；"
                f"尚无生效别名 {report['unresolved_pairs']} 个联赛/ID。\n\n"
                "不请求 API，不改 match_id、赛果或冻结预测。"
                "审计：state/audit/apif_identity_merges.csv、fdorg_identity_merges.csv。\n\n"
            )
            for title, key in (("合并前", "coverage_before"), ("合并后", "coverage_after")):
                stream.write(
                    f"#### {title}\n\n" + "\n\n".join(coverage_lines(report[key])) + "\n\n"
                )
