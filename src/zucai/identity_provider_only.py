"""T68: identities for provider teams that JC never sells (reserve sides).

A league's current roster can contain reserve sides (for example the Dutch
second tier's ``Jong`` teams). JC lists none of their fixtures, so no official
numeric team id exists and the ordinary creation route (which starts from an
official id) cannot name them. The owner approves each such team by name; this
module re-verifies the facts at apply time and creates the canonical team row.
Binding the provider id to that row stays a separate, reviewed alias entry, and
the history is moved by the ordinary scoped reconcile.

Nothing here reads odds or any market information.
"""

from __future__ import annotations

import csv
import io
import json
import re
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select, text

from zucai.db import get_engine, session_scope
from zucai.identity_binding import _utc
from zucai.identity_same_club import official_witness
from zucai.models import Team
from zucai.sources.apif_aliases import valid_identity_category
from zucai.sources.base import _atomic_write
from zucai.sources.teams import normalize_team

RECEIPTS = "state/audit/apif_provider_only_identities.csv"
FIELDS = (
    "competition_id",
    "provider_team_id",
    "project_team_id",
    "canonical",
    "created_utc",
    "decision_json",
)
ACTION = "create_provider_identity"
DOCUMENT_KEY = "provider_only_decisions"
# Positive reserve markers only. "II" and "B" are left out on purpose: they are also
# parts of first-team names (Willem II), so they can never make a side a reserve side.
RESERVE_LABEL = re.compile(r"\b(?:Jong|Castilla|Reserves?)\b|预备|青年队|二队", re.I)
WINDOW = timedelta(hours=3)
REVIEW_URL = r"https://github\.com/qw5563680/FOOTBALL/pull/[1-9][0-9]*(?:#[A-Za-z0-9_-]+)?"


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _formal_ids(facts):
    """Provider ids that already carry an approved binding in any league."""
    return {
        str(key)
        for entries in facts.get("formal", {}).values()
        for key, entry in entries.items()
        if isinstance(entry, dict) and entry.get("status") in {"approved", "auto_verified"}
    }


def _route(code):
    from zucai.league_registry import load_registry

    entry = load_registry()["providers"]["apif"].get(str(code))
    if not entry or entry["status"] != "verified":
        raise ValueError(f"赛事 {code} 没有已核实的目录路线")
    return entry["config"]


def candidates(facts, code):
    """Current-roster provider teams no official fixture ever involved, with the reasons
    that still block naming them. Read-only; the owner decides team by team."""
    code = str(code)
    config = _route(code)
    season = config.get("current_season")
    rows = [
        r
        for r in facts.get("apif", [])
        if str(r.get("competition_id")) == code and r.get("complete", True)
    ]
    roster = {str(r[side]) for r in rows if r.get("season") == season for side in ("home", "away")}
    witness = official_witness(facts)
    official = [
        r
        for r in facts.get("official", [])
        if str(r.get("competition_id")) == code
        and r.get("kickoff_utc")
        and not r.get("clock_conflict")
        and r.get("status") != "cancelled"
    ]
    teams = {
        str(r[f"{side}_source_id"])
        for r in official
        for side in ("home", "away")
        if r.get(f"{side}_source_id")
    }
    settled = {
        tid: witness[tid]["provider_team_id"]
        for tid in teams
        if witness.get(tid, {}).get("provider_team_id")
    }
    unsettled = sorted(teams - set(settled), key=int)
    claimed = set(settled.values())
    formal = _formal_ids(facts)
    # Kick-offs of official fixtures per provider id of one of their two teams.
    sold = defaultdict(list)
    for r in official:
        for side in ("home", "away"):
            pid = settled.get(str(r.get(f"{side}_source_id")))
            if pid:
                sold[pid].append(_utc(r["kickoff_utc"]))
    for stamps in sold.values():
        stamps.sort()
    output = []
    for pid in sorted(roster - claimed - formal, key=int):
        labels, fixtures, overlaps = set(), 0, []
        for r in rows:
            for side, other in (("home", "away"), ("away", "home")):
                if str(r[side]) != pid:
                    continue
                labels.add(r.get(f"{side}_name") or "")
                fixtures += 1
                if not r.get("kickoff_utc"):
                    continue
                # The opponent's own official fixture at this time would make this side
                # the other official team: JC did sell it, so it is not provider-only.
                stamps = sold.get(str(r[other]), [])
                when = _utc(r["kickoff_utc"])
                if bisect_left(stamps, when - WINDOW) != bisect_right(stamps, when + WINDOW):
                    overlaps.append(r["match_id"])
        blockers = []
        if not season:
            blockers.append("current_season_not_configured")
        if unsettled:
            blockers.append("official_teams_without_settled_provider_id")
        if not labels or any(not RESERVE_LABEL.search(label) for label in labels):
            blockers.append("label_without_reserve_marker")
        if overlaps:
            blockers.append("fixture_sold_by_jc")
        output.append(
            dict(
                action=ACTION,
                provider="apif",
                competition_id=code,
                league=config["league"],
                provider_team_id=pid,
                provider_labels=sorted(labels),
                fixtures=fixtures,
                official_teams=len(teams),
                official_fixtures=len(official),
                blockers=blockers,
                details=dict(
                    unsettled_official_teams=unsettled, fixtures_sold_by_jc=sorted(overlaps)
                ),
            )
        )
    return output


def _approval(decision):
    category = decision.get("category")
    if (
        decision.get("action") != ACTION
        or decision.get("status") != "approved"
        or decision.get("provider") != "apif"
        or decision.get("approved_by") != "qw5563680"
        or not isinstance(decision.get("review_url"), str)
        or not re.fullmatch(REVIEW_URL, decision["review_url"])
        or not re.fullmatch(r"[1-9][0-9]*", str(decision.get("provider_team_id", "")))
        or not re.fullmatch(r"[1-9][0-9]*", str(decision.get("competition_id", "")))
        or not isinstance(decision.get("canonical"), str)
        or normalize_team(decision["canonical"]) != decision["canonical"]
        or not decision["canonical"].strip()
        or decision["canonical"].casefold().startswith(("apif:", "fdorg:", "jc:", "fd:"))
        or not valid_identity_category(category)
        or not category.endswith("/reserve")
        or not isinstance(decision.get("provider_labels"), list)
        or not decision["provider_labels"]
    ):
        raise ValueError("来源独有球队的批准记录不完整或格式无效")
    if _utc(decision["approved_utc"]) > datetime.now(UTC):
        raise ValueError("批准时间不能晚于当前时间")


def verify(facts, decision):
    """Re-derive the candidate from the current archive; the approval must still fit it."""
    _approval(decision)
    code, pid = decision["competition_id"], decision["provider_team_id"]
    found = [c for c in candidates(facts, code) if c["provider_team_id"] == pid]
    if not found:
        raise ValueError(
            f"来源球队 {pid} 不是 {code} 当前赛季名单里的来源独有球队"
            "（已有绑定、已被官方球队见证，或不在名单内）"
        )
    current = found[0]
    if current["blockers"]:
        raise ValueError(f"来源球队 {pid} 不能建身份：{'、'.join(current['blockers'])}")
    if not set(decision["provider_labels"]) <= set(current["provider_labels"]):
        raise ValueError(f"来源球队 {pid} 的批准队名与当前原件不一致")
    return current


def _read_receipts(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        if reader.fieldnames != list(FIELDS):
            raise ValueError("来源独有球队审计表结构无效")
        rows = list(reader)
    if any(None in row or None in row.values() for row in rows):
        raise ValueError("来源独有球队审计行无效")
    keys = [(r["competition_id"], r["provider_team_id"]) for r in rows]
    if len(set(keys)) != len(rows) or len({r["project_team_id"] for r in rows}) != len(rows):
        raise ValueError("来源独有球队审计记录不唯一")
    return rows


def read_decisions(approvals):
    document = json.loads(Path(approvals).read_bytes())
    decisions = document.get(DOCUMENT_KEY, [])
    if not isinstance(decisions, list):
        raise ValueError("provider_only_decisions 结构无效")
    return decisions


def apply_provider_only(database, archive, approvals):
    """Create the approved canonical rows. Moves no match and binds no provider id."""
    from zucai.identity_binding_archive import load_archive

    database, archive = Path(database), Path(archive)
    if not database.is_file():
        raise ValueError("Creation requires an existing restored database")
    decisions = read_decisions(approvals)
    audit = database.parent / RECEIPTS
    before = audit.read_bytes() if audit.exists() else None
    receipts = _read_receipts(audit)
    known = {(r["competition_id"], r["provider_team_id"]): r for r in receipts}
    seen, pending = set(), []
    for decision in decisions:
        _approval(decision)
        key = (decision["competition_id"], decision["provider_team_id"])
        if key in seen:
            raise ValueError("来源独有球队的批准记录重复")
        seen.add(key)
        existing = known.get(key)
        if existing is not None:
            if json.loads(existing["decision_json"]) != decision:
                raise ValueError("已有的建身份记录与批准内容不同")
            continue
        pending.append(decision)
    names = [d["canonical"].casefold() for d in pending]
    if len(set(names)) != len(names):
        raise ValueError("两条批准使用了同一个规范队名")
    if pending:
        facts = load_archive(archive, leagues=sorted({d["competition_id"] for d in pending}))
        for decision in pending:
            verify(facts, decision)
    created = []
    engine = get_engine(database)
    try:
        with session_scope(engine) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            teams = list(session.scalars(select(Team)))
            ids = {t.id for t in teams}
            if any(int(r["project_team_id"]) not in ids for r in receipts):
                raise ValueError("已有建身份记录指向的球队不在库内")
            owned = {normalize_team(s).casefold() for t in teams for s in (t.name, *t.aliases)}
            for decision in pending:
                if decision["canonical"].casefold() in owned:
                    raise ValueError(f"规范队名已被占用，须先核对既有身份：{decision['canonical']}")
                target = Team(name=decision["canonical"], aliases=[])
                session.add(target)
                session.flush()
                owned.add(decision["canonical"].casefold())
                receipt = dict(
                    competition_id=decision["competition_id"],
                    provider_team_id=decision["provider_team_id"],
                    project_team_id=str(target.id),
                    canonical=target.name,
                    created_utc=datetime.now(UTC).isoformat(),
                    decision_json=_json(decision),
                )
                receipts.append(receipt)
                created.append(receipt)
            if created:
                stream = io.StringIO(newline="")
                writer = csv.DictWriter(stream, FIELDS)
                writer.writeheader()
                writer.writerows(receipts)
                audit.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write(audit, stream.getvalue().encode())
    except Exception:
        if before is None:
            audit.unlink(missing_ok=True)
        else:
            _atomic_write(audit, before)
        raise
    finally:
        engine.dispose()
    return dict(
        created=len(created),
        already_applied=len(decisions) - len(created),
        records=created,
        matches_changed=0,
        formal_bindings_changed=0,
    )
