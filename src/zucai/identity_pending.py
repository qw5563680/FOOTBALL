"""Teams whose history is known to sit under another stored identity.

A reviewed list names pairs of stored team names that are the same club (for example a
JC Chinese name and a football-data.co.uk English name) but are not merged yet. While
the other identity still holds matches of a league, the model reads only part of that
club's history there, so the 14-match report marks those fixtures 「身份待核」 and keeps
them out of the pick-nine candidates. The mark clears by itself once the identities are
merged: nothing here renames, merges or re-keys anything, and frozen forecasts are not
touched.

Nothing here reads odds or any market information.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import or_, select

from zucai.models import Match, Team
from zucai.sources.football_data import CONFIG_ROOT
from zucai.sources.teams import normalize_team

PATH = CONFIG_ROOT / "identity_reviews/pending-identities.json"
SIDES = (("home", "主队"), ("away", "客队"))


def load(path=None):
    """The reviewed list as {canonical name: other stored name}; duplicates are refused."""
    document = json.loads(Path(path or PATH).read_bytes())
    teams = document.get("teams") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or document.get("version") != 1
        or not isinstance(teams, list)
    ):
        raise ValueError("身份待核清单结构无效")
    pairs = {}
    for entry in teams:
        names = [
            entry.get(key) if isinstance(entry, dict) else None for key in ("canonical", "other")
        ]
        if any(not isinstance(name, str) or not name.strip() for name in names):
            raise ValueError("身份待核清单条目须同时给出 canonical 与 other")
        canonical, other = names
        if normalize_team(canonical) != canonical or canonical == other or canonical in pairs:
            raise ValueError(f"身份待核清单条目无效或重复：{canonical}")
        pairs[canonical] = other
    return pairs


def pending_sides(session, pairs, league, home, away):
    """Which sides of one fixture still have history of this league under the other name."""
    found = {}
    for side, name in (("home", home), ("away", away)):
        other = pairs.get(name)
        if other is None:
            continue
        team = session.scalar(select(Team).where(Team.name == other))
        if team is None:
            continue
        held = session.scalar(
            select(Match.match_id)
            .where(
                Match.league == league,
                or_(Match.home_team_id == team.id, Match.away_team_id == team.id),
            )
            .limit(1)
        )
        if held is not None:
            found[side] = dict(name=name, other=other)
    return found or None


def identity_pending(row):
    value = row.get("identity_pending")
    return isinstance(value, dict) and bool(value)


def pending_label(row):
    """Compact inline label; callers escape it for their output format."""
    if not identity_pending(row):
        return ""
    parts = [
        f"{label} {row['identity_pending'][side]['name']}"
        f"（历史仍在「{row['identity_pending'][side]['other']}」名下）"
        for side, label in SIDES
        if side in row["identity_pending"]
    ]
    return "身份待核：" + "、".join(parts)


def pending_footnote(rows):
    if not any(identity_pending(row) for row in rows):
        return ""
    return (
        "身份待核说明：该队的历史比赛还挂在另一个队名下、尚未合并，模型只读到一部分；"
        "概率仅供研究，不进入任九模型单选或最高项排序。"
    )
