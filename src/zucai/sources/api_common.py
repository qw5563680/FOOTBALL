"""Provider identity and regulation-time scores shared by the fixture APIs."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class ApiGame:
    provider: str
    provider_match_id: int
    competition_code: str
    league: str
    kickoff_utc: datetime
    status: str
    home_raw: str
    away_raw: str
    home_provider_id: int
    away_provider_id: int
    full: tuple[int, int] | None
    half: tuple[int, int] | None
    regular_time_basis: Literal["regular", "derived", "missing"]
