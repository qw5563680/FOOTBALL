"""Read-only promotion and relegation identity audit on a pinned feed archive."""

import argparse
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from zucai.predict.seasons import season_bounds, season_for

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_FILES = (
    "state/teams.jsonl",
    "state/matches.jsonl",
    "state/results.jsonl",
    "state/audit/apif_identity_merges.csv",
    "state/audit/fdorg_identity_merges.csv",
)
# Highest to lowest division. Levels without archive coverage still supply a boundary.
LADDERS = (
    ("英超", "英冠", "英甲"),
    ("法甲", "法乙", "法丙"),
    ("德甲", "德乙", "德丙"),
    ("荷甲", "荷乙", "荷丙"),
    ("日职", "日乙"),
    ("瑞超", "瑞典超甲"),
    ("挪超", "挪甲"),
    ("韩职", "韩职K2"),
    ("葡超", "葡甲"),
    ("意甲", "意乙"),
    ("西甲", "西乙"),
    ("芬超", "芬甲"),
    ("巴甲", "巴乙"),
    ("沙职", "沙特甲"),
)
NON_LADDER = {
    "美职": "封闭联赛",
    "澳超": "封闭联赛",
    "欧战": "俱乐部杯赛池，国内联赛换级另计",
    "欧国联": "国家队赛事，库内没有 A–D 级别字段",
}
PROVIDERS = {"apif": "api_football", "fdorg": "football_data_org", "fd": "football_data_co_uk"}
SOURCES = {"apif", "fdorg", "fd", "jc"}
PROVISIONAL = re.compile(r"(apif|fdorg):team:([0-9]+)")
SCOPED = re.compile(r"(apif|fdorg):([^:]+):team:([0-9]+)")
EXPECTED_SPLITS = {
    "英超": ("伯恩茅斯", "富勒姆", "诺丁汉", "桑德兰", "利兹联"),
    "法甲": ("欧塞尔", "勒阿弗尔", "图卢兹", "昂热", "洛里昂"),
}


def _jsonl(path):
    with path.open(encoding="utf-8") as stream:
        yield from map(json.loads, stream)


def _sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _seasons(cutoff, league):
    season = season_for(cutoff, league)
    for _ in range(4):
        yield season
        season = season_for(season_bounds(season, league)[0] - timedelta(seconds=1), league)


def _read_audit(archive):
    with (archive / "feed_manifest.json").open(encoding="utf-8") as stream:
        manifest = json.load(stream)
    for filename in ARCHIVE_FILES:
        if _sha(archive / filename) != manifest["files"][filename]:
            raise ValueError(f"归档 SHA256 不一致：{filename}")
    matches_with_results = {row[0] for row in _jsonl(archive / "state/results.jsonl")}
    teams = {
        row_id: (name, json.loads(aliases))
        for row_id, name, aliases in _jsonl(archive / "state/teams.jsonl")
    }
    cutoff = manifest["created_utc"]
    season_rules = {league: ladder[0] for ladder in LADDERS for league in ladder}
    season_windows = {
        league: list(reversed(list(_seasons(cutoff, league))))
        for league in dict.fromkeys(season_rules.values())
    }
    windows = {
        league: [(season, *season_bounds(season, top)) for season in season_windows[top]]
        for league, top in season_rules.items()
    }
    # Archived timestamps may use either Z or an explicit UTC offset. Compare parsed UTC times.
    latest = datetime.fromisoformat(cutoff)
    roster = defaultdict(lambda: defaultdict(set))
    game_counts = Counter()
    sources_by_row = defaultdict(set)
    historical = defaultdict(dict)
    for row in _jsonl(archive / "state/matches.jsonl"):
        match_id, source, league, home, away, kickoff, _, status, *_ = row
        if (
            league not in windows
            or source not in SOURCES
            or status != "finished"
            or match_id not in matches_with_results
        ):
            continue
        instant = datetime.fromisoformat(kickoff)
        if instant >= latest:
            continue
        for team_id in (home, away):
            historical[league, team_id][match_id] = instant
            sources_by_row[league, team_id].add(source)
        for season, start, end in windows[league]:
            if start <= instant < end:
                game_counts[league, season] += 1
                for team_id in (home, away):
                    roster[league, season][team_id].add(match_id)
                break
    gaps = []
    for provider in ("apif", "fdorg"):
        with (archive / f"state/audit/{provider}_identity_merges.csv").open(
            encoding="utf-8", newline=""
        ) as stream:
            for row in csv.DictReader(stream):
                if row["reason"] == "目标联赛正式别名未传播":
                    gaps.append((provider, row))
    return manifest, teams, season_windows, roster, game_counts, sources_by_row, historical, gaps


def _alias_index(ladder, teams, sources, gaps, formal):
    """Evidence for comparing two IDs. The original IDs stay in each roster."""
    names = {name: team_id for team_id, (name, _) in teams.items()}
    members = {key[1] for key in sources if key[0] in ladder}
    provider_names = defaultdict(set)
    candidates = defaultdict(set)
    for team_id in members:
        name, aliases = teams[team_id]
        for alias in aliases:
            if key := SCOPED.fullmatch(alias):
                provider, league, provider_id = key.groups()
                if league in ladder:
                    provider_names[provider, provider_id].add(team_id)
            elif alias_id := names.get(alias):
                if alias_id in members and any(
                    "fd" in sources.get((league, alias_id), ()) for league in ladder
                ):
                    candidates[alias_id].add(team_id)
    for provider, config in formal.items():
        for league in ladder:
            for alias, entry in config.get(league, {}).items():
                if entry.get("status") not in {"approved", "auto_verified"}:
                    continue
                canonical_id = names.get(entry.get("canonical"))
                if canonical_id not in members:
                    continue
                if provider == "fd":
                    alias_id = names.get(alias)
                    if alias_id in members and any(
                        "fd" in sources.get((item, alias_id), ()) for item in ladder
                    ):
                        candidates[alias_id].add(canonical_id)
                else:
                    provider_names[provider, alias].add(canonical_id)
    for provider, row in gaps:
        if row["league"] in ladder and (team_id := names.get(row["canonical"])) in members:
            provider_names[provider, row["team_id"]].add(team_id)
    for team_id in members:
        name = teams[team_id][0]
        if key := PROVISIONAL.fullmatch(name):
            candidates[team_id].update(provider_names[key.groups()])
    # Identity ambiguity stays unresolved. Do not join transitive aliases.
    index = {}
    for alias_id, targets in candidates.items():
        others = targets - {alias_id}
        if len(others) == 1:
            target = next(iter(others))
            if not candidates.get(target, set()) - {target}:
                index[alias_id] = target
    return index


def _canonical_roster(rows, aliases):
    grouped = defaultdict(dict)
    for team_id, fixtures in rows.items():
        grouped[aliases.get(team_id, team_id)][team_id] = fixtures
    return grouped


def _format_evidence(teams, roster):
    return "、".join(
        f"{teams[team_id][0]} `#{team_id}` ({min(fixtures)})"
        for team_id, fixtures in sorted(roster.items())
    )


def _newcomers(ladder, modeled, season_windows, roster, games, aliases, teams):
    grouped = {
        key: _canonical_roster(value, aliases) for key, value in roster.items() if key[0] in ladder
    }
    for position, league in enumerate(ladder):
        if league not in modeled:
            continue
        neighbors = [ladder[i] for i in (position - 1, position + 1) if 0 <= i < len(ladder)]
        seasons = season_windows[ladder[0]]
        for previous, current in zip(seasons[:-1], seasons[1:], strict=True):
            old = grouped.get((league, previous), {})
            recent = grouped.get((league, current), {})
            for identity in sorted(
                recent.keys() - old.keys(), key=lambda team_id: teams[team_id][0]
            ):
                side = recent[identity]
                previous_leagues = [
                    other for other in neighbors if identity in grouped.get((other, previous), {})
                ]
                overlap = any(identity in grouped.get((other, current), {}) for other in neighbors)
                if len(previous_leagues) == 1 and not overlap:
                    prior_league = previous_leagues[0]
                    prior = grouped[prior_league, previous][identity]
                    disposition = "接上" if side.keys() <= prior.keys() else "分行或部分分行"
                    yield (
                        league,
                        current,
                        teams[identity][0],
                        disposition,
                        prior_league,
                        side,
                        prior,
                    )
                else:
                    unobserved = [other for other in neighbors if not games[other, previous]]
                    if overlap or len(previous_leagues) > 1:
                        reason = "同季两级均有记录，无法认定相邻赛季换级"
                    elif unobserved:
                        reason = "邻级归档无赛果（" + "、".join(unobserved) + "）"
                    else:
                        reason = "邻级未找到可核实的前季身份"
                    yield league, current, teams[identity][0], reason, "", side, {}


def _ledger(teams, historical, gaps):
    names = {name: team_id for team_id, (name, _) in teams.items()}
    print("\n### 任务单点名的 10 支分行球队\n")
    print("|上级球队（行 ID）|次级球队（行 ID）|上级有赛果行|次级有赛果行|次级最近赛果（UTC）|")
    print("|---|---|---:|---:|---|")
    for upper, clubs in EXPECTED_SPLITS.items():
        lower = next(ladder[1] for ladder in LADDERS if ladder[0] == upper)
        for name in clubs:
            team_id = names.get(name)
            if team_id is None:
                print(f"|{name}（缺席）|待核|—|—|—|")
                continue
            possible = [
                alias
                for alias in teams[team_id][1]
                if alias in names and historical.get((lower, names[alias]))
            ]
            upper_games = historical.get((upper, team_id), {})
            if len(possible) != 1:
                print(f"|{name} `#{team_id}`|未获唯一分行证据|{len(upper_games)}|—|—|")
                continue
            alias = possible[0]
            alias_id = names[alias]
            lower_games = historical[lower, alias_id]
            latest = max(lower_games, key=lambda key: (lower_games[key], key))
            print(
                f"|{name} `#{team_id}`|{alias} `#{alias_id}`|{len(upper_games)}|"
                f"{len(lower_games)}|`{latest}` {lower_games[latest].isoformat()}|"
            )
    print("\n### 归档内未传播的正式别名审计\n")
    print("|联赛|来源队号|临时球队行 ID|规范球队行 ID|临时行有赛果的比赛数|审计运行/原因|")
    print("|---|---|---|---|---:|---|")
    for provider, row in sorted(
        gaps, key=lambda item: (item[1]["league"], int(item[1]["team_id"]))
    ):
        team_id = int(row["provisional_team_row_id"])
        current = len(historical.get((row["league"], team_id), {}))
        canonical = row["canonical"]
        print(
            f"|{row['league']}|`{provider}:{row['team_id']}`|`#{team_id}`|"
            f"{canonical} `#{names.get(canonical, '缺席')}`|{current}|"
            f"{row['run_id']} / {row['reason']}|"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args()
    manifest, teams, windows, roster, games, sources, historical, gaps = _read_audit(args.archive)
    modeled = json.loads((ROOT / "data/model/params.json").read_text(encoding="utf-8"))["leagues"]
    if set(modeled) - (set(NON_LADDER) | {league for ladder in LADDERS for league in ladder}):
        raise ValueError("存在未登记审计范围的模型联赛")
    formal = {
        provider: json.loads((ROOT / f"data/team_aliases/{name}.json").read_text(encoding="utf-8"))
        for provider, name in PROVIDERS.items()
    }
    entries = []
    for ladder in LADDERS:
        aliases = _alias_index(ladder, teams, sources, gaps, formal)
        entries.extend(_newcomers(ladder, modeled, windows, roster, games, aliases, teams))
    print(f"归档运行 `{manifest['run']}`，UTC `{manifest['created_utc']}`。")
    print(f"数据清单 SHA256 `{_sha(args.archive / 'feed_manifest.json')}`。")
    print("\n|有配置的联赛|接上|分行或部分分行|新行但换级待核|审计范围|")
    print("|---|---:|---:|---:|---|")
    for league in modeled:
        if league in NON_LADDER:
            print(f"|{league}|—|—|—|{NON_LADDER[league]}|")
            continue
        top = next(ladder[0] for ladder in LADDERS if league in ladder)
        counts = Counter(row[3] for row in entries if row[0] == league)
        same = counts["接上"]
        split = counts["分行或部分分行"]
        unresolved = sum(counts.values()) - same - split
        seasons = "、".join(windows[top][1:])
        print(f"|{league}|{same}|{split}|{unresolved}|{seasons}|")
    print("\n### 确认上一季相邻级别有赛果的参赛变化\n")
    print("|现联赛|赛季|球队|同一行可读历史|本季球队与比赛见证|上一季联赛、球队与比赛见证|")
    print("|---|---|---|---|---|---|")
    unresolved = defaultdict(list)
    for league, season, club, status, prior_league, side, prior in entries:
        if not prior:
            unresolved[league, season, status].append(club)
            continue
        print(
            f"|{league}|{season}|{club}|{status}|{_format_evidence(teams, side)}|"
            f"{prior_league} {_format_evidence(teams, prior)}|"
        )
    print("\n### 只确认是本季新增参赛行的待核记录\n")
    print("|联赛|赛季|待核原因|球队|")
    print("|---|---|---|---|")
    for (league, season, reason), clubs in sorted(unresolved.items()):
        print(f"|{league}|{season}|{reason}|{'、'.join(clubs)}|")
    _ledger(teams, historical, gaps)


if __name__ == "__main__":
    main()
