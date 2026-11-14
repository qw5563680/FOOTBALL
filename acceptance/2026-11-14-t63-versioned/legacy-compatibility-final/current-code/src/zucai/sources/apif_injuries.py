"""Pure injuries page parsing and an adapter to the existing paced request route."""

import copy
import json
from dataclasses import dataclass
from datetime import datetime

from zucai.sources.apif_stats import envelope, identifier, instant
from zucai.sources.base import Source

PARSER_VERSION = "t38-injuries-v1"
INJURY_TYPES = ("Missing Fixture", "Questionable")


@dataclass(frozen=True)
class Injury:
    provider_fixture_id: str
    provider_league_id: int
    provider_season: int
    provider_team_id: int | None
    player_id: int | None
    player_name: str | None
    injury_type: str
    reason: str | None
    fixture_kickoff_utc: datetime
    captured_utc: datetime
    captured_day: str
    available_utc: datetime | None
    availability_basis: str
    captured_before_kickoff: bool
    raw_value: dict
    parser_version: str = PARSER_VERSION


def _optional_text(value):
    if value is not None and not isinstance(value, str):
        raise ValueError("伤停文本字段无效")
    return value


def parse_injuries(raw, *, league_id, season, captured_utc, daily=False, rejected=None):
    """Validate exact scope; keep provider IDs and the actual capture clock unchanged."""
    identifier(league_id)
    identifier(season)
    captured = instant(captured_utc)
    doc = json.loads(raw) if isinstance(raw, (bytes, str)) else raw
    source_rows = envelope(doc, "injuries", {"league": league_id, "season": season})
    if set(doc["parameters"]) != {"league", "season"}:
        raise ValueError("伤停页范围必须仅为 league + season")
    result = []
    for number, row in enumerate(source_rows, 1):
        fixture_id = None
        try:
            fixture, league, player, team = (
                row["fixture"],
                row["league"],
                row["player"],
                row["team"],
            )
            fixture_id = str(identifier(fixture["id"]))
            if identifier(league["id"]) != league_id or identifier(league["season"]) != season:
                raise ValueError("伤停行联赛季与请求不符")
            kickoff = instant(fixture["date"])
            if "timestamp" in fixture and fixture["timestamp"] != kickoff.timestamp():
                raise ValueError("伤停 fixture 开球时间字段矛盾")
            injury_type = player["type"]
            if not isinstance(injury_type, str) or not injury_type.strip():
                raise ValueError("伤停类型缺失")
            before = captured < kickoff
            basis = "captured" if daily and before else "backfilled"
            result.append(
                Injury(
                    provider_fixture_id=fixture_id,
                    provider_league_id=league_id,
                    provider_season=season,
                    provider_team_id=identifier(team.get("id"), nullable=True),
                    player_id=identifier(player.get("id"), nullable=True),
                    player_name=_optional_text(player.get("name")),
                    injury_type=injury_type,
                    reason=_optional_text(player.get("reason")),
                    fixture_kickoff_utc=kickoff,
                    captured_utc=captured,
                    captured_day=captured.date().isoformat(),
                    available_utc=captured if basis == "captured" else None,
                    availability_basis=basis,
                    captured_before_kickoff=before,
                    raw_value=copy.deepcopy(row),
                )
            )
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            if rejected is None:
                raise ValueError(f"伤停第 {number} 行无效: {exc}") from exc
            rejected.append(
                dict(row_number=number, provider_fixture_id=fixture_id, reason=str(exc))
            )
    return result


class ApifInjuries(Source[Injury]):
    """Acquisition uses StatsClient.fetch once; parsing never performs I/O."""

    name = "api-football"

    def __init__(self, *, league_id, season, captured_utc, daily=False, client=None):
        self.league_id, self.season = league_id, season
        self.captured_utc, self.daily, self.client = captured_utc, daily, client

    def fetch_raw(self, **kwargs):
        if kwargs or self.client is None:
            raise ValueError("伤停抓取需要既有 StatsClient 和显式联赛季")
        return self.client.fetch("injuries", league_id=self.league_id, season=self.season)

    def parse(self, raw):
        return parse_injuries(
            raw,
            league_id=self.league_id,
            season=self.season,
            captured_utc=self.captured_utc,
            daily=self.daily,
        )
