"""ID aliases inferred from complete oriented fixture sets, using T09's checked graph.

The caller supplies complete season/page captures, not cropped test samples. Neither
spelling nor score chooses a match. A bridge feed must already have verified time
and canonical names before its observations are passed as counterpart evidence.
"""

from zucai.sources.fd_ingest import _canonical
from zucai.sources.fdorg_verify import _graph


def infer_aliases(games, official, *, aliases=None, provenance=None):
    games = list(games)
    if not games:
        return dict(aliases or {})
    league = games[0].league
    if any(g.league != league for g in games):
        raise ValueError("每次只核验一个联赛")
    aliases = dict(aliases or {})
    provenance = provenance or {}
    active = {k: _canonical({league: aliases}, league, k) for k in aliases}
    graph = _graph(games, official, {k: v for k, v in active.items() if v is not None})
    by_id = {g.provider_match_id: g for g in games}
    by_official = {j["match_id"]: j for j in official}
    labels = {
        str(i): name
        for g in games
        for i, name in ((g.home_provider_id, g.home_raw), (g.away_provider_id, g.away_raw))
    }
    records = dict(aliases)
    for key, label in labels.items():
        if active.get(key):
            continue
        origin = graph["alias_origin"].get(key)
        if not origin:
            records[key] = dict(
                name=label,
                canonical=None,
                status="proposed",
                reason="缺少双向唯一的主客方向比赛证据",
            )
            continue
        witness = origin["witnesses"][0]
        game = by_id[witness["fd_row"]]
        other = by_official[witness["jc_match_id"]]
        records[key] = dict(
            name=label,
            canonical=graph["aliases"][key],
            status="auto_verified",
            evidence=[
                dict(
                    apif_id=f"apif:{game.provider_match_id}",
                    counterpart_id=other["match_id"],
                    home_id=game.home_provider_id,
                    away_id=game.away_provider_id,
                    counterpart_home=other["home"],
                    counterpart_away=other["away"],
                    apif_kickoff_utc=game.kickoff_utc.isoformat(),
                    counterpart_kickoff_utc=other["kickoff_utc"],
                    difference_minutes=witness["difference_minutes"],
                    iteration=origin["iteration"],
                    **provenance.get(game.provider_match_id, {}),
                )
            ],
        )
    return records
