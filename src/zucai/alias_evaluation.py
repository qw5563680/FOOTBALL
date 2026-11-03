"""Offline comparison only: this module never writes or activates an alias."""

import json
from collections import Counter, defaultdict
from types import SimpleNamespace

from zucai.alias_proposals import write_new_output
from zucai.aliases_snapshot import formal_aliases, selected_leagues, snapshot
from zucai.sources.apif_verify import infer_aliases
from zucai.sources.fd_ingest import WINDOW, _canonical
from zucai.sources.fd_verify import _utc


def complete_scopes(data, selected):
    groups = {}
    for row in data["audits"]["identity_matches"]:
        key = row["league"], row.get("season"), row["raw_sha256"]
        groups.setdefault(key, {})[row["provider_match_id"]] = row
    latest = {
        (row["league_id"], row["season"]): row for row in data["audits"].get("identity_scopes", [])
    }
    rows, missing = [], []
    for code, config in selected.items():
        for year in ("2025", "2026"):
            attempt = latest.get((code, year), {})
            scope = list(
                groups.get((config["league"], year, attempt.get("raw_sha256")), {}).values()
            )
            if (
                attempt.get("league") != config["league"]
                or attempt.get("complete") != "true"
                or attempt.get("scope_size") != str(len(scope))
                or not scope
                or any(
                    row.get("complete") != "true" or row.get("scope_size") != str(len(scope))
                    for row in scope
                )
            ):
                missing.append(f"{code}/{year}")
            else:
                rows.extend(scope)
    return rows, missing


def evaluate_rules(data, aliases, selected):
    rows, missing = complete_scopes(data, selected)
    official = [row for row in data["matches"] if row["source"] == "jc"]
    known = defaultdict(set)
    for league, entries in aliases.items():
        for key in entries:
            name = _canonical(aliases, league, key)
            if name:
                known[key].add(name)
    witnesses, names, reverse = defaultdict(set), defaultdict(set), defaultdict(set)
    strict, conflicts = set(), []
    for code, config in selected.items():
        league = config["league"]
        games = [
            SimpleNamespace(
                league=league,
                provider_match_id=int(row["provider_match_id"]),
                home_provider_id=int(row["home_provider_id"]),
                away_provider_id=int(row["away_provider_id"]),
                home_raw=row["home"],
                away_raw=row["away"],
                kickoff_utc=_utc(row["kickoff_utc"]),
            )
            for row in rows
            if row["league"] == league
        ]
        jc = [row for row in official if row["league"] == league]
        if len({g.provider_match_id for g in games}) != len(games):
            conflicts.append(f"{code}: 跨季出现重复 APIF 比赛 ID")
            continue
        edges = [
            (i, j)
            for i, game in enumerate(games)
            for j, other in enumerate(jc)
            if abs(game.kickoff_utc - _utc(other["kickoff_utc"])) <= WINDOW
        ]
        forward, backward = Counter(i for i, _ in edges), Counter(j for _, j in edges)
        for i, j in edges:
            if forward[i] != 1 or backward[j] != 1:
                continue
            game, other = games[i], jc[j]
            for key, canonical in (
                (str(game.home_provider_id), other["home"]),
                (str(game.away_provider_id), other["away"]),
            ):
                names[key].add(canonical)
                reverse[canonical].add(key)
                witnesses[key, canonical].add((game.provider_match_id, other["match_id"]))
        try:
            inferred = infer_aliases(games, jc, aliases=aliases.get(league, {}))
            strict.update(
                key for key, entry in inferred.items() if entry["status"] == "auto_verified"
            )
        except ValueError:
            conflicts.append(f"{code}: 现有严格图发现身份冲突")
    candidates = {}
    for key, labels in names.items():
        if len(labels) != 1 or known.get(key) and known[key] != labels:
            conflicts.append(f"APIF 球队 {key} 的中文身份不一致")
            continue
        canonical = next(iter(labels))
        if len(reverse[canonical]) != 1:
            conflicts.append(f"中文身份 {canonical} 对应多个 APIF ID")
            continue
        proof = witnesses[key, canonical]
        if (
            len({pair[0] for pair in proof}) >= 2
            and len({pair[1] for pair in proof}) >= 2
            and key not in known
        ):
            candidates[key] = dict(
                canonical=canonical,
                witnesses=[dict(apif_id=f"apif:{a}", counterpart_id=b) for a, b in sorted(proof)],
            )
    additional = set(candidates) - strict
    status = "insufficient_evidence" if missing else "conflict" if conflicts else "complete"
    return dict(
        status=status,
        production_rule_changed=False,
        missing_scopes=missing,
        two_match_candidates=len(candidates),
        already_strict=len(set(candidates) & strict),
        additional_teams=len(additional),
        additional_ids=sorted(additional),
        conflicts=sorted(set(conflicts)),
        eligible_for_next_task=status == "complete" and len(additional) >= 20,
        candidates=candidates,
        note="仅离线评估；须在身份桥接及所有者审批完成后的完整数据上复验。即使达标，也只能在下一张任务单讨论实现。",
    )


def write_evaluation(archive, codes, out):
    data = snapshot(archive)
    result = evaluate_rules(data, formal_aliases(), selected_leagues(codes))
    result["manifest_sha256"] = data["manifest_sha256"]
    write_new_output(out, archive, json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result
