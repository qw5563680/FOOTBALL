"""Accumulate review-only candidates from this run's complete, safely archived scopes."""

import json
from collections import defaultdict
from hashlib import sha256
from pathlib import Path

from zucai.sources.api_football import ApiFootball
from zucai.sources.apif_command import ALIASES as APIF_ALIASES
from zucai.sources.apif_verify import infer_aliases
from zucai.sources.base import RawStore, _atomic_write
from zucai.sources.fd_verify import _read_official
from zucai.sources.fdorg_command import ALIASES as FDORG_ALIASES
from zucai.sources.fdorg_verify import _active, _Fixture, _graph, _new_aliases
from zucai.sources.football_data import read_json
from zucai.sources.football_data_org import FootballDataOrg


def write_candidates(provider, database, raw_dir, captures, output):
    """Reuse T09/T10 oriented graphs; never activate candidates or modify official aliases."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"{provider}.json"
    candidates = read_json(target) if target.exists() else {}
    aliases = read_json(FDORG_ALIASES if provider == "fdorg" else APIF_ALIASES)
    official = _read_official(database)
    store = RawStore(raw_dir)
    source = FootballDataOrg(store=store) if provider == "fdorg" else ApiFootball(store=store)
    paths = {}
    subdir = Path(raw_dir) / source.name
    for sidecar in subdir.rglob("*.meta.json"):
        meta = read_json(sidecar)
        if meta.get("status_code") == 200:
            paths[meta["sha256"]] = sidecar
    new_count = 0
    try:
        for capture in captures:
            sha = capture["raw_sha256"]
            sidecar = paths[sha]
            path = sidecar.with_suffix("").with_suffix("")
            raw = path.read_bytes()
            if sha256(raw).hexdigest() != sha:
                raise ValueError("候选原件哈希不符")
            games = source.parse(raw, **capture["scope"])
            # Removed identities could conceal ambiguous edges. A rejected scope cannot
            # establish new aliases; its audit remains available for human investigation.
            if source.rejected:
                continue
            groups = defaultdict(list)
            for game in games:
                groups[game.league].append(game)
            for league, games in groups.items():
                entries = aliases.get(league, {})
                jc = [j for j in official if j["league"] == league]
                provenance = dict(raw_sha256=sha, sidecar=sidecar.relative_to(raw_dir).as_posix())
                if provider == "fdorg":
                    games = [
                        _Fixture(
                            provider_match_id=g.provider_match_id,
                            home_provider_id=g.home_provider_id,
                            away_provider_id=g.away_provider_id,
                            kickoff_utc=g.kickoff_utc,
                            home_raw=g.home_raw,
                            away_raw=g.away_raw,
                            provider_status=g.status,
                            import_rejection=None,
                        )
                        for g in games
                    ]
                    graph = _graph(games, jc, _active({league: entries}, league))
                    inferred = _new_aliases(games, jc, entries, graph, provenance)
                else:
                    inferred = infer_aliases(
                        games,
                        jc,
                        aliases=entries,
                        provenance={game.provider_match_id: provenance for game in games},
                    )
                for key, entry in inferred.items():
                    if entry.get("status") != "auto_verified" or entry == entries.get(key):
                        continue
                    prior = candidates.setdefault(league, {}).get(key)
                    if prior and prior["canonical"] != entry["canonical"]:
                        raise ValueError("候选与历史证据冲突，须人工核验")
                    if prior:
                        entry["evidence"] = list(prior["evidence"]) + [
                            e for e in entry["evidence"] if e not in prior["evidence"]
                        ]
                    else:
                        new_count += 1
                    candidates[league][key] = entry
    finally:
        source.session.close()
    _atomic_write(target, (json.dumps(candidates, ensure_ascii=False, indent=2) + "\n").encode())
    return new_count
