"""T68 第八节第 13 项第二阶段（提案部分）：竞彩官方球队与库内既有球队是同一家俱乐部。

依据只有比赛：一支竞彩官方球队在某赛事过去十二个月的每一场官方比赛，三小时窗口内都有
同一个接口球队编号处在同一主客位置，没有一场例外，且这样的编号只有一个、主客都有见证、
不被该赛事另一支官方球队占用。这个编号若在别名表里已有获批条目指向库内某支球队，
两者就是同一家俱乐部。名称只作展示，不参与判断。本模块只读、只出提案。
"""

from bisect import bisect_left, bisect_right
from collections import defaultdict
from hashlib import sha256

from zucai.identity_fd_witness import WINDOW, _instant

MIN_OFFICIAL_MATCHES = 2
BASIS = "official_match_witness_same_provider_team"
REASONS = {
    "fewer_than_min_official_matches": f"官方比赛不足 {MIN_OFFICIAL_MATCHES} 场",
    "official_match_without_provider_record": "有官方比赛在三小时窗口内没有接口比赛",
    "no_consistent_provider_id": "没有一个接口编号在每一场官方比赛里都处在同一主客位置",
    "several_consistent_provider_ids": "同时有多个接口编号满足条件，无法唯一确定",
    "missing_home_or_away_witness": "主场或客场缺少见证",
    "provider_id_claimed_by_several_official_teams": "同一个接口编号同时对上了另一支官方球队",
}


def official_witness(facts):
    """Official numeric team id -> the provider team id every one of its matches agrees on."""
    index = defaultdict(list)
    for row in facts.get("apif", []):
        if row.get("kickoff_utc") and row.get("complete", True):
            index[str(row.get("competition_id"))].append(
                (_instant(row["kickoff_utc"]), str(row["home"]), str(row["away"]), row["match_id"])
            )
    ordered = {code: sorted(rows) for code, rows in index.items()}
    stamps = {code: [row[0] for row in rows] for code, rows in ordered.items()}
    games = defaultdict(list)
    for row in facts.get("official", []):
        if not row.get("kickoff_utc") or row.get("clock_conflict"):
            continue
        for side in ("home", "away"):
            official = row.get(f"{side}_source_id")
            if official:
                games[str(official)].append(
                    (
                        _instant(row["kickoff_utc"]),
                        side,
                        row["match_id"],
                        str(row["competition_id"]),
                    )
                )
    result = {}
    for official, rows in games.items():
        entry = dict(
            official_team_id=official,
            matches=len(rows),
            covered=0,
            home_witnesses=0,
            away_witnesses=0,
            provider_team_id=None,
            candidates=[],
            witnesses=[],
            reasons=[],
        )
        result[official] = entry
        consistent, covered = None, []
        for kickoff, side, match_id, code in sorted(rows):
            window = ordered.get(code, [])[
                bisect_left(stamps.get(code, []), kickoff - WINDOW) : bisect_right(
                    stamps.get(code, []), kickoff + WINDOW
                )
            ]
            if not window:
                continue
            position = 1 if side == "home" else 2
            same_side = {candidate[position] for candidate in window}
            consistent = same_side if consistent is None else consistent & same_side
            covered.append((kickoff, side, match_id, window))
        entry["covered"] = len(covered)
        if len(rows) < MIN_OFFICIAL_MATCHES:
            entry["reasons"].append("fewer_than_min_official_matches")
        if len(covered) != len(rows):
            entry["reasons"].append("official_match_without_provider_record")
        entry["candidates"] = sorted(consistent or ())
        if not consistent:
            if covered:
                entry["reasons"].append("no_consistent_provider_id")
            continue
        if len(consistent) > 1:
            entry["reasons"].append("several_consistent_provider_ids")
            continue
        provider = next(iter(consistent))
        for kickoff, side, match_id, window in covered:
            position = 1 if side == "home" else 2
            picked = sorted(c[3] for c in window if c[position] == provider)
            entry["witnesses"].append(
                dict(
                    match_id=match_id,
                    provider_match_id=picked[0],
                    kickoff_utc=kickoff.isoformat(),
                    side=side,
                )
            )
            entry[f"{side}_witnesses"] += 1
        if not entry["home_witnesses"] or not entry["away_witnesses"]:
            entry["reasons"].append("missing_home_or_away_witness")
        entry["claimed_provider_team_id"] = provider
    claims = defaultdict(list)
    for entry in result.values():
        if entry.get("claimed_provider_team_id"):
            claims[entry["claimed_provider_team_id"]].append(entry)
    for group in claims.values():
        if len(group) > 1:
            for entry in group:
                entry["reasons"].append("provider_id_claimed_by_several_official_teams")
    for entry in result.values():
        claimed = entry.pop("claimed_provider_team_id", None)
        if claimed and not entry["reasons"]:
            entry["provider_team_id"] = claimed
        entry["reasons"] = sorted(set(entry["reasons"]))
    return result


def witness_summary(entry):
    """The part of a witness that is shown in a verdict and frozen in a decision."""
    return dict(
        matches=entry["matches"],
        covered=entry["covered"],
        home_witnesses=entry["home_witnesses"],
        away_witnesses=entry["away_witnesses"],
        witnesses=entry["witnesses"],
    )


def link_decision(team, verdict, tables, *, evidence, canonical, json_text):
    """A proposed, not yet approved, 'same club' decision for one official team.

    ``verdict`` is the 关联 verdict carrying the stored team and the provider id.
    The stored team keeps its row and history; a second row that only exists because
    JC matches were synced under the official name is listed to be absorbed.
    """
    keep = str(verdict["project_team_id"])
    by_id = {str(row["id"]): row for row in tables["teams"]}
    blockers = []
    stored = by_id.get(keep)
    if stored is None:
        blockers.append("stored_team_missing")
    absorb = team.get("project_team_id")
    absorb = str(absorb) if absorb and str(absorb) != keep else None
    if absorb is not None:
        row = by_id.get(absorb)
        aliases = row["aliases"] if row else []
        if row is None or aliases not in ([], "[]"):
            blockers.append("official_name_row_has_aliases_or_is_missing")
        references = [
            match
            for match in tables["matches"]
            if absorb in (str(match["home_team_id"]), str(match["away_team_id"]))
        ]
        if any(match["source"] != "jc" for match in references):
            blockers.append("official_name_row_has_non_jc_matches")
        if any(keep in (str(m["home_team_id"]), str(m["away_team_id"])) for m in references):
            blockers.append("both_rows_meet_in_one_match")
    if team.get("category_status") != "approved" or not team.get("category"):
        blockers.append("category_not_approved_or_ambiguous")
    if not evidence or any(not row["originals"] for row in evidence):
        blockers.append("numeric_original_evidence_missing")
    witness = verdict["witness"]
    return dict(
        status="proposed",
        action="link_existing_identity",
        official_team_id=team["official_team_id"],
        canonical=canonical,
        official_names=team["official_names"],
        category=team.get("category"),
        evidence=evidence,
        evidence_sha256=sha256(json_text(evidence).encode()).hexdigest(),
        project_team_id=keep,
        previous_name=stored["name"] if stored else None,
        provider_team_id=verdict["provider_team_id"],
        absorb_project_team_id=absorb,
        witness=witness,
        witness_sha256=sha256(json_text(witness).encode()).hexdigest(),
        blockers=blockers,
    )
