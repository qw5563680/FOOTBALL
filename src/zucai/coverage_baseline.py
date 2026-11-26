"""Independent 90-minute historical proportions for unmodeled fixture rows."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from zucai.models import Match, Result
from zucai.result_source_groups import merge_known_result_groups
from zucai.result_source_links import load_result_links

MODEL_NAME = "historical-1x2-baseline"
MODEL_VERSION = "v1"
OUTCOMES = "hda"


@dataclass(frozen=True)
class BaselineHistory:
    by_league: dict[str, Counter]
    overall: Counter

    def for_league(self, league: str | None) -> dict | None:
        local = self.by_league.get(league)
        counts = local or self.overall
        total = counts.total()
        if not total:
            return None
        return {
            "model_name": MODEL_NAME,
            "model_version": MODEL_VERSION,
            "probabilities": {key: counts[key] / total for key in OUTCOMES},
            "history_scope": "league" if local else "global",
            "history_count": total,
        }


def historical_baseline(session: Session, cutoff: datetime) -> BaselineHistory:
    """Count only archived observed results and allow no assumed clock or smoothing."""
    if not isinstance(cutoff, datetime) or cutoff.utcoffset() is None:
        raise ValueError("基准预测截点必须包含时区")
    query = (
        select(
            Match.match_id,
            Match.source,
            Match.league,
            Match.home_team_id,
            Match.away_team_id,
            Match.kickoff_utc,
            Result.available_utc,
            Result.home_goals,
            Result.away_goals,
        )
        .join(Result, Result.match_id == Match.match_id)
        .where(
            Match.status == "finished",
            Result.available_utc.is_not(None),
            Result.available_source.in_(("captured", "published")),
        )
        .order_by(Match.match_id)
    )
    matches, parents, exact = {}, {}, {}
    for row in session.execute(query).mappings():
        try:
            kickoff = datetime.fromisoformat(row["kickoff_utc"])
            available = datetime.fromisoformat(row["available_utc"])
        except (TypeError, ValueError):
            continue
        if kickoff.utcoffset() is None or available.utcoffset() is None:
            continue
        if not kickoff < available < cutoff:
            continue
        score = row["home_goals"], row["away_goals"]
        if any(type(goals) is not int or goals < 0 for goals in score):
            continue
        key = row["match_id"]
        matches[key] = row
        signature = row["league"], row["home_team_id"], row["away_team_id"], kickoff
        parents[key] = exact.setdefault(signature, key)

    merge_known_result_groups(parents, matches, load_result_links(session), cutoff)
    scores_by_game = defaultdict(set)
    for key, row in matches.items():
        parent = key
        while parents[parent] != parent:
            parent = parents[parent]
        scores_by_game[parent].add((row["home_goals"], row["away_goals"]))

    leagues = defaultdict(Counter)
    overall = Counter()
    for key, scores in scores_by_game.items():
        if len(scores) != 1:
            continue
        ((home, away),) = scores
        outcome = "h" if home > away else "a" if away > home else "d"
        leagues[matches[key]["league"]][outcome] += 1
        overall[outcome] += 1
    return BaselineHistory(dict(leagues), overall)
