"""Pure cutoff views of links already validated by ``load_result_links``."""

from collections import defaultdict
from datetime import timedelta

from zucai.sources.apif_stats import instant


def known_result_groups(matches, links, as_of):
    if as_of is None:
        return {}
    cutoff = instant(as_of)
    groups = {}
    for left, right, verified in links:
        if instant(verified) >= cutoff or left not in matches or right not in matches:
            continue
        a, b = matches[left], matches[right]
        if any(a[key] != b[key] for key in ("league", "home_team_id", "away_team_id")) or abs(
            instant(a["kickoff_utc"]) - instant(b["kickoff_utc"])
        ) > timedelta(hours=3):
            raise ValueError("赛果关联当前身份或开球窗口不符")
        members = groups.get(left, frozenset({left})) | groups.get(right, frozenset({right}))
        if len({matches[key]["source"] for key in members}) != len(members):
            raise ValueError("赛果关联组件存在同来源的多个比赛")
        for member in members:
            groups[member] = members
    return groups


def merge_known_result_groups(parents, matches, links, as_of):
    """Add proven edges to an existing exact/pointer grouping, without hiding ties."""

    def root(key):
        while parents[key] != key:
            key = parents[key]
        return key

    members = defaultdict(set)
    for key in parents:
        members[root(key)].add(key)
    for group in set(known_result_groups(matches, links, as_of).values()):
        roots = {root(key) for key in group if key in parents}
        if len(roots) < 2:
            continue
        merged = set().union(*(members[key] for key in roots))
        if len({matches[key]["source"] for key in merged}) != len(merged):
            continue
        target = min(roots)
        for key in roots - {target}:
            parents[key] = target
            members.pop(key)
        members[target] = merged
