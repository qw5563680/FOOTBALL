"""Read-only exact-ID history across already linked provider rows, experiment only."""

from collections import defaultdict

from zucai.features.stats import StatsFeatures

PRIORITY = {"jc": 0, "fd": 1, "fdorg": 2, "apif": 3}


def unified_history(session, *, provider):
    builder = StatsFeatures(session, provider=provider)
    games = {key: game for key, game in builder.matches.items() if game["status"] == "finished"}
    parents = {key: key for key in games}

    def root(key):
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    def identity(game):
        return game["league"], game["home_team_id"], game["away_team_id"]

    exact = {}
    for key, game in games.items():
        natural = (*identity(game), game["kickoff_utc"])
        if natural in exact:
            parents[root(key)] = root(exact[natural])
        exact[natural] = key
        other = game.get("counterpart_id")
        if (
            other in games
            and identity(game) == identity(games[other])
            and abs((game["kickoff_utc"] - games[other]["kickoff_utc"]).total_seconds()) <= 3 * 3600
        ):
            parents[root(key)] = root(other)
    groups = defaultdict(list)
    for key in games:
        groups[root(key)].append(games[key])
    histories, statistics = defaultdict(list), defaultdict(list)
    by_match = defaultdict(list)
    for key, values in builder.statistics.items():
        by_match[key[0]].append((key, values))
    canonical = []
    for members in groups.values():
        representative = min(members, key=lambda g: (PRIORITY.get(g["source"], 9), g["match_id"]))
        game = dict(representative)
        # A provider discrepancy must not move a completed match earlier into a window.
        game["kickoff_utc"] = max(g["kickoff_utc"] for g in members)
        game["earliest_kickoff_utc"] = min(g["kickoff_utc"] for g in members)
        game["history_match_ids"] = sorted(g["match_id"] for g in members)
        builder.matches[game["match_id"]] = game
        canonical.append(game)
        for side in ("home", "away"):
            team = game[side + "_team_id"]
            histories["aligned", game["league"], team].append(game)
        for member in members:
            for key, values in by_match[member["match_id"]]:
                statistics[game["match_id"], key[1], key[2]].extend(values)
    builder.histories.update(histories)
    for key, values in histories.items():
        values.sort(key=lambda g: (g["kickoff_utc"], g["match_id"]))
        builder.times[key] = [g["kickoff_utc"].value for g in values]
    builder.statistics = statistics
    return builder, sorted(canonical, key=lambda g: (g["kickoff_utc"], g["match_id"]))
