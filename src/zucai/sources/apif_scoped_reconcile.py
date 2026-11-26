"""Explicit offline consolidation for approved scoped APIF identities.

Identity projection is league-local and precedes counterpart validation, so two
provisional opponents do not deadlock. Neither route permission nor results change.
"""

import argparse
import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from sqlalchemy import or_, select, text

from zucai.db import get_engine, session_scope
from zucai.league_registry import load_registry
from zucai.models import Base, Match
from zucai.sources import jc_counterpart
from zucai.sources.apif_aliases import binding_scope_allows, validate_approvals
from zucai.sources.apif_command import ALIASES
from zucai.sources.apif_ingest import _Teams
from zucai.sources.apif_reconcile import _formal_entry, _references, write_audit
from zucai.sources.base import _atomic_write


def reconcile_scoped(database, codes, run_id, *, aliases_path=None):
    database = Path(database)
    if not database.is_file():
        raise ValueError("待合并数据库不存在")
    path = Path(aliases_path or ALIASES)
    raw = path.read_bytes()
    aliases = json.loads(raw)
    validate_approvals(aliases, require_metadata=True)
    from zucai.jc_catalogue_kickoffs import APPROVED_ROUTE_IDS, cup_identity_route

    registry = load_registry()["providers"]["apif"]
    codes = sorted(set(str(c) for c in codes))
    if not codes or any(
        (c not in APPROVED_ROUTE_IDS and not cup_identity_route(c, registry.get(c)))
        or c not in registry
        or registry[c]["status"] != "verified"
        or registry[c].get("mode") != "import"
        for c in codes
    ):
        raise ValueError("须明确选择已核验的 import 路线")
    report = dict(
        operation="scoped-reconcile",
        requests=0,
        daily_changed=False,
        rekeyed_matches=0,
        linked=0,
        pending_links=[],
        decisions=[],
    )
    audit_path = database.parent / "state/audit/apif_scoped_identity_merges.csv"
    link_path = database.parent / "state/audit/jc_relink.csv"
    previous = {p: p.read_bytes() if p.exists() else None for p in (audit_path, link_path)}
    engine = get_engine(database)
    moved, audit, relinks = set(), [], []
    try:
        with session_scope(engine) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            for code in codes:
                league = registry[code]["config"]["league"]
                teams = _Teams(session, aliases)
                plans = []
                for key, entry in sorted(aliases.get(league, {}).items()):
                    if entry.get("status") != "approved" or "binding_scope" not in entry:
                        continue  # This entry cannot authorize any legacy migration.
                    temporary = teams.one(f"apif:team:{key}")
                    if temporary is None:
                        continue
                    row = dict(
                        run_id=run_id,
                        league=league,
                        team_id=key,
                        provisional_team_row_id=temporary.id,
                        canonical=entry["canonical"],
                        matches_rekeyed=0,
                        alias_status=entry["status"],
                        reason="",
                        alias_source=json.dumps(
                            dict(
                                formal_aliases_sha256=sha256(raw).hexdigest(),
                                entry=entry,
                                verified_utc=datetime.now(UTC).isoformat(),
                            ),
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    )
                    try:
                        _formal_entry(session, aliases, league, key)
                        if temporary.name != f"apif:team:{key}" or temporary.aliases:
                            raise ValueError("临时身份名称或 aliases 冲突")
                        target = teams.one(entry["canonical"])
                        if target is None or target.name.startswith(("apif:", "fdorg:")):
                            raise ValueError("规范身份缺失或仍为临时身份")
                        if (league, key) in teams.collisions:
                            raise ValueError("同联赛规范身份对应多个来源编号")
                        binding = f"apif:{league}:team:{key}"
                        owner = teams.one(binding)
                        if owner is not None and owner.id != target.id:
                            raise ValueError("来源编号已绑定其他球队")
                        prefix = f"apif:{league}:team:"
                        if any(a.startswith(prefix) and a != binding for a in target.aliases):
                            raise ValueError("规范身份已有另一个同联赛来源编号")
                        for table in Base.metadata.sorted_tables:
                            if table.name == "matches":
                                continue
                            columns = [
                                c
                                for c in table.columns
                                if any(fk.target_fullname == "teams.id" for fk in c.foreign_keys)
                            ]
                            if (
                                columns
                                and session.execute(
                                    select(table)
                                    .where(or_(*(c == temporary.id for c in columns)))
                                    .limit(1)
                                ).first()
                                is not None
                            ):
                                raise ValueError("临时身份有附属统计或身份快照引用，须专项核对")
                        references = _references(session, temporary.id, league)
                        if any(m.source != "apif" for m in references):
                            raise ValueError("临时身份被非 APIF 比赛引用")
                        games = [
                            m
                            for m in references
                            if binding_scope_allows(entry, league, kickoff_utc=m.kickoff_utc)
                        ]
                        if not games:
                            continue
                        plans.append((temporary, target, binding, games, row))
                    except ValueError as exc:
                        row["reason"] = str(exc)
                        audit.append(row)
                # One projected snapshot, never per-team iteration order.
                projected = {}
                for temporary, target, _binding, games, _row in plans:
                    for game in games:
                        pair = projected.setdefault(
                            game.match_id, [game.home_team_id, game.away_team_id]
                        )
                        for side in (0, 1):
                            if pair[side] == temporary.id:
                                pair[side] = target.id
                if any(pair[0] == pair[1] for pair in projected.values()):
                    raise ValueError("投影后主客球队相同；事务回滚")
                for _temporary, target, binding, games, row in plans:
                    for game in games:
                        game.home_team_id, game.away_team_id = projected[game.match_id]
                        moved.add(game.match_id)
                    if binding not in target.aliases:
                        target.aliases.append(binding)
                    row.update(matches_rekeyed=len(games), reason="scoped_identity_merged")
                    audit.append(row)
                session.flush()
                # Re-check stored authorized pairs too: a JC row can arrive after migration.
                current = _Teams(session, aliases)
                authorized = {}
                for key, entry in aliases.get(league, {}).items():
                    if entry.get("status") != "approved" or "binding_scope" not in entry:
                        continue
                    try:
                        _formal_entry(session, aliases, league, key)
                        target = current.one(entry["canonical"])
                        if (
                            target is not None
                            and (league, key) not in current.collisions
                            and f"apif:{league}:team:{key}" in target.aliases
                        ):
                            authorized.setdefault(target.id, []).append(entry)
                    except ValueError:
                        continue  # A conflicting formal identity never authorizes a new link.
                link_ids = set(projected)
                for game in session.scalars(
                    select(Match).where(Match.source == "apif", Match.league == league)
                ):
                    if all(
                        any(
                            binding_scope_allows(e, league, kickoff_utc=game.kickoff_utc)
                            for e in authorized.get(tid, [])
                        )
                        for tid in (game.home_team_id, game.away_team_id)
                    ):
                        link_ids.add(game.match_id)
                for mid in sorted(link_ids):
                    game = session.get(Match, mid)
                    try:
                        target = jc_counterpart.late_jc_candidate(session, game)
                        if target is not None and target.counterpart_id != mid:
                            target.counterpart_id = mid
                            report["linked"] += 1
                            relinks.append(
                                dict(
                                    api_id=mid,
                                    jc_id=target.match_id,
                                    league=league,
                                    api_kickoff_utc=game.kickoff_utc,
                                    jc_kickoff_utc=target.kickoff_utc,
                                    outcome="linked",
                                    reason="scoped_reconcile",
                                    candidates=[target.match_id],
                                )
                            )
                    except jc_counterpart.CounterpartConflict as exc:
                        report["pending_links"].append(
                            dict(
                                match_id=mid,
                                league=league,
                                reason=str(exc),
                                candidates=exc.candidates,
                            )
                        )
                        relinks.append(
                            dict(
                                api_id=mid,
                                jc_id="",
                                league=league,
                                api_kickoff_utc=game.kickoff_utc,
                                jc_kickoff_utc="",
                                outcome="skipped",
                                reason=str(exc),
                                candidates=exc.candidates,
                            )
                        )
                # Shared provisional rows in other competitions remain intact.
                for temporary, *_ in plans:
                    if not _references(session, temporary.id):
                        session.delete(temporary)
                session.flush()
            if audit:
                write_audit(audit_path, audit)
            if relinks:
                jc_counterpart._write_audit(link_path, relinks)
    except Exception:
        for p, content in previous.items():
            if content is None:
                p.unlink(missing_ok=True)
            else:
                _atomic_write(p, content)
        raise
    finally:
        engine.dispose()
    report.update(
        rekeyed_matches=len(moved),
        changed_match_ids=sorted(moved),
        decisions=audit,
        status="partial"
        if report["pending_links"] or any(r["reason"] != "scoped_identity_merged" for r in audit)
        else "complete",
    )
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--codes", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = reconcile_scoped(args.db, args.codes.split(","), args.run_id)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(args.out, (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode())


if __name__ == "__main__":
    main()
