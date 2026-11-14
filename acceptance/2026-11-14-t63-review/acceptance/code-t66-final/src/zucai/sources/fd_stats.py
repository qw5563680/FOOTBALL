"""Only nine FD division-season CSVs expose the T31 statistics vocabulary."""

import csv
import io
from dataclasses import dataclass
from datetime import datetime

from zucai.sources.apif_stats import PARSER_VERSION, number
from zucai.sources.football_data import csv_url, local_kickoff, verified_league

DIVISIONS = {"E0", "E1", "SP1", "I1", "D1", "F1", "F2", "N1", "P1"}
FIELDS = {
    "S": "shots",
    "ST": "shots_on_target",
    "C": "corners",
    "F": "fouls",
    "O": "offsides",
    "Y": "yellow_cards",
    "R": "red_cards",
    "FKC": "free_kicks_conceded",
}


@dataclass(frozen=True)
class FdStatistics:
    div_code: str
    season: str
    kickoff_utc: datetime
    home_raw: str
    away_raw: str
    row_number: int
    statistics: list[dict]


def parse_fd_statistics(raw, div, season, leagues):
    if div not in DIVISIONS or season is None:
        raise ValueError("技术统计仅支持九个主联赛季文件，不支持国别合并文件")
    csv_url(div=div, season=season)
    entry = verified_league(leagues, div)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252")
    reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
    header = reader.fieldnames or []
    named = [field for field in header if field]
    if not {"Div", "Date", "Time", "HomeTeam", "AwayTeam"} <= set(header) or len(named) != len(
        set(named)
    ):
        raise ValueError("主联赛 CSV 身份列缺失或重复")
    games, rejected = [], []
    for index, row in enumerate(reader, 2):
        if not any(row.values()):
            continue
        try:
            if row["Div"] != div or not row["HomeTeam"] or not row["AwayTeam"] or None in row:
                raise ValueError("CSV 行身份不完整或 Div 不符")
            kickoff = local_kickoff(row["Date"], row["Time"], entry["time_basis"])
            start = 2000 + int(season[:2])
            if (
                not datetime(start, 7, 1, tzinfo=kickoff.tzinfo)
                <= kickoff
                < datetime(start + 1, 7, 1, tzinfo=kickoff.tzinfo)
            ):
                raise ValueError("CSV 开球不在指定赛季")
            statistics = []
            for side, prefix in (("home", "H"), ("away", "A")):
                for suffix, key in FIELDS.items():
                    column = prefix + suffix
                    value, reason = number(row.get(column))
                    if column not in header:
                        reason = "column_absent"
                    statistics.append(
                        dict(
                            provider="fd",
                            endpoint="csv",
                            provider_team_id=None,
                            side=side,
                            period_basis="90",
                            extra_time_included=False,
                            eligible_90=True,
                            parser_version=PARSER_VERSION,
                            record_key=f"{side}:{key}",
                            stat_key=key,
                            raw_value=row.get(column),
                            value=value,
                            unit="count",
                            missing_reason=reason,
                        )
                    )
            games.append(
                FdStatistics(
                    div, season, kickoff, row["HomeTeam"], row["AwayTeam"], index, statistics
                )
            )
        except (ValueError, TypeError) as exc:
            rejected.append({"row_number": index, "reason": str(exc)})
    return games, rejected
