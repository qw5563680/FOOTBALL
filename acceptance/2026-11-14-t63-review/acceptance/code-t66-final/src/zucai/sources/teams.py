"""保守的队名匹配与人工核对清单。"""

import csv
import re
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from zucai.models import Team

_TEAM_SUFFIX = re.compile(r"(?:\s*队|(?:\s+|(?<=[\u3400-\u9fff]))F\.?C\.?)+$", re.I)


def normalize_team(name: str) -> str:
    normalized = " ".join(unicodedata.normalize("NFKC", name).split())
    normalized = _TEAM_SUFFIX.sub("", normalized).rstrip()
    if not normalized or normalized.casefold() in {"fc", "f.c."}:
        raise ValueError("队名不能为空")
    return normalized


def _record_unknown(source: str, raw_name: str, normalized_name: str) -> None:
    path = Path("data/unmatched_teams.csv")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8", newline="") as audit:
        audit.seek(0)
        reader = csv.DictReader(audit)
        if any(row["normalized"].casefold() == normalized_name.casefold() for row in reader):
            return
        writer = csv.DictWriter(
            audit, fieldnames=["source", "raw_name", "normalized", "created_utc"]
        )
        if audit.tell() == 0:
            writer.writeheader()
        writer.writerow(
            {
                "source": source,
                "raw_name": raw_name,
                "normalized": normalized_name,
                "created_utc": datetime.now(UTC)
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z"),
            }
        )


def upsert_team(session: Session, name: str, source: str = "unknown") -> Team:
    canonical = normalize_team(name)
    team = session.scalar(select(Team).where(Team.name == canonical))
    if team is None:
        key = canonical.casefold()
        candidates = [
            entry
            for entry in session.scalars(select(Team))
            if entry.name.casefold() == key
            or any(normalize_team(alias).casefold() == key for alias in entry.aliases)
        ]
        if len(candidates) > 1:
            raise ValueError(f"队名别名存在歧义，请人工核对 {name!r}")
        if candidates:
            team = candidates[0]
        else:
            session.execute(
                insert(Team)
                .values(name=canonical, aliases=[])
                .on_conflict_do_nothing(index_elements=[Team.name])
            )
            team = session.scalars(select(Team).where(Team.name == canonical)).one()
            _record_unknown(source, name, canonical)
    if name != team.name and name not in team.aliases:
        team.aliases.append(name)
    session.flush()
    return team
