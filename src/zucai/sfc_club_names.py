"""Owner-approved club short names printed by the 14-match lottery source.

The lottery page abbreviates club names its own way (达姆施 for 达姆施塔). A fixture JC does
not sell has no ID bridge, so such a row can only reach its stored fixture through an
approved name. An entry is keyed by league and the source's team ID; the printed label
must also agree, and the entry never becomes a general alias: it is read only when a
lottery row is mapped. Unapproved or malformed entries are ignored or refused.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from zucai.sources.football_data import CONFIG_ROOT
from zucai.sources.teams import normalize_team

PATH = CONFIG_ROOT / "team_aliases/sfc_clubs.json"
REVIEW = r"https://github\.com/qw5563680/FOOTBALL/pull/[1-9][0-9]*"


def load(path=None):
    document = json.loads(Path(path or PATH).read_bytes())
    leagues = document.get("leagues") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or document.get("version") != 1
        or not isinstance(leagues, dict)
    ):
        raise ValueError("胜负彩俱乐部简称表结构无效")
    table = {}
    for league, entries in leagues.items():
        if not isinstance(entries, dict):
            raise ValueError(f"胜负彩俱乐部简称表联赛条目无效：{league}")
        for provider_id, entry in entries.items():
            if (
                not isinstance(entry, dict)
                or not re.fullmatch(r"[1-9][0-9]*", str(provider_id))
                or any(
                    not isinstance(entry.get(key), str) or not entry[key].strip()
                    for key in ("label", "canonical", "status", "approved_by", "approved_utc")
                )
                or normalize_team(entry["canonical"]) != entry["canonical"]
                or entry["label"] == entry["canonical"]
            ):
                raise ValueError(f"胜负彩俱乐部简称条目无效：{league}/{provider_id}")
            if entry["status"] != "approved":
                continue
            if entry["approved_by"] != "qw5563680" or not re.fullmatch(
                REVIEW, str(entry.get("approval_pr", ""))
            ):
                raise ValueError(f"胜负彩俱乐部简称缺少所有者批准记录：{league}/{provider_id}")
            table[league, str(provider_id)] = entry
    return table


@lru_cache(maxsize=1)
def _table():
    return load()


def club_name(league, provider_id, label):
    """The approved stored name for one printed short name, or None.

    The source's team ID and the label as printed must both agree with the entry.
    """
    if provider_id is None:
        return None
    entry = _table().get((league, str(provider_id)))
    if entry is None or entry["label"] != label:
        return None
    return entry["canonical"]
