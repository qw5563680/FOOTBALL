"""Repair only JC match identities explicitly named in approved alias evidence."""

import csv
import hashlib
import io
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from zucai.db import get_engine, session_scope
from zucai.models import Match, Team
from zucai.sources import jc_counterpart, sporttery_aliases
from zucai.sources.base import _atomic_write

LOG = logging.getLogger(__name__)
FIELDS = (
    "run_id",
    "league",
    "raw_name",
    "canonical",
    "jc_id",
    "api_id",
    "previous_team_id",
    "canonical_team_id",
    "alias_source",
    "reason",
)
EVIDENCE_FIELDS = {
    "jc_match_id",
    "provider_match_id",
    "league",
    "kickoff_utc",
    "side",
    "raw_team_id",
    "canonical_team_id",
    "canonical_binding",
    "opponent_team_id",
}


def _utc(value):
    instant = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(instant, datetime) or instant.utcoffset() is None:
        raise ValueError("竞彩身份修复证据时间须包含明确时区")
    return instant


def _plans(aliases):
    """An approved name alone never authorizes rewriting all its historical matches."""
    plans, claimed = [], set()
    for league, names in aliases.items():
        for raw, entry in names.items():
            canonical = sporttery_aliases.approved_canonical(aliases, league, raw)
            if canonical is None:
                continue
            for proof in entry.get("evidence", []):
                if not isinstance(proof, dict) or "jc_match_id" not in proof:
                    continue
                if (
                    not EVIDENCE_FIELDS <= proof.keys()
                    or proof["league"] != league
                    or proof["side"] not in {"home", "away"}
                    or not str(proof["jc_match_id"]).startswith("jc:")
                    or str(proof["provider_match_id"]).split(":")[0] not in jc_counterpart.PROVIDERS
                    or any(
                        type(proof[key]) is not int or proof[key] <= 0
                        for key in ("raw_team_id", "canonical_team_id", "opponent_team_id")
                    )
                    or len(
                        {
                            proof[key]
                            for key in ("raw_team_id", "canonical_team_id", "opponent_team_id")
                        }
                    )
                    != 3
                ):
                    raise ValueError("竞彩身份修复证据字段无效或不完整")
                _utc(proof["kickoff_utc"])
                provider = proof["provider_match_id"].split(":")[0]
                binding = proof["canonical_binding"]
                prefix = f"{provider}:{league}:team:"
                if not isinstance(binding, str) or not binding.startswith(prefix):
                    raise ValueError("竞彩身份修复来源绑定与联赛不一致")
                if not binding.removeprefix(prefix).isdigit():
                    raise ValueError("竞彩身份修复来源球队 ID 无效")
                if proof["jc_match_id"] in claimed:
                    raise ValueError("同一竞彩比赛存在多份身份修复证据")
                claimed.add(proof["jc_match_id"])
                plans.append((league, raw, canonical, entry, proof))
    return plans


def _checked_pair(session, league, raw, canonical, proof):
    jc = session.get(Match, proof["jc_match_id"])
    api = session.get(Match, proof["provider_match_id"])
    if jc is None or api is None:
        raise ValueError("明确证据中的竞彩或 API 比赛不存在")
    if (
        jc.source != "jc"
        or api.source not in jc_counterpart.PROVIDERS
        or jc.league != league
        or api.league != league
        or _utc(jc.kickoff_utc) != _utc(proof["kickoff_utc"])
        or _utc(api.kickoff_utc) != _utc(proof["kickoff_utc"])
    ):
        raise ValueError("比赛来源、联赛或精确开球与核验记录不符")
    old = session.get(Team, proof["raw_team_id"])
    target = session.get(Team, proof["canonical_team_id"])
    if old is None or old.name != raw or old.aliases:
        raise ValueError("缩写球队行名称或既有别名与核验记录不符")
    if target is None or target.name != canonical:
        raise ValueError("规范球队行名称与核验记录不符")
    binding = proof["canonical_binding"]
    prefix = binding.rsplit(":", 1)[0] + ":"
    if binding not in target.aliases or any(
        alias.startswith(prefix) and alias != binding for alias in target.aliases
    ):
        raise ValueError("规范球队的来源绑定缺失或冲突")
    for team in session.scalars(select(Team)):
        if team.id != target.id and any(
            alias.casefold() in {canonical.casefold(), binding.casefold()}
            for alias in [team.name, *team.aliases]
        ):
            raise ValueError("规范名或来源绑定存在多个球队所有者")
        if team.id not in {old.id, target.id} and any(
            alias.casefold() == raw.casefold() for alias in [team.name, *team.aliases]
        ):
            raise ValueError("缩写名已有其他球队别名所有者")
    side = proof["side"] + "_team_id"
    other = "away_team_id" if proof["side"] == "home" else "home_team_id"
    if (
        getattr(jc, side) not in {old.id, target.id}
        or getattr(api, side) != target.id
        or getattr(jc, other) != proof["opponent_team_id"]
        or getattr(api, other) != proof["opponent_team_id"]
    ):
        raise ValueError("比赛主客方向、缩写身份或对手与核验记录不符")
    original = getattr(jc, side)
    # The shared counterpart check sees the approved projection in the identity map.
    # no_autoflush ensures a rejected projection never issues a database write.
    try:
        with session.no_autoflush:
            setattr(jc, side, target.id)
            candidate = jc_counterpart.late_jc_candidate(session, api)
            if candidate is None or candidate.match_id != jc.match_id:
                raise ValueError("核验竞彩不是唯一同向 API 对应比赛")
            all_sources = set(session.scalars(select(Match.source).distinct()))
            neighbors = jc_counterpart._neighbors(session, jc, all_sources)
            if [match.match_id for match in neighbors] != [api.match_id]:
                raise ValueError("规范身份在同联赛时间窗存在其他冲突比赛")
    except jc_counterpart.CounterpartConflict as exc:
        raise ValueError(str(exc)) from exc
    finally:
        setattr(jc, side, original)
    return jc, api, side, target.id


def _write_audit(path, rows):
    previous = []
    if path.exists():
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream, strict=True)
            if reader.fieldnames != list(FIELDS):
                raise ValueError("竞彩身份修复审计字段无效")
            previous = list(reader)
        if any(None in row or any(v is None for v in row.values()) for row in previous):
            raise ValueError("竞彩身份修复审计行无效")
    records = list(previous)
    for record in rows:
        row = {field: str(record[field]) for field in FIELDS}
        if not any(all(old[k] == row[k] for k in FIELDS if k != "run_id") for old in records):
            records.append(row)
    if records == previous:
        return
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, FIELDS)
    writer.writeheader()
    writer.writerows(records)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, output.getvalue().encode("utf-8"))


def reconcile(database, run_id, *, aliases_path=None):
    """Offline explicit match rebind; leave team rows and every historical file intact."""
    database = Path(database)
    if not database.is_file():
        raise ValueError("待修复数据库不存在")
    path = Path(aliases_path or sporttery_aliases.ALIASES_PATH)
    aliases = sporttery_aliases.load_aliases(path)
    plans = _plans(aliases)
    alias_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    report = dict(
        status="complete",
        operation="jc-reconcile",
        requests=0,
        rekeyed_matches=0,
        linked_matches=0,
        already_reconciled=0,
        skipped_conflicts=0,
        pairs=[],
    )
    audits, links, previous_audits = [], [], {}
    engine = get_engine(database)
    try:
        with session_scope(engine) as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            for league, raw, canonical, entry, proof in plans:
                row = dict(
                    run_id=run_id,
                    league=league,
                    raw_name=raw,
                    canonical=canonical,
                    jc_id=proof["jc_match_id"],
                    api_id=proof["provider_match_id"],
                    previous_team_id=proof["raw_team_id"],
                    canonical_team_id=proof["canonical_team_id"],
                    alias_source=json.dumps(
                        {"formal_aliases_sha256": alias_hash, "entry": entry},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    reason="",
                )
                try:
                    jc, api, side, canonical_id = _checked_pair(
                        session, league, raw, canonical, proof
                    )
                    changed = getattr(jc, side) != canonical_id
                    linked = jc.counterpart_id != api.match_id
                    if not changed and not linked:
                        report["already_reconciled"] += 1
                        continue
                    row["previous_team_id"] = getattr(jc, side)
                    setattr(jc, side, canonical_id)
                    jc.counterpart_id = api.match_id
                    session.flush()
                    report["rekeyed_matches"] += int(changed)
                    report["linked_matches"] += int(linked)
                    row["reason"] = "rebound_and_linked" if changed else "linked"
                    links.append(
                        dict(
                            api_id=api.match_id,
                            jc_id=jc.match_id,
                            league=league,
                            api_kickoff_utc=api.kickoff_utc,
                            jc_kickoff_utc=jc.kickoff_utc,
                            outcome="linked",
                            reason="jc_identity_reconcile",
                            candidates=[jc.match_id],
                        )
                    )
                except ValueError as exc:
                    report["status"] = "partial"
                    report["skipped_conflicts"] += 1
                    row["reason"] = str(exc)
                audits.append(row)
            report["pairs"] = audits
            for name, rows, writer in (
                ("jc_identity_merges", audits, _write_audit),
                ("jc_relink", links, jc_counterpart._write_audit),
            ):
                if not rows:
                    continue
                audit_path = database.parent / f"state/audit/{name}.csv"
                previous_audits[audit_path] = (
                    audit_path.read_bytes() if audit_path.exists() else None
                )
                writer(audit_path, rows)
    except Exception:
        for audit_path, previous in previous_audits.items():
            try:
                if previous is None:
                    audit_path.unlink(missing_ok=True)
                elif not audit_path.exists() or audit_path.read_bytes() != previous:
                    _atomic_write(audit_path, previous)
            except OSError:
                LOG.exception("数据库已回滚，但恢复竞彩身份审计失败；本轮不得发布")
        raise
    finally:
        engine.dispose()
    return report


def write_summary(report):
    if not os.environ.get("GITHUB_STEP_SUMMARY"):
        return
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
        stream.write(
            f"### 竞彩已核验缩写身份修复：{report['status']}\n\n"
            f"重定向 {report['rekeyed_matches']} 场；关联 {report['linked_matches']} 场；"
            f"已修复 {report['already_reconciled']} 场；"
            f"冲突跳过 {report['skipped_conflicts']} 场。\n\n"
            "只处理正式别名列出的比赛证据，不请求 API，不改历史球队名称或冻结预测。"
            "审计：state/audit/jc_identity_merges.csv、jc_relink.csv。\n\n"
        )
