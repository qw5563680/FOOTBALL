"""Copy formal same-provider ID authorization to leagues with provisional history."""

import hashlib
import json
import logging
from collections import defaultdict
from copy import deepcopy
from pathlib import Path

from zucai import aliases_snapshot
from zucai.cup_history import PROVISIONAL, team_games
from zucai.sources.apif_aliases import binding_scope_allows, validate_approvals
from zucai.sources.base import _atomic_write
from zucai.sources.fdorg_verify import _active

LOG = logging.getLogger(__name__)
APPROVAL_FIELDS = ("approved_by", "approved_utc", "approval_pr")


def propagated_entry(source, source_league, source_sha, *, name=None):
    """Retain the original authorization, never invent approval metadata."""
    validate_approvals({source_league: {"1": source}}, require_metadata=True)
    return dict(
        name=name or source.get("name", ""),
        canonical=source["canonical"],
        status=source["status"],
        evidence=[
            dict(
                kind="same_provider_id",
                source_league=source_league,
                source_status=source["status"],
                source_aliases_sha256=source_sha,
            )
        ],
        **{key: deepcopy(source[key]) for key in APPROVAL_FIELDS if key in source},
        **(
            {"binding_scope": deepcopy(source["binding_scope"])}
            if "binding_scope" in source
            else {}
        ),
    )


def _plan(aliases, provider, histories, sha):
    updated = deepcopy(aliases)
    active = {league: _active(aliases, league) for league in aliases}
    changes = []
    for key, leagues in sorted(histories.items(), key=lambda item: int(item[0])):
        origins = [league for league, entries in active.items() if key in entries]
        if not origins:
            continue
        if len({active[league][key] for league in origins}) != 1:
            raise ValueError(f"{provider}/{key} 同源 ID 跨联赛规范名冲突")
        # Prefer original evidence over a previously propagated entry; use a stable tie-break.
        origins.sort(
            key=lambda league: (
                any(
                    e.get("kind") == "same_provider_id"
                    for e in aliases[league][key].get("evidence", [])
                ),
                league,
            )
        )
        origin = origins[0]
        source = aliases[origin][key]
        for league in sorted(leagues):
            if not binding_scope_allows(source, league):
                continue
            # T68 target entries require their own PR authorization.
            if "binding_scope" in source and key not in active.get(league, {}):
                continue
            if key in active.get(league, {}):
                continue  # Active records, including provenance, remain byte-for-byte values.
            previous = aliases.get(league, {}).get(key, {})
            entry = propagated_entry(source, origin, sha, name=previous.get("name"))
            updated.setdefault(league, {})[key] = entry
            changes.append(
                dict(
                    provider=provider,
                    league=league,
                    team_id=key,
                    previous_status=previous.get("status"),
                    **entry,
                )
            )
    for league in {row["league"] for row in changes}:
        _active(updated, league)  # Reject reverse collisions before writing either provider.
    return updated, changes


def propagate(archive):
    """Only formal files change; the hash-verified archive is read through a temporary DB."""
    archive = Path(archive).resolve()
    paths = {
        "apif": Path(aliases_snapshot.ALIASES).resolve(),
        "fdorg": Path(aliases_snapshot.FDORG_ALIASES).resolve(),
    }
    if any(path.is_relative_to(archive) for path in paths.values()):
        raise ValueError("正式别名不得写入只读数据归档")
    raw = {provider: path.read_bytes() for provider, path in paths.items()}
    formal = {provider: json.loads(content) for provider, content in raw.items()}
    for provider, aliases in formal.items():
        validate_approvals(aliases, require_metadata=provider == "apif")
    data = aliases_snapshot.snapshot(archive)
    games = team_games(data["matches"])
    histories = {provider: defaultdict(set) for provider in paths}
    for team in data["teams"]:
        if found := PROVISIONAL.fullmatch(team["name"]):
            provider, key = found.groups()
            histories[provider][key].update(
                game["league"] for game in games[team["id"]] if game["source"] == provider
            )
    changed, changes = {}, []
    for provider, aliases in formal.items():
        updated, rows = _plan(
            aliases, provider, histories[provider], hashlib.sha256(raw[provider]).hexdigest()
        )
        if rows:
            changed[provider] = (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode()
            changes.extend(rows)
    if any(path.read_bytes() != raw[provider] for provider, path in paths.items()):
        raise ValueError("正式别名已改变，请重新运行传播命令")
    attempted = []
    try:
        for provider, content in changed.items():
            attempted.append(provider)
            _atomic_write(paths[provider], content)
    except Exception:
        for provider in attempted:
            if paths[provider].read_bytes() != raw[provider]:
                _atomic_write(paths[provider], raw[provider])
        raise
    for row in changes:
        LOG.info(
            "%s %s/%s → %s；%s",
            row["provider"],
            row["league"],
            row["team_id"],
            row["canonical"],
            json.dumps(row, ensure_ascii=False, sort_keys=True),
        )
    LOG.info(
        "传播 %d 条；归档 SHA256 %s；归档只读，未合并数据库", len(changes), data["manifest_sha256"]
    )
    return dict(changes=changes, manifest_sha256=data["manifest_sha256"])
