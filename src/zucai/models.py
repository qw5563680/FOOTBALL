"""共享的比赛、快照和预测存储模型。"""

from datetime import UTC, datetime

from sqlalchemy import JSON, CheckConstraint, Float, ForeignKey, Index, String, UniqueConstraint
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
    sale_end_utc: Mapped[str] = mapped_column(UTCString)


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
