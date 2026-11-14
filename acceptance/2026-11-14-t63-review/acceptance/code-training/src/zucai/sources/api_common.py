"""Provider identity and regulation-time scores shared by the fixture APIs."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


class LocalValidationError(ValueError):
    """Project-authored diagnostic; summary must be fixed text, never external data."""

    def __init__(self, message, *, summary=None):
        super().__init__(message)
        self.summary = message if summary is None else summary


class CredentialError(ValueError):
    """Safe credential state without any value, length or character details."""

    def __init__(self, variable, *, missing):
        self.reason = "环境凭据变量未设置/为空" if missing else "环境凭据含非法字符"
        super().__init__(f"请设置有效的 {variable}（{self.reason}）")


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
