"""Extend the existing three-feed training guard without editing its implementation."""

from collections import defaultdict, deque
from functools import wraps

import pandas as pd


def extend_guard(previous):
    @wraps(previous)
    def reject_cross_source_overlaps(frame):
        previous(frame)
        selected = frame.loc[frame.source.isin(("jc", "fd", "fdorg", "apif"))]
        recent = defaultdict(deque)
        window = pd.Timedelta(hours=3)
        for row in selected.sort_values("kickoff_utc").itertuples():
            group = recent[row.league]
            while group and group[0].kickoff_utc < row.kickoff_utc - window:
                group.popleft()
            for other in group:
                if row.source == other.source or "apif" not in (row.source, other.source):
                    continue
                if any(
                    str(name).startswith(("apif:team:", "fdorg:team:"))
                    for name in (row.home, row.away, other.home, other.away)
                ) or not {
                    row.home,
                    row.away,
                }.isdisjoint((other.home, other.away)):
                    raise ValueError(
                        "同联赛三小时内存在跨来源重复或待核对的身份重叠："
                        + other.match_id
                        + " / "
                        + row.match_id
                    )
            group.append(row)

    return reject_cross_source_overlaps
