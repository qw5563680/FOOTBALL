"""UTC season boundaries for evaluation, independent of publisher labels."""

import re

import pandas as pd

from zucai.predict.base import utc

CALENDAR_LEAGUES = {
    "日职",
    "瑞超",
    "挪超",
    "芬超",
    "美职",
    "巴甲",
    "JPN",
    "SWE",
    "NOR",
    "FIN",
    "USA",
    "BRA",
}


def season_for(instant, league):
    stamp = utc(instant)
    if league in CALENDAR_LEAGUES:
        return str(stamp.year)
    year = stamp.year - (stamp.month < 7)
    return f"{year % 100:02d}{(year + 1) % 100:02d}"


def season_bounds(season, league):
    season = str(season)
    if not re.fullmatch(r"\d{4}", season):
        raise ValueError("赛季须为四位数字")
    if league in CALENDAR_LEAGUES:
        year, month = int(season), 1
        if not 1900 <= year < 2100:
            raise ValueError("自然年联赛请使用完整年份，例如2025")
    else:
        year, month = 2000 + int(season[:2]), 7
        if (year + 1) % 100 != int(season[2:]):
            raise ValueError("跨年赛季须连续，例如2425")
    return (
        pd.Timestamp(year=year, month=month, day=1, tz="UTC"),
        pd.Timestamp(year=year + 1, month=month, day=1, tz="UTC"),
    )


def parse_seasons(value, league):
    if value is None:
        return None
    items = value.split(",") if isinstance(value, str) else list(value)
    if not items or len(set(items)) != len(items):
        raise ValueError("赛季不能为空或重复")
    return sorted(items, key=lambda item: season_bounds(item, league)[0])


def next_season(season, league):
    return season_for(season_bounds(season, league)[1], league)


def latest_complete_seasons(as_of, league):
    start, _ = season_bounds(season_for(as_of, league), league)
    return [season_for(start - pd.DateOffset(years=i), league) for i in (2, 1)]
