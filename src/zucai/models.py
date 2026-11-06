"""共享的比赛、快照和预测存储模型。"""

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class Base(DeclarativeBase):
    pass


class UTCString(TypeDecorator):
    """Write timezone-aware instants as lexically sortable UTC strings."""

    impl = String(27)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        instant = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(instant, datetime) or instant.utcoffset() is None:
            raise ValueError("时间必须包含明确的时区")
        return instant.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(collation="NOCASE"), unique=True)
    aliases: Mapped[list[str]] = mapped_column(MutableList.as_mutable(JSON), default=list)


class Match(Base):
    __tablename__ = "matches"
    __table_args__ = (
        CheckConstraint(
            "length(source) > 0 AND substr(match_id, 1, length(source)+1) = "
            "source || ':' AND length(match_id) > length(source)+1"
        ),
        CheckConstraint("home_team_id <> away_team_id"),
        CheckConstraint("status IN ('scheduled', 'finished', 'cancelled')"),
        Index("ix_matches_league_kickoff", "league", "kickoff_utc"),
    )

    match_id: Mapped[str] = mapped_column(primary_key=True)
    source: Mapped[str]
    league: Mapped[str]
    home_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    away_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    kickoff_utc: Mapped[str] = mapped_column(UTCString)
    jc_num: Mapped[str | None]
    status: Mapped[str] = mapped_column(default="scheduled")
    result_checked_utc: Mapped[str | None] = mapped_column(UTCString)
    result_provider: Mapped[str | None]
    counterpart_id: Mapped[str | None]


class Odds(Base):
    __tablename__ = "odds"
    __table_args__ = (
        UniqueConstraint("match_id", "pool", "line", "selection", "snapshot_utc"),
        CheckConstraint("price > 0"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[str] = mapped_column(ForeignKey("matches.match_id"))
    pool: Mapped[str]
    line: Mapped[float] = mapped_column(Float, default=0.0)
    selection: Mapped[str]
    price: Mapped[float]
    snapshot_utc: Mapped[str] = mapped_column(UTCString)


class ClosingOdds(Base):
    """Publisher closing prices with no independently known quote timestamp."""

    __tablename__ = "closing_odds"
    __table_args__ = (
        CheckConstraint("pool = 'had'"),
        CheckConstraint("basis = 'publisher_closing'"),
        CheckConstraint("bookmaker IN ('PS', 'B365', 'Avg', 'Max')"),
        CheckConstraint("h > 1 AND d > 1 AND a > 1"),
    )

    match_id: Mapped[str] = mapped_column(ForeignKey("matches.match_id"), primary_key=True)
    bookmaker: Mapped[str] = mapped_column(primary_key=True)
    pool: Mapped[str] = mapped_column(primary_key=True, default="had")
    h: Mapped[float]
    d: Mapped[float]
    a: Mapped[float]
    basis: Mapped[str] = mapped_column(default="publisher_closing")


class Result(Base):
    __tablename__ = "results"
    __table_args__ = (
        CheckConstraint("home_goals >= 0 AND away_goals >= 0"),
        CheckConstraint("ht_home_goals >= 0 AND ht_away_goals >= 0"),
    )

    match_id: Mapped[str] = mapped_column(ForeignKey("matches.match_id"), primary_key=True)
    home_goals: Mapped[int]
    away_goals: Mapped[int]
    ht_home_goals: Mapped[int | None]
    ht_away_goals: Mapped[int | None]
    available_utc: Mapped[str | None] = mapped_column(UTCString)
    available_source: Mapped[str | None]


class SfcIssue(Base):
    __tablename__ = "sfc_issues"

    issue_no: Mapped[str] = mapped_column(primary_key=True)
    sale_end_utc: Mapped[str | None] = mapped_column(UTCString)
    observed_utc: Mapped[str | None] = mapped_column(UTCString)
    source_url: Mapped[str | None]
    raw_sha256: Mapped[str | None]
    date_basis: Mapped[str | None]
    snapshot: Mapped[list[dict] | None] = mapped_column(JSON)


class SfcIssueMatch(Base):
    __tablename__ = "sfc_issue_matches"
    __table_args__ = (
        CheckConstraint("seq BETWEEN 1 AND 14"),
        UniqueConstraint("issue_no", "match_id"),
    )

    issue_no: Mapped[str] = mapped_column(ForeignKey("sfc_issues.issue_no"), primary_key=True)
    seq: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[str] = mapped_column(ForeignKey("matches.match_id"))


class Prediction(Base):
    __tablename__ = "predictions"
    __table_args__ = (
        UniqueConstraint("match_id", "model_name", "model_version", "created_utc"),
        CheckConstraint(
            "p_home BETWEEN 0 AND 1 AND p_draw BETWEEN 0 AND 1 AND p_away BETWEEN 0 AND 1"
        ),
        CheckConstraint("abs(p_home + p_draw + p_away - 1) < 0.000001"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[str | None]
    mode: Mapped[str | None]
    as_of_utc: Mapped[str | None] = mapped_column(UTCString)
    match_id: Mapped[str] = mapped_column(ForeignKey("matches.match_id"))
    model_name: Mapped[str]
    model_version: Mapped[str]
    p_home: Mapped[float]
    p_draw: Mapped[float]
    p_away: Mapped[float]
    score_matrix: Mapped[list[list[float]]] = mapped_column(JSON)
    created_utc: Mapped[str] = mapped_column(UTCString, default=lambda: datetime.now(UTC))


class TechnicalProvenance:
    """Append-only source observations; no inferred historical availability clock."""

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str]
    provider_fixture_id: Mapped[str]
    endpoint: Mapped[str]
    record_key: Mapped[str]
    match_id: Mapped[str] = mapped_column(ForeignKey("matches.match_id"))
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    provider_team_id: Mapped[int | None]
    side: Mapped[str]
    period_basis: Mapped[str]
    extra_time_included: Mapped[bool]
    eligible_90: Mapped[bool]
    captured_utc: Mapped[str] = mapped_column(UTCString)
    available_utc: Mapped[str | None] = mapped_column(UTCString)
    availability_basis: Mapped[str]
    raw_sha256: Mapped[str]
    raw_location: Mapped[str]
    parser_version: Mapped[str]
    missing_reason: Mapped[str | None]
    version: Mapped[int]


def _technical_constraints():
    return (
        UniqueConstraint("provider", "provider_fixture_id", "endpoint", "record_key", "version"),
        UniqueConstraint("provider", "provider_fixture_id", "endpoint", "record_key", "raw_sha256"),
        CheckConstraint("provider IN ('fd', 'apif')"),
        CheckConstraint("side IN ('home', 'away')"),
        CheckConstraint("version > 0 AND length(raw_sha256) = 64"),
        CheckConstraint("period_basis IN ('90', 'incl_extra_time', 'unknown')"),
        CheckConstraint("extra_time_included = (period_basis = 'incl_extra_time')"),
        CheckConstraint("eligible_90 = (period_basis = '90')"),
        CheckConstraint("availability_basis IN ('backfilled', 'captured')"),
        CheckConstraint("availability_basis <> 'backfilled' OR available_utc IS NULL"),
    )


class MatchStat(TechnicalProvenance, Base):
    __tablename__ = "match_stats"
    __table_args__ = _technical_constraints()

    stat_key: Mapped[str]
    raw_value: Mapped[object | None] = mapped_column(JSON)
    value: Mapped[float | None]
    unit: Mapped[str]


class MatchEvent(TechnicalProvenance, Base):
    __tablename__ = "match_events"
    __table_args__ = _technical_constraints()

    event_type: Mapped[str | None]
    detail: Mapped[str | None]
    elapsed: Mapped[int | None]
    extra: Mapped[int | None]
    player_id: Mapped[int | None]
    assist_id: Mapped[int | None]
    raw_value: Mapped[dict] = mapped_column(JSON)
    value: Mapped[dict] = mapped_column(JSON)


class MatchLineup(TechnicalProvenance, Base):
    __tablename__ = "match_lineups"
    __table_args__ = _technical_constraints()

    role: Mapped[str]
    player_id: Mapped[int | None]
    formation: Mapped[str | None]
    minutes: Mapped[float | None]
    rating: Mapped[float | None]
    position: Mapped[str | None]
    captured_before_kickoff: Mapped[bool]
    raw_value: Mapped[dict] = mapped_column(JSON)
    value: Mapped[dict] = mapped_column(JSON)


class MatchInjury(Base):
    """Content versions with immutable first provenance and advancing last observation."""

    __tablename__ = "match_injuries"
    __table_args__ = (
        UniqueConstraint("provider", "provider_fixture_id", "record_key", "version"),
        CheckConstraint("length(content_sha256) = 64"),
        CheckConstraint("last_captured_utc >= captured_utc"),
        CheckConstraint("provider = 'apif' AND endpoint = 'injuries'"),
        CheckConstraint("version > 0 AND length(raw_sha256) = 64"),
        CheckConstraint("captured_day = substr(captured_utc, 1, 10)"),
        CheckConstraint("captured_before_kickoff = (captured_utc < fixture_kickoff_utc)"),
        CheckConstraint("availability_basis IN ('backfilled', 'captured')"),
        CheckConstraint("availability_basis <> 'backfilled' OR available_utc IS NULL"),
        CheckConstraint(
            "availability_basis <> 'captured' OR "
            "(available_utc IS NOT NULL AND available_utc = captured_utc "
            "AND captured_before_kickoff = 1)"
        ),
        CheckConstraint("side IS NULL OR side IN ('home', 'away')"),
        CheckConstraint(
            "(match_id IS NULL AND team_id IS NULL AND side IS NULL) OR "
            "(match_id IS NOT NULL AND team_id IS NOT NULL AND side IS NOT NULL)"
        ),
        Index("ix_match_injuries_scope", "league", "provider_season", "captured_utc"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(default="apif")
    endpoint: Mapped[str] = mapped_column(default="injuries")
    league: Mapped[str]
    provider_season: Mapped[int]
    provider_fixture_id: Mapped[str]
    match_id: Mapped[str | None] = mapped_column(ForeignKey("matches.match_id"))
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"))
    provider_team_id: Mapped[int | None]
    side: Mapped[str | None]
    player_id: Mapped[int | None]
    player_name: Mapped[str | None]
    injury_type: Mapped[str]
    reason: Mapped[str | None]
    fixture_kickoff_utc: Mapped[str] = mapped_column(UTCString)
    captured_utc: Mapped[str] = mapped_column(UTCString)
    captured_day: Mapped[str]
    last_captured_utc: Mapped[str] = mapped_column(UTCString)
    record_key: Mapped[str]
    content_sha256: Mapped[str]
    available_utc: Mapped[str | None] = mapped_column(UTCString)
    availability_basis: Mapped[str]
    captured_before_kickoff: Mapped[bool]
    raw_sha256: Mapped[str]
    raw_location: Mapped[str]
    parser_version: Mapped[str]
    version: Mapped[int]
    raw_value: Mapped[dict] = mapped_column(JSON)


class ApifFixtureIndex(Base):
    __tablename__ = "apif_fixture_index"
    __table_args__ = (
        UniqueConstraint("match_id"),
        CheckConstraint("home_team_id <> away_team_id"),
        CheckConstraint("home_provider_id <> away_provider_id"),
        CheckConstraint("provider = 'apif' AND endpoint = 'fixtures' AND version = 1"),
        CheckConstraint("length(raw_sha256) = 64"),
    )

    provider_fixture_id: Mapped[str] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(default="apif")
    endpoint: Mapped[str] = mapped_column(default="fixtures")
    match_id: Mapped[str] = mapped_column(ForeignKey("matches.match_id"))
    league: Mapped[str]
    provider_league_id: Mapped[int]
    season: Mapped[int]
    kickoff_utc: Mapped[str] = mapped_column(UTCString)
    home_provider_id: Mapped[int]
    away_provider_id: Mapped[int]
    home_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    away_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    captured_utc: Mapped[str] = mapped_column(UTCString)
    available_utc: Mapped[str | None] = mapped_column(UTCString)
    availability_basis: Mapped[str] = mapped_column(default="backfilled")
    raw_sha256: Mapped[str]
    raw_location: Mapped[str]
    parser_version: Mapped[str]
    evidence: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(default=1)


class FdStatsLink(Base):
    """Independent FD statistics attachment; never replaces a source counterpart."""

    __tablename__ = "fd_stats_links"
    __table_args__ = (
        UniqueConstraint("match_id"),
        CheckConstraint("home_team_id <> away_team_id"),
        CheckConstraint("length(raw_sha256) = 64 AND length(aliases_sha256) = 64"),
    )

    provider_fixture_id: Mapped[str] = mapped_column(primary_key=True)
    match_id: Mapped[str] = mapped_column(ForeignKey("matches.match_id"))
    league: Mapped[str]
    div_code: Mapped[str]
    season: Mapped[str]
    kickoff_utc: Mapped[str] = mapped_column(UTCString)
    home_raw: Mapped[str]
    away_raw: Mapped[str]
    home_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    away_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    captured_utc: Mapped[str] = mapped_column(UTCString)
    raw_sha256: Mapped[str]
    raw_location: Mapped[str]
    aliases_sha256: Mapped[str]
    evidence: Mapped[dict] = mapped_column(JSON)
