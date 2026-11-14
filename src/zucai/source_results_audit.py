"""Read-only APIF result comparisons for T63's existing fifteen domestic leagues.

The CSV is evidence, never a training policy: all rows keep the existing source
pending owner review. Pair counts are per comparison source, so one APIF result
can have separate fd, jc and fdorg comparisons. Kickoff deltas are signed
``comparison - apif`` seconds. No name matching or database migration is performed.
"""

import csv
import io
import json
import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from zucai.result_source_identity import _binding, _owners
from zucai.sources.base import _atomic_write

LEAGUES = (
    "英超",
    "英冠",
    "西甲",
    "意甲",
    "德甲",
    "法甲",
    "法乙",
    "荷甲",
    "葡超",
    "日职",
    "瑞超",
    "挪超",
    "芬超",
    "美职",
    "巴甲",
)
COMPARISON_SOURCES = ("fd", "jc", "fdorg")
SOURCES = ("apif", *COMPARISON_SOURCES)
DECISION = "keep_existing_source_pending_owner_review"
WINDOW_SECONDS = 3 * 60 * 60
EVIDENCE_FIELDS = (
    "match_id",
    "source",
    "league",
    "home_team_id",
    "away_team_id",
    "home_team",
    "away_team",
    "home_goals",
    "away_goals",
    "score",
    "available_source",
    "available_utc",
    "kickoff_utc",
    "counterpart_id",
)
FIELDS = (
    "league",
    *(f"{side}_{field}" for side in ("apif", "comparison") for field in EVIDENCE_FIELDS),
    "kickoff_delta_seconds",
    "link_basis",
    "classification",
    "reason",
    "decision",
)


def _utc(value):
    try:
        when = datetime.fromisoformat(value)
        return when.astimezone(UTC) if when.utcoffset() is not None else None
    except (TypeError, ValueError):
        return None


def _read(database):
    """Read one consistent SQLite snapshot without creating or migrating anything."""
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not tables.intersection({"matches", "results", "teams"}):
            return []
        required = {
            "matches": {
                "match_id",
                "source",
                "league",
                "home_team_id",
                "away_team_id",
                "kickoff_utc",
            },
            "results": {"match_id", "home_goals", "away_goals"},
            "teams": {"id", "name"},
        }
        columns = {}
        for table, fields in required.items():
            columns[table] = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            if not fields <= columns[table]:
                raise ValueError(f"赛果审计数据库缺少必要字段：{table}")
        optional = []
        for table, alias, field in (
            ("matches", "m", "counterpart_id"),
            ("results", "r", "available_utc"),
            ("results", "r", "available_source"),
        ):
            value = f"{alias}.{field}" if field in columns[table] else "NULL"
            optional.append(f"{value} AS {field}")
        query = (
            "SELECT m.match_id, m.source, m.league, m.home_team_id, m.away_team_id, "
            "m.kickoff_utc, h.name AS home_team, a.name AS away_team, "
            "r.match_id AS result_id, r.home_goals, r.away_goals, "
            + ", ".join(optional)
            + " FROM matches m LEFT JOIN results r ON r.match_id=m.match_id"
            " LEFT JOIN teams h ON h.id=m.home_team_id"
            " LEFT JOIN teams a ON a.id=m.away_team_id ORDER BY m.match_id"
        )
        rows = [dict(row) for row in connection.execute(query)]
        aliases_column = "aliases" if "aliases" in columns["teams"] else "'[]' AS aliases"
        teams = {}
        # A binding can be claimed by a team with no matches in this audit (or at all).
        for team in connection.execute(f"SELECT id, name, {aliases_column} FROM teams"):
            try:
                aliases = json.loads(team["aliases"])
            except (TypeError, ValueError):
                aliases = None
            teams[team["id"]] = {"name": team["name"], "aliases": aliases}
        owners = _owners(teams)
    for row in rows:
        row["when"] = _utc(row["kickoff_utc"])
        row["binding_reason"] = _provider_binding_reason(row, teams, owners)
        row["unbound_apif_team_ids"] = {
            team_id
            for team_id in (row["home_team_id"], row["away_team_id"])
            if team_id in teams
            and isinstance(teams[team_id]["aliases"], list)
            and all(
                isinstance(alias, str) and not alias.startswith(f"apif:{row['league']}:team:")
                for alias in teams[team_id]["aliases"]
            )
        }
    return rows


def _provider_binding_reason(row, teams, owners):
    """Audit current explicit bindings only; never mint or backdate identity proof."""
    if row["source"] not in {"apif", "fdorg"}:
        return ""
    for side in ("home", "away"):
        team_id = row[f"{side}_team_id"]
        if team_id not in teams:
            return "missing_team_identity"
        try:
            binding = _binding(teams[team_id], row["source"], row["league"])
        except ValueError as exc:
            return f"provider_binding_invalid:{row['source']}:{side}:{exc}"
        if owners[binding.casefold()] != {team_id}:
            owner_ids = ",".join(str(owner) for owner in sorted(owners[binding.casefold()]))
            return (
                f"provider_binding_multiple_owners:{row['source']}:{side}:"
                f"{binding};owner_team_ids={owner_ids}"
            )
    return ""


def _pair(row):
    return row["home_team_id"], row["away_team_id"]


def _unmapped_fd_side(apif, other):
    if other["source"] != "fd" or apif["league"] != other["league"]:
        return False
    pair, old = _pair(apif), _pair(other)
    equal = [pair[side] == old[side] for side in (0, 1)]
    return (
        sum(equal) == 1
        and not (pair[0] == old[1] or pair[1] == old[0])
        and old[equal.index(False)] in other.get("unbound_apif_team_ids", set())
    )


class _Index:
    def __init__(self, rows):
        self.by_id = {row["match_id"]: row for row in rows}
        self.by_pair = defaultdict(list)
        self.by_team = defaultdict(dict)
        self.claims = defaultdict(list)
        for row in rows:
            self.by_pair[(row["league"], row["source"], frozenset(_pair(row)))].append(row)
            for team in _pair(row):
                self.by_team[row["league"], row["source"], team][row["match_id"]] = row
            if row["counterpart_id"]:
                self.claims[row["counterpart_id"]].append(row["match_id"])

    def source(self, key):
        row = self.by_id.get(key)
        return row["source"] if row is not None else key.partition(":")[0]

    def candidates(self, row, sources, *, all_links=False):
        # Include reversed pairs as exclusions. Names never create candidate edges.
        candidates = set()
        if row["when"] is not None:
            for source in sources:
                key = row["league"], source, frozenset(_pair(row))
                for other in self.by_pair[key]:
                    if (
                        other["when"] is not None
                        and abs((row["when"] - other["when"]).total_seconds()) <= WINDOW_SECONDS
                    ):
                        candidates.add(other["match_id"])
            # This is an unresolved candidate, never a match or a binding.
            for source in sources:
                if {row["source"], source} != {"apif", "fd"}:
                    continue
                for team in _pair(row):
                    for other in self.by_team[row["league"], source, team].values():
                        api, fd = (row, other) if row["source"] == "apif" else (other, row)
                        if (
                            _unmapped_fd_side(api, fd)
                            and other["when"] is not None
                            and abs((row["when"] - other["when"]).total_seconds()) <= WINDOW_SECONDS
                        ):
                            candidates.add(other["match_id"])
        linked = [*self.claims[row["match_id"]]]
        if row["counterpart_id"]:
            linked.append(row["counterpart_id"])
        candidates.update(key for key in linked if all_links or self.source(key) in sources)
        return sorted(candidates)


def _row_reason(row):
    source, key = row["source"], row["match_id"]
    if source not in SOURCES or not key.startswith(f"{source}:") or key == f"{source}:":
        return "unknown_source_prefix"
    if any(type(team) is not int or team <= 0 for team in _pair(row)) or len(set(_pair(row))) != 2:
        return "invalid_team_ids"
    names = row["home_team"], row["away_team"]
    if any(not isinstance(name, str) or not name.strip() for name in names):
        return "missing_team_identity"
    if any(re.match(r"^[a-z][a-z0-9_-]*:team:", name, re.IGNORECASE) for name in names):
        return "temporary_team_identity"
    if row["binding_reason"]:
        return row["binding_reason"]
    if row["when"] is None:
        return "invalid_kickoff"
    return ""


def _pair_reason(apif, other):
    reason = _row_reason(apif) or _row_reason(other)
    if reason:
        return reason
    if other["source"] not in COMPARISON_SOURCES:
        return "unsupported_comparison_source"
    if apif["league"] != other["league"]:
        return "league_mismatch"
    if _pair(apif) == tuple(reversed(_pair(other))):
        return "reversed_home_away"
    if _unmapped_fd_side(apif, other):
        return "unverified_fd_identity_overlap"
    if _pair(apif) != _pair(other):
        return "team_id_mismatch"
    if abs((other["when"] - apif["when"]).total_seconds()) > WINDOW_SECONDS:
        return "kickoff_outside_window"
    return ""


def _basis(apif, other):
    if apif["counterpart_id"] == other["match_id"] or other["counterpart_id"] == apif["match_id"]:
        return "counterpart"
    if _unmapped_fd_side(apif, other):
        return "shared_team_id_candidate"
    return "exact_team_ids_unique"


def _classification(apif, other):
    scores_differ = (apif["home_goals"], apif["away_goals"]) != (
        other["home_goals"],
        other["away_goals"],
    )
    kickoffs_differ = apif["when"] != other["when"]
    if (
        scores_differ
        and apif["league"] == "德甲"
        and apif["when"].date().isoformat() == other["when"].date().isoformat() == "2024-12-14"
        and (apif["home_team"], apif["away_team"]) == ("柏林联合", "波鸿")
        and (other["home_team"], other["away_team"]) == ("柏林联合", "波鸿")
        and (apif["home_goals"], apif["away_goals"]) == (1, 1)
        and (other["home_goals"], other["away_goals"]) == (0, 2)
    ):
        return "disciplinary_vs_on_field"
    if scores_differ and kickoffs_differ:
        return "score_and_kickoff_conflict"
    if scores_differ:
        return "score_conflict"
    return "kickoff_only_conflict" if kickoffs_differ else "consistent"


def _record(apif, other, classification, reason="", basis="none", *, missing_id=""):
    row = dict.fromkeys(FIELDS, "")
    row.update(
        league=(apif or other)["league"],
        link_basis=basis,
        classification=classification,
        reason=reason,
        decision=DECISION,
    )
    for side, match in (("apif", apif), ("comparison", other)):
        if match is None:
            row[f"{side}_match_id"] = missing_id
            continue
        for field in EVIDENCE_FIELDS:
            if field != "score":
                row[f"{side}_{field}"] = match[field]
        if match["result_id"]:
            row[f"{side}_score"] = f"{match['home_goals']}:{match['away_goals']}"
    if apif and other and apif["when"] is not None and other["when"] is not None:
        seconds = (other["when"] - apif["when"]).total_seconds()
        row["kickoff_delta_seconds"] = int(seconds) if seconds.is_integer() else seconds
    return row


def _audit_rows(matches):
    index = _Index(matches)
    apif_results = [
        row
        for row in matches
        if row["league"] in LEAGUES and row["source"] == "apif" and row["result_id"]
    ]
    comparisons = [
        row
        for row in matches
        if row["league"] in LEAGUES and row["source"] in COMPARISON_SOURCES and row["result_id"]
    ]
    rows, used = [], set()
    for apif in apif_results:
        candidates = index.candidates(apif, COMPARISON_SOURCES, all_links=True)
        if not candidates:
            reason = _row_reason(apif)
            rows.append(
                _record(
                    apif,
                    None,
                    "unverified" if reason else "unmatched",
                    reason or "no_comparison_candidate",
                )
            )
            continue
        source_counts = Counter(index.source(key) for key in candidates)
        for key in candidates:
            other = index.by_id.get(key)
            if other is None:
                reason = (
                    "counterpart_missing"
                    if index.source(key) in SOURCES
                    else "unknown_source_prefix"
                )
                rows.append(
                    _record(
                        apif,
                        None,
                        "unverified",
                        reason,
                        "counterpart_unresolved",
                        missing_id=key,
                    )
                )
                continue
            used.add(key)
            reason = _pair_reason(apif, other)
            if not reason and source_counts[other["source"]] != 1:
                reason = "multiple_comparison_candidates"
            if not reason and len(index.candidates(other, ("apif",))) != 1:
                reason = "multiple_apif_candidates"
            basis = _basis(apif, other)
            if reason:
                # Exact ID candidates have not earned a unique-match claim here.
                if basis == "exact_team_ids_unique":
                    basis = "exact_team_ids_candidate"
                rows.append(_record(apif, other, "unverified", reason, basis))
            elif not other["result_id"]:
                rows.append(_record(apif, other, "unmatched", "comparison_result_missing", basis))
            else:
                rows.append(_record(apif, other, _classification(apif, other), basis=basis))
    for other in comparisons:
        if other["match_id"] in used:
            continue
        candidates = index.candidates(other, ("apif",))
        if candidates:
            for key in candidates:
                apif = index.by_id.get(key)
                reason = _pair_reason(apif, other) if apif else "counterpart_missing"
                if not reason and len(index.candidates(apif, (other["source"],))) != 1:
                    reason = "multiple_comparison_candidates"
                if not reason and len(candidates) != 1:
                    reason = "multiple_apif_candidates"
                basis = _basis(apif, other) if apif else "counterpart_unresolved"
                if reason and basis == "exact_team_ids_unique":
                    basis = "exact_team_ids_candidate"
                rows.append(
                    _record(
                        apif,
                        other,
                        "unverified" if reason else "unmatched",
                        reason or "apif_result_missing",
                        basis,
                        missing_id=key,
                    )
                )
        else:
            reason = _row_reason(other)
            rows.append(
                _record(
                    None,
                    other,
                    "unverified" if reason else "unmatched",
                    reason or "no_apif_candidate",
                )
            )
    rows.sort(key=lambda row: (row["league"], row["apif_match_id"], row["comparison_match_id"]))
    return rows, len(apif_results), len(comparisons)


def audit_source_results(database, destination):
    """Atomically write a current audit CSV and return JSON-compatible summary counts.

    ``matched`` includes consistent and conflicting verified pairs; ``conflicts``
    is its differing-score/time subset. All counts describe the full comparison;
    ``comparison_rows`` is that denominator. ``rows`` counts only the actionable
    CSV data rows: score/time conflicts and unverified identities. Consistent
    pairs and missing results/candidates remain in summary counts only.
    Existing availability values, including NULL, are copied without inference.
    An empty SQLite database and schemas lacking optional legacy columns work.
    API rows require current unique league/provider numeric team bindings, with
    ownership checked across all teams. No historical identity timestamps are inferred.
    """
    database, destination = Path(database).resolve(), Path(destination)
    if not database.is_file():
        raise ValueError("赛果审计数据库不存在")
    protected = {
        database,
        *(Path(str(database) + suffix) for suffix in ("-wal", "-shm", "-journal")),
    }
    if destination.resolve() in protected or (
        destination.exists() and destination.samefile(database)
    ):
        raise ValueError("审计输出不可覆盖数据库或其日志")
    rows, apif_count, comparison_count = _audit_rows(_read(database))
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, FIELDS)
    writer.writeheader()
    actionable = [row for row in rows if row["classification"] not in {"consistent", "unmatched"}]
    writer.writerows(actionable)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(destination, output.getvalue().encode("utf-8"))
    counts = Counter(row["classification"] for row in rows)
    unmatched, unverified = counts["unmatched"], counts["unverified"]
    matched = len(rows) - unmatched - unverified
    return {
        "status": "complete" if apif_count else "no_apif_results",
        "rows": len(actionable),
        "comparison_rows": len(rows),
        "apif_results": apif_count,
        "comparison_results": comparison_count,
        "matched": matched,
        "conflicts": matched - counts["consistent"],
        "unmatched": unmatched,
        "unverified": unverified,
        "classifications": dict(sorted(counts.items())),
        "reasons": dict(sorted(Counter(row["reason"] for row in rows if row["reason"]).items())),
        "decision": DECISION,
    }
