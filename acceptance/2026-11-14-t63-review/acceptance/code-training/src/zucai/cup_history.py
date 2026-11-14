"""Read-only T29 coverage; counts describe stored identities, not training samples."""

import re
from collections import defaultdict

from zucai.sources.fd_verify import _utc

CUPS = {"欧冠", "欧罗巴", "欧协联"}
JC_SINCE = "2025-07-01T00:00:00+00:00"
PROVISIONAL = re.compile(r"(apif|fdorg):team:([1-9][0-9]*)")


def team_games(matches):
    games = defaultdict(list)
    for match in matches:
        for key in {match["home_team_id"], match["away_team_id"]}:
            games[key].append(match)
    return games


def jc_cup_teams(matches):
    return {
        match[side + "_team_id"]
        for match in matches
        if match["source"] == "jc"
        and match["league"] in CUPS
        and _utc(match["kickoff_utc"]) >= _utc(JC_SINCE)
        for side in ("home", "away")
    }


def history_metrics(teams, matches):
    games = team_games(matches)
    cohort = jc_cup_teams(matches)
    counts = [len(games[key]) for key in cohort]
    provisional = {provider: dict(teams=0, cup_appearances=0) for provider in ("apif", "fdorg")}
    for team in teams:
        if found := PROVISIONAL.fullmatch(team["name"]):
            provider = found[1]
            provisional[provider]["teams"] += 1
            provisional[provider]["cup_appearances"] += sum(
                match["league"] in CUPS for match in games[team["id"]]
            )
    return dict(
        jc_since_utc=JC_SINCE,
        jc_cup_teams=len(cohort),
        jc_under_20=sum(n < 20 for n in counts),
        jc_under_60=sum(n < 60 for n in counts),
        jc_20_to_59=sum(20 <= n < 60 for n in counts),
        provisional=provisional,
    )


def coverage_lines(metrics):
    return [
        "统计口径：2025-07-01 UTC 起库内 jc 欧冠/欧罗巴/欧协联参赛球队"
        "（任务单在售样本口径，不代表当前仍在销售）；"
        "库内比赛按不同 match_id 计数，含所有状态，不是训练场数。",
        f"jc 欧战球队：{metrics['jc_cup_teams']}；少于 20 场：{metrics['jc_under_20']}；"
        f"少于 60 场：{metrics['jc_under_60']}；其中 20–59 场：{metrics['jc_20_to_59']}。",
        *(
            f"{provider} 临时身份：{counts['teams']}；欧战出场：{counts['cup_appearances']}。"
            for provider, counts in metrics["provisional"].items()
        ),
        "临时身份数含无比赛的行；欧战出场按球队/比赛计，双方均为临时身份的一场计两次。",
    ]
