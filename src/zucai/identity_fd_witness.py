"""T68 第八节第 13 项：只有英文名的 fd/fdorg 球队，用比赛见证对上接口球队编号。

离线、只读，只产出提案；不写数据库、不改别名表、不发任何请求。球队名称只作展示，
不参与判断。成立条件（同时满足）：

- 该队在该联赛不少于 ``MIN_MATCHES`` 场；
- 有接口逐场记录覆盖（开球时间相差不超过 ``WINDOW``）的场次不少于 ``MIN_COVERAGE``；
- 每一场被覆盖的比赛里，都有同一个接口球队编号处在同一主客位置，没有一场例外，
  且这样的编号只有一个；
- 主场与客场都有见证；
- 这个编号不同时对上该联赛的另一支 fd/fdorg 球队。
"""

import argparse
import csv
import hashlib
import json
import logging
import re
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from zucai.sources.base import _atomic_write
from zucai.sources.teams import normalize_team

LOG = logging.getLogger(__name__)
PROTOCOL = "T68/fd-match-witness-v1"
MIN_MATCHES = 20
MIN_COVERAGE = 0.95
WINDOW = timedelta(hours=3)
FD_SOURCES = frozenset({"fd", "fdorg"})
IDENTITY_MATCHES = "state/audit/apif_identity_matches.csv"
PROVISIONAL = re.compile(r"apif:team:(\d+)")
APPROVED = frozenset({"approved", "auto_verified"})
REASONS = {
    "no_provider_records_for_league": "归档里没有该联赛的接口逐场记录",
    "fewer_than_min_matches": f"该队在该联赛不足 {MIN_MATCHES} 场",
    "coverage_below_threshold": "有接口记录覆盖的场次不足九成五",
    "no_consistent_provider_id": "没有一个接口编号在每一场被覆盖的比赛里都处在同一主客位置",
    "several_consistent_provider_ids": "同时有多个接口编号满足条件，无法唯一确定",
    "missing_home_or_away_witness": "主场或客场缺少见证",
    "provider_id_claimed_by_several_fd_teams": "同一个接口编号同时对上了该联赛的另一支 fd 球队",
    "conflicting_provider_ids_across_leagues": "该队在不同联赛对上了不同的接口编号",
}


def _instant(value):
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("比赛时间必须带时区")
    return stamp


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _rows(archive, name, columns):
    with (archive / f"state/{name}.jsonl").open(encoding="utf-8") as stream:
        return [
            dict(zip(columns, json.loads(line), strict=True)) for line in stream if line.strip()
        ]


def witness_proposals(teams, matches, provider_rows, formal):
    """Pure core. ``teams``: id -> {name, aliases}; ``matches``: stored match dicts;
    ``provider_rows``: league -> [{kickoff, home, away, match_id, names, sha256}];
    ``formal``: the paid-API alias table. Returns one entry per fd-only team and league."""
    approved_by_id, approved_names = defaultdict(set), set()
    for formal_entries in formal.values():
        for pid, entry in formal_entries.items():
            if entry.get("status") in APPROVED and entry.get("canonical"):
                approved_by_id[str(pid)].add(normalize_team(entry["canonical"]))
                approved_names.add(normalize_team(entry["canonical"]))
    sources, played = defaultdict(set), defaultdict(lambda: defaultdict(list))
    for row in matches:
        for side in ("home", "away"):
            team = row.get(f"{side}_team_id")
            if team is None:
                continue
            sources[team].add(row.get("source"))
            if row.get("source") in FD_SOURCES and row.get("league") and row.get("kickoff_utc"):
                played[team][row["league"]].append((_instant(row["kickoff_utc"]), side, row))
    index = {}
    for league, rows in provider_rows.items():
        ordered = sorted(rows, key=lambda r: r["kickoff"])
        index[league] = ([r["kickoff"] for r in ordered], ordered)
    display = defaultdict(set)
    for rows in provider_rows.values():
        for row in rows:
            for side in ("home", "away"):
                if row.get("names", {}).get(side):
                    display[str(row[side])].add(row["names"][side])

    entries = []
    for team, leagues in sorted(played.items(), key=lambda item: int(item[0])):
        record = teams.get(team) or {}
        name = record.get("name") or ""
        aliases = record.get("aliases") or []
        if not sources[team] <= FD_SOURCES:
            continue
        if normalize_team(name) in approved_names or any(
            str(alias).startswith("apif:") for alias in aliases
        ):
            continue
        for league, games in sorted(leagues.items()):
            entry = dict(
                protocol=PROTOCOL,
                league=league,
                project_team_id=str(team),
                name=name,
                sources=sorted(sources[team]),
                matches=len(games),
                covered=0,
                provider_team_id=None,
                provider_names=[],
                home_witnesses=0,
                away_witnesses=0,
                witnesses=[],
                status="unverified",
                action=None,
                reasons=[],
            )
            entries.append(entry)
            if league not in index:
                entry["reasons"].append("no_provider_records_for_league")
                continue
            stamps, ordered = index[league]
            consistent, covered = None, []
            for kickoff, side, row in sorted(games, key=lambda g: g[0]):
                window = ordered[
                    bisect_left(stamps, kickoff - WINDOW) : bisect_right(stamps, kickoff + WINDOW)
                ]
                if not window:
                    continue
                same_side = {str(candidate[side]) for candidate in window}
                consistent = same_side if consistent is None else consistent & same_side
                covered.append((kickoff, side, row, window))
            entry["covered"] = len(covered)
            if len(games) < MIN_MATCHES:
                entry["reasons"].append("fewer_than_min_matches")
            if len(covered) < MIN_COVERAGE * len(games):
                entry["reasons"].append("coverage_below_threshold")
            if not consistent:
                if covered:
                    entry["reasons"].append("no_consistent_provider_id")
                continue
            if len(consistent) > 1:
                entry["reasons"].append("several_consistent_provider_ids")
                continue
            provider = next(iter(consistent))
            entry["provider_team_id"] = provider
            entry["provider_names"] = sorted(display[provider])
            for kickoff, side, row, window in covered:
                picked = sorted(
                    (c for c in window if str(c[side]) == provider), key=lambda c: c["match_id"]
                )
                entry["witnesses"].append(
                    dict(
                        match_id=row["match_id"],
                        provider_match_id=picked[0]["match_id"],
                        kickoff_utc=kickoff.isoformat(),
                        provider_kickoff_utc=picked[0]["kickoff"].isoformat(),
                        side=side,
                        raw_sha256=picked[0].get("sha256"),
                    )
                )
                entry[f"{side}_witnesses"] += 1
            if not entry["home_witnesses"] or not entry["away_witnesses"]:
                entry["reasons"].append("missing_home_or_away_witness")

    claims = defaultdict(list)
    for entry in entries:
        if entry["provider_team_id"]:
            claims[(entry["league"], entry["provider_team_id"])].append(entry)
    for group in claims.values():
        if len({entry["project_team_id"] for entry in group}) > 1:
            for entry in group:
                entry["reasons"].append("provider_id_claimed_by_several_fd_teams")
    by_team = defaultdict(set)
    for entry in entries:
        if entry["provider_team_id"] and not entry["reasons"]:
            by_team[entry["project_team_id"]].add(entry["provider_team_id"])
    names = {normalize_team(t.get("name") or ""): key for key, t in teams.items()}
    for entry in entries:
        if len(by_team[entry["project_team_id"]]) > 1:
            entry["reasons"].append("conflicting_provider_ids_across_leagues")
        if entry["reasons"]:
            entry["reasons"] = sorted(set(entry["reasons"]))
            continue
        entry["status"] = "proposed"
        targets = sorted(
            (
                names[canonical]
                for canonical in approved_by_id.get(entry["provider_team_id"], ())
                if canonical in names and str(names[canonical]) != entry["project_team_id"]
            ),
            key=str,
        )
        if targets:
            entry["action"] = "same_team"
            entry["same_as_project_team_ids"] = [str(target) for target in targets]
            entry["same_as_names"] = [teams[target]["name"] for target in targets]
        else:
            entry["action"] = "register_provider_id"
    return entries


def _provider_rows(archive, teams, matches, formal):
    rows = defaultdict(list)
    path = archive / IDENTITY_MATCHES
    if path.is_file():
        with path.open(encoding="utf-8", newline="") as stream:
            for row in csv.DictReader(stream):
                if row["complete"] != "true":
                    continue
                rows[row["league"]].append(
                    dict(
                        kickoff=_instant(row["kickoff_utc"]),
                        home=row["home_provider_id"],
                        away=row["away_provider_id"],
                        match_id=f"apif:{row['provider_match_id']}",
                        names=dict(home=row["home"], away=row["away"]),
                        sha256=row["raw_sha256"],
                    )
                )
    # Stored paid-API matches: the provider id is the provisional name or the
    # unique approved alias-table entry of that league. Unresolved rows are skipped.
    by_league = {
        league: {
            normalize_team(entry["canonical"]): str(pid)
            for pid, entry in entries.items()
            if entry.get("status") in APPROVED and entry.get("canonical")
        }
        for league, entries in formal.items()
    }

    def provider(team, league):
        name = (teams.get(team) or {}).get("name") or ""
        found = PROVISIONAL.fullmatch(name)
        if found:
            return found.group(1)
        return by_league.get(league, {}).get(normalize_team(name))

    seen = {(league, row["match_id"]) for league, items in rows.items() for row in items}
    for row in matches:
        if row.get("source") != "apif" or not row.get("league") or not row.get("kickoff_utc"):
            continue
        home = provider(row["home_team_id"], row["league"])
        away = provider(row["away_team_id"], row["league"])
        if home is None or away is None or (row["league"], row["match_id"]) in seen:
            continue
        rows[row["league"]].append(
            dict(
                kickoff=_instant(row["kickoff_utc"]),
                home=home,
                away=away,
                match_id=row["match_id"],
                names={},
                sha256=None,
            )
        )
    return rows


def propose(archive, *, leagues=None, formal_path=None):
    """Read one archive and return the fd match-witness proposals. Nothing is written."""
    from zucai.identity_binding_archive import FORMAL

    archive = Path(archive)
    manifest = json.loads((archive / "feed_manifest.json").read_bytes())
    formal_path = Path(formal_path) if formal_path else FORMAL
    formal = json.loads(formal_path.read_bytes())
    teams = {
        row["id"]: dict(name=row["name"], aliases=row["aliases"] or [])
        for row in _rows(archive, "teams", manifest["columns"]["teams"])
    }
    for record in teams.values():
        if isinstance(record["aliases"], str):
            record["aliases"] = json.loads(record["aliases"])
    matches = _rows(archive, "matches", manifest["columns"]["matches"])
    entries = witness_proposals(
        teams, matches, _provider_rows(archive, teams, matches, formal), formal
    )
    if leagues:
        entries = [entry for entry in entries if entry["league"] in set(leagues)]
    inputs = dict(
        manifest_sha256=_sha(archive / "feed_manifest.json"),
        formal_aliases_sha256=_sha(formal_path),
        identity_matches_sha256=(
            _sha(archive / IDENTITY_MATCHES) if (archive / IDENTITY_MATCHES).is_file() else None
        ),
        min_matches=MIN_MATCHES,
        min_coverage=MIN_COVERAGE,
        window_hours=WINDOW.total_seconds() / 3600,
    )
    return dict(
        version=1,
        protocol=PROTOCOL,
        inputs=inputs,
        proposals=entries,
        requests=0,
        production_writes=0,
    )


def review_markdown(document):
    lines = [
        "# 只有英文名的球队：按比赛见证对接口编号（待审提案）",
        "",
        "全部为待审提案，不自动生效。球队名称只作展示，不参与判断。",
        "",
        "| 联赛 | 库内球队（编号） | 接口编号 | 接口名称 | 场数 / 被覆盖 | 主 / 客见证 | 结论 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for entry in document["proposals"]:
        if entry["status"] == "proposed" and entry["action"] == "same_team":
            verdict = "提案：与库内 " + "、".join(entry["same_as_names"]) + " 是同一支球队"
        elif entry["status"] == "proposed":
            verdict = "提案：给这支球队登记该接口编号"
        else:
            verdict = "不给提案：" + "；".join(REASONS[reason] for reason in entry["reasons"])
        lines.append(
            "| {} | {}（{}） | {} | {} | {} / {} | {} / {} | {} |".format(
                entry["league"],
                entry["name"],
                entry["project_team_id"],
                entry["provider_team_id"] or "—",
                "、".join(entry["provider_names"]) or "—",
                entry["matches"],
                entry["covered"],
                entry["home_witnesses"],
                entry["away_witnesses"],
                verdict,
            )
        )
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Offline T68 fd match-witness proposals. Nothing is written to the archive."
    )
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--leagues", nargs="+")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    out = args.out.resolve()
    if out.exists():
        parser.error("--out 须为尚不存在的新目录")
    if out.is_relative_to(args.archive.resolve()):
        parser.error("--out 不得位于归档内")
    document = propose(args.archive, leagues=args.leagues)
    out.mkdir(parents=True)
    _atomic_write(
        out / "fd-witness.json",
        (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    _atomic_write(out / "owner-review.md", review_markdown(document).encode("utf-8"))
    proposed = sum(entry["status"] == "proposed" for entry in document["proposals"])
    LOG.info(
        "T68 fd witness proposals=%s; unverified=%s; requests=0; production writes=0",
        proposed,
        len(document["proposals"]) - proposed,
    )
    return document


if __name__ == "__main__":
    main()
