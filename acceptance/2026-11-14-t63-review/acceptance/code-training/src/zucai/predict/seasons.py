"""UTC season boundaries for evaluation, independent of publisher labels."""

import re
from dataclasses import dataclass

import pandas as pd

from zucai.predict.base import utc

CALENDAR_LEAGUES = {
    "瑞超",
    "挪超",
    "芬超",
    "美职",
    "巴甲",
    "SWE",
    "NOR",
    "FIN",
    "USA",
    "BRA",
}


@dataclass(frozen=True)
class SeasonRule:
    """A UTC segment: recurring yearly boundary or a fixed transition label.

    Rules are ordered by effective_from; the next rule ends the current segment.
    A fixed label requires finite bounds on both sides.
    """

    effective_from: str | None
    start_month: int = 1
    label: str | None = None
    complete_eligible: bool = True


_CALENDAR_RULES = (SeasonRule(None),)
_CROSS_YEAR_RULES = (SeasonRule(None, start_month=7),)
_JPN_RULES = (
    SeasonRule(None),
    SeasonRule("2026-01-01T00:00Z", label="2026T", complete_eligible=False),
    SeasonRule("2026-08-01T00:00Z", start_month=8),
)
LEAGUE_SEASON_RULES = {
    "日职": _JPN_RULES,
    "JPN": _JPN_RULES,
    "韩职": _CALENDAR_RULES,
    "澳超": _CROSS_YEAR_RULES,
}


def _rules(league):
    return LEAGUE_SEASON_RULES.get(
        league, _CALENDAR_RULES if league in CALENDAR_LEAGUES else _CROSS_YEAR_RULES
    )


def _rule_for(stamp, league):
    for rule in reversed(_rules(league)):
        if rule.effective_from is None or utc(rule.effective_from) <= stamp:
            return rule
    raise ValueError(f"{league} 的赛季规则未覆盖 {stamp}")


def season_for(instant, league):
    stamp = utc(instant)
    rule = _rule_for(stamp, league)
    if rule.label is not None:
        return rule.label
    if rule.start_month == 1:
        return str(stamp.year)
    year = stamp.year - (stamp.month < rule.start_month)
    return f"{year % 100:02d}{(year + 1) % 100:02d}"


def season_bounds(season, league):
    season = str(season)
    rules = _rules(league)
    for index, rule in enumerate(rules):
        segment_low = utc(rule.effective_from) if rule.effective_from is not None else None
        segment_high = utc(rules[index + 1].effective_from) if index + 1 < len(rules) else None
        if rule.label is not None:
            if season == rule.label and segment_low is not None and segment_high is not None:
                return segment_low, segment_high
            continue
        if not re.fullmatch(r"\d{4}", season):
            continue
        if rule.start_month == 1:
            year = int(season)
            if not 1900 <= year < 2100:
                continue
        else:
            year = 2000 + int(season[:2])
            if (year + 1) % 100 != int(season[2:]):
                continue
        low = pd.Timestamp(year=year, month=rule.start_month, day=1, tz="UTC")
        high = pd.Timestamp(year=year + 1, month=rule.start_month, day=1, tz="UTC")
        if segment_low is not None:
            low = max(low, segment_low)
        if segment_high is not None:
            high = min(high, segment_high)
        if low < high:
            return low, high
    raise ValueError(
        f"{league} 的赛季标签无效：{season}；请使用该联赛适用的自然年、连续跨年或过渡季标签"
    )


def season_is_complete_eligible(season, league):
    """Transition competitions remain ineligible even after their end date."""
    low, _ = season_bounds(season, league)
    return _rule_for(low, league).complete_eligible


def parse_seasons(value, league):
    if value is None:
        return None
    items = value.split(",") if isinstance(value, str) else list(value)
    if not items or len(set(items)) != len(items):
        raise ValueError("赛季不能为空或重复")
    return sorted(items, key=lambda item: season_bounds(item, league)[0])


def next_season(season, league):
    return season_for(season_bounds(season, league)[1], league)


def latest_complete_seasons(as_of, league, count=2):
    if type(count) is not int or count <= 0:
        raise ValueError("完整赛季数量须为正整数")
    cursor, _ = season_bounds(season_for(as_of, league), league)
    result = []
    while len(result) < count:
        season = season_for(cursor - pd.Timedelta(nanoseconds=1), league)
        cursor, _ = season_bounds(season, league)
        if season_is_complete_eligible(season, league):
            result.append(season)
    return list(reversed(result))
