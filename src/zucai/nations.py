"""Verified national team identities, scoped to the men's UEFA Nations League."""

import json
from datetime import timedelta
from functools import lru_cache

from zucai.sources.base import _atomic_write
from zucai.sources.football_data import CONFIG_ROOT, read_json

LEAGUE = "欧国联"


@lru_cache(maxsize=1)
def identities():
    return read_json(CONFIG_ROOT / "api_football/nations.json")


def sfc_team(provider_id, label):
    """An ID plus the original printed label must agree before expanding a short name."""
    entry = identities()["sfc"].get(provider_id)
    if entry is None:
        return label
    if label not in (entry["label"], entry["canonical"]):
        raise ValueError("欧国联球队编号与期次原队名冲突")
    return entry["canonical"]


def collect_editions(now, state, common, *, fetch):
    """One verified season page per edition, refresh active fixtures once per UTC day."""
    config = read_json(CONFIG_ROOT / "api_football/leagues.json")["5"]
    seasons = sorted(config["seasons_available"], reverse=True)
    cursor = state / "nations_league.json"
    receipts = read_json(cursor) if cursor.exists() else {}
    today = now.date().isoformat()
    for season in seasons:
        previous = receipts.get(str(season))
        if previous and (season != seasons[0] or previous["day"] == today):
            continue
        result = fetch(
            league_id="5",
            seasons=[season],
            assume=timedelta(hours=6),
            quota_path=state / "api_football_quota.json",
            **common,
        )
        (capture,) = result["files"]
        if not capture["outcomes"]:
            raise ValueError(f"欧国联 {season} 没有通过核验的比赛")
        receipts[str(season)] = dict(
            day=today,
            raw_sha256=capture["raw_sha256"],
            accepted=len(capture["outcomes"]),
            rejected=result["rejected_rows"],
            conflicts=result["suspected_duplicates"],
        )
        state.mkdir(parents=True, exist_ok=True)
        _atomic_write(cursor, (json.dumps(receipts, sort_keys=True) + "\n").encode())
        yield result
