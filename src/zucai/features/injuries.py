"""Complete-capture injury counts and previous-starting-lineup intersections."""

import json
from collections import defaultdict
from copy import copy
from datetime import timedelta
from pathlib import Path

from sqlalchemy import inspect, select

from zucai.injuries_storage import (
    AUDIT_NAME,
    RECEIPT_VERSION,
    read_capture_receipt,
    verify_capture_row,
)
from zucai.models import ApifFixtureIndex, Match, MatchInjury, MatchLineup
from zucai.result_source_groups import merge_known_result_groups
from zucai.result_source_links import load_result_links
from zucai.sources.apif_injuries import INJURY_TYPES
from zucai.sources.apif_stats import instant
from zucai.stats_storage import read_audit

VERSION = "t38-injury-features-v1"
REPLAY_ASSUMPTION = "latest_archived_fixture_list_assumed_known_immediately_before_kickoff"
METRICS = ("missing_n", "questionable_n", "missing_last_starters_n")
SOURCE_ORDER = {"jc": 0, "fd": 1, "fdorg": 2, "apif": 3}


def _read_rows(session, model, tables):
    if model.__tablename__ not in tables:
        return []
    return [dict(row) for row in session.execute(select(model.__table__)).mappings()]


def _receipt_rows(directory, name, outcomes=("ingested",)):
    if directory is None:
        return []
    return [
        dict(row, detail=json.loads(row["detail"]))
        for row in read_audit(directory / name)
        if row["outcome"] in outcomes
    ]


def _injury_pages(directory):
    pages = []
    for row in _receipt_rows(directory, AUDIT_NAME, outcomes=("ingested", "rejected")):
        detail = row["detail"]
        if (
            row["endpoint"] != "injuries"
            or not isinstance(detail, dict)
            or detail.get("receipt_version") != RECEIPT_VERSION
        ):
            continue
        try:
            payload = read_capture_receipt(directory.parent.parent, detail)
            verify_capture_row(row, payload)
            fixtures, incomplete = payload["fixtures"], False
        except (OSError, ValueError, KeyError, TypeError, EOFError):
            fixtures, incomplete = {}, True
        row["detail"] = dict(
            detail, fixtures=fixtures, page_errors=detail["page_errors"] + int(incomplete)
        )
        pages.append(row)
    return pages


def _cell(value=None, *, reason=None, n=0, evidence=None, provenance=None, **extra):
    provenance = provenance or {}
    return dict(
        value=value,
        n=n,
        missing_reason=reason,
        evidence=evidence or [],
        provenance=provenance,
        availability_basis=provenance.get("availability_basis"),
        captured_utc=provenance.get("captured_utc"),
        **extra,
    )


class InjuryFeatures:
    """Read rows and their archived capture receipts into an immutable working view."""

    def __init__(self, session):
        bind = session.get_bind()
        tables = set(inspect(bind).get_table_names())
        self.matches = {row["match_id"]: row for row in _read_rows(session, Match, tables)}
        self.result_links = load_result_links(session)
        self._proof_views = {}
        self.indices = _read_rows(session, ApifFixtureIndex, tables)
        self.injuries = {row["id"]: row for row in _read_rows(session, MatchInjury, tables)}
        self.lineups = [
            row
            for row in _read_rows(session, MatchLineup, tables)
            if row["provider"] == "apif" and row["endpoint"] == "lineups"
        ]
        database = bind.engine.url.database
        directory = (
            Path(database).parent / "state/audit" if database and database != ":memory:" else None
        )
        self.pages = _injury_pages(directory)
        self.lineup_receipts = [
            row
            for row in _receipt_rows(directory, "stats_ingest_audit.csv")
            if row["endpoint"] == "lineups"
        ]
        self.groups = {key: key for key in self.matches}
        exact = {}
        # Exact canonical team IDs and UTC kickoff agree with the experiment host's
        # history deduplication. No provider names or approximate matching are used.
        for game in self.matches.values():
            identity = (
                game["league"],
                instant(game["kickoff_utc"]),
                game["home_team_id"],
                game["away_team_id"],
            )
            previous = exact.setdefault(identity, game["match_id"])
            left, right = self._group(game["match_id"]), self._group(previous)
            self.groups[max(left, right)] = min(left, right)
        # Consistent explicit counterparts additionally cover recorded clock offsets.
        for game in self.matches.values():
            other = self.matches.get(game["counterpart_id"])
            if (
                other
                and all(
                    game[key] == other[key] for key in ("league", "home_team_id", "away_team_id")
                )
                and abs(instant(game["kickoff_utc"]) - instant(other["kickoff_utc"]))
                <= timedelta(hours=3)
            ):
                left, right = self._group(game["match_id"]), self._group(other["match_id"])
                self.groups[max(left, right)] = min(left, right)
        self.index_groups = defaultdict(list)
        self.histories = defaultdict(list)
        self.page_scopes = defaultdict(list)
        self.lineup_groups = defaultdict(list)
        self.lineup_capture_groups = defaultdict(list)
        self.group_clocks = {}
        for row in self.indices:
            self.index_groups[self._group(row["match_id"])].append(row)
        for row in self.matches.values():
            group = self._group(row["match_id"])
            kickoff = instant(row["kickoff_utc"])
            earliest, latest = self.group_clocks.get(group, (kickoff, kickoff))
            self.group_clocks[group] = min(earliest, kickoff), max(latest, kickoff)
            if row["status"] == "finished":
                for side in ("home", "away"):
                    self.histories[row[side + "_team_id"]].append(row)
        for row in self.pages:
            detail = row["detail"]
            self.page_scopes[detail["provider_league_id"], detail["provider_season"]].append(row)
        for row in self.lineups:
            self.lineup_groups[row["provider_fixture_id"]].append(row)
        for row in self.lineup_receipts:
            self.lineup_capture_groups[row["provider_fixture_id"]].append(row)

    def _group(self, match_id):
        while self.groups.get(match_id, match_id) != match_id:
            match_id = self.groups[match_id]
        return match_id

    def _proof_view(self, cutoff):
        links = tuple(link for link in self.result_links if instant(link[2]) < cutoff)
        if not links:
            return self
        if links not in self._proof_views:
            view = copy(self)
            view.groups = dict(self.groups)
            merge_known_result_groups(view.groups, self.matches, links, cutoff)
            view.index_groups = defaultdict(list)
            view.group_clocks = {}
            for row in self.indices:
                view.index_groups[view._group(row["match_id"])].append(row)
            for row in self.matches.values():
                group = view._group(row["match_id"])
                kickoff = instant(row["kickoff_utc"])
                first, last = view.group_clocks.get(group, (kickoff, kickoff))
                view.group_clocks[group] = min(first, kickoff), max(last, kickoff)
            self._proof_views[links] = view
        return self._proof_views[links]

    def _index(self, game):
        candidates = self.index_groups.get(self._group(game["match_id"]), [])
        if len(candidates) != 1:
            return (
                None,
                "fixture_identity_missing" if not candidates else "fixture_identity_ambiguous",
            )
        row = candidates[0]
        if any(row[key] != game[key] for key in ("league", "home_team_id", "away_team_id")) or abs(
            instant(row["kickoff_utc"]) - instant(game["kickoff_utc"])
        ) > timedelta(hours=3):
            return None, "fixture_identity_mismatch"
        return row, None

    @staticmethod
    def _row_available(row, cutoff, mode, replay_at):
        captured = instant(row["captured_utc"])
        basis = row["availability_basis"]
        if basis == "backfilled":
            effective = captured if mode == "strict" else replay_at
            reason = "backfilled_not_known" if mode == "strict" else "not_known_before_cutoff"
        elif basis == "captured":
            available = row.get("available_utc")
            if not available:
                return None, "missing_available_utc"
            effective = instant(available)
            if effective < captured or captured >= cutoff:
                return None, "not_known_before_cutoff"
            reason = "not_known_before_cutoff"
        else:
            return None, "unknown_availability_basis"
        return (effective, None) if effective < cutoff else (None, reason)

    def _snapshot(self, index, cutoff, mode, kickoff):
        pages = self.page_scopes.get((index["provider_league_id"], index["season"]), [])
        replay_at = kickoff - timedelta(microseconds=1)
        eligible = [
            row
            for row in pages
            if (instant(row["captured_utc"]) < cutoff if mode == "strict" else replay_at < cutoff)
        ]
        if not eligible:
            reason = "capture_receipt_missing"
            if pages:
                reason = (
                    "backfilled_not_known"
                    if mode == "strict" and not any(row["detail"]["daily"] for row in pages)
                    else "not_known_before_cutoff"
                )
            return [], {}, reason
        latest_time = max(instant(row["captured_utc"]) for row in eligible)
        latest = [row for row in eligible if instant(row["captured_utc"]) == latest_time]
        if len({row["raw_sha256"] for row in latest}) > 1:
            return [], {}, "ambiguous_capture_time"
        page = latest[-1]
        provenance = dict(
            captured_utc=page["captured_utc"],
            raw_sha256=page["raw_sha256"],
            raw_location=page["raw_location"],
            capture_record_key=page["record_key"],
            provider_fixture_id=index["provider_fixture_id"],
            effective_available_utc=(
                instant(page["captured_utc"]) if mode == "strict" else replay_at
            ).isoformat(),
            replay_assumption=REPLAY_ASSUMPTION if mode == "backfill_replay" else None,
        )
        detail = page["detail"]
        if detail["page_errors"]:
            return [], provenance, "incomplete_capture"
        if detail["empty"]:
            return [], provenance, "empty_capture"
        snapshot = detail["fixtures"].get(index["provider_fixture_id"])
        if snapshot is None:
            return [], provenance, "fixture_absent_from_capture"
        if snapshot["conflicts"]:
            return [], provenance, "same_day_content_conflict"
        if snapshot["rejected"]:
            return [], provenance, "incomplete_capture"
        if snapshot["unresolved"]:
            return [], provenance, "unresolved_injury_identity"
        rows, seen = [], set()
        for entry in snapshot["entries"]:
            row = self.injuries.get(entry["row_id"])
            if (
                not row
                or row["raw_sha256"] != entry["raw_sha256"]
                or row["version"] != entry["version"]
                or row["record_key"] != entry["record_key"]
                or not (
                    instant(row["captured_utc"])
                    <= instant(page["captured_utc"])
                    <= instant(row["last_captured_utc"])
                )
            ):
                return [], provenance, "capture_rows_missing"
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            side = row["side"]
            if (
                side not in {"home", "away"}
                or row["provider_fixture_id"] != index["provider_fixture_id"]
                or row["match_id"] != index["match_id"]
                or row["team_id"] != index[side + "_team_id"]
                or row["provider_team_id"] != index[side + "_provider_id"]
                or row["player_id"] is None
                or row["injury_type"] not in INJURY_TYPES
            ):
                return [], provenance, "unresolved_injury_identity"
            _, reason = self._row_available(row, cutoff, mode, replay_at)
            if reason:
                return [], provenance, reason
            rows.append(row)
        by_player = defaultdict(set)
        for row in rows:
            by_player[row["player_id"]].add((row["team_id"], row["injury_type"]))
        if any(len(values) > 1 for values in by_player.values()):
            return [], provenance, "ambiguous_player_status"
        bases = sorted({row["availability_basis"] for row in rows})
        provenance.update(
            availability_bases=bases, availability_basis=bases[0] if len(bases) == 1 else "mixed"
        )
        return rows, provenance, None

    def _previous(self, game, team, cutoff):
        groups = defaultdict(list)
        for previous in self.histories.get(team, []):
            if (
                previous["status"] == "finished"
                and team in (previous["home_team_id"], previous["away_team_id"])
                and self._group(previous["match_id"]) != self._group(game["match_id"])
            ):
                groups[self._group(previous["match_id"])].append(previous)
        representatives = []
        for group, rows in groups.items():
            earliest, latest = self.group_clocks[group]
            # Filter the completed fixture, not an earlier source copy that can
            # survive after its later counterpart has already been excluded.
            if latest + timedelta(hours=3) >= cutoff:
                continue
            representative = min(
                rows, key=lambda row: (SOURCE_ORDER.get(row["source"], 99), row["match_id"])
            )
            representatives.append(
                dict(
                    representative,
                    kickoff_utc=latest.isoformat(),
                    earliest_kickoff_utc=earliest.isoformat(),
                )
            )
        if not representatives:
            return None, "previous_finished_match_missing"
        latest = max(instant(row["kickoff_utc"]) for row in representatives)
        matches = [row for row in representatives if instant(row["kickoff_utc"]) == latest]
        return (matches[0], None) if len(matches) == 1 else (None, "previous_match_ambiguous")

    def _starters(self, previous, team, cutoff, mode):
        index, reason = self._index(previous)
        if reason:
            return None, {}, "previous_" + reason
        fixture_id = index["provider_fixture_id"]
        receipts = [
            row
            for row in self.lineup_capture_groups.get(fixture_id, [])
            if mode == "backfill_replay" or instant(row["captured_utc"]) < cutoff
        ]
        rows_by_key = defaultdict(list)
        for row in self.lineup_groups.get(fixture_id, []):
            rows_by_key[row["record_key"]].append(row)
        kickoff = instant(previous["kickoff_utc"])
        latest = self.group_clocks.get(self._group(previous["match_id"]), (kickoff, kickoff))[1]
        replay_at = max(kickoff, latest) + timedelta(hours=3)
        receipts.sort(key=lambda row: instant(row["captured_utc"]), reverse=True)
        for receipt in receipts:
            detail = receipt["detail"]
            provenance = {
                key: receipt[key] for key in ("captured_utc", "raw_sha256", "raw_location")
            }
            provenance.update(
                match_id=previous["match_id"],
                provider_fixture_id=fixture_id,
                replay_assumption="previous_kickoff_plus_3h" if mode == "backfill_replay" else None,
            )
            simultaneous = {
                row["raw_sha256"]
                for row in receipts
                if instant(row["captured_utc"]) == instant(receipt["captured_utc"])
            }
            if len(simultaneous) > 1:
                return None, provenance, "ambiguous_previous_lineup_capture"
            if detail.get("empty"):
                return None, provenance, "empty_previous_lineup_capture"
            if detail.get("rejected"):
                return None, provenance, "incomplete_previous_lineup_capture"
            selected = []
            unavailable = False
            for key in detail.get("record_keys", []):
                candidates = [
                    row
                    for row in rows_by_key.get(key, [])
                    if instant(row["captured_utc"]) <= instant(receipt["captured_utc"])
                ]
                if not candidates:
                    return None, provenance, "previous_lineup_capture_rows_missing"
                exact = [row for row in candidates if row["raw_sha256"] == receipt["raw_sha256"]]
                selected_row = max(
                    exact or candidates,
                    key=lambda row: (instant(row["captured_utc"]), row["version"]),
                )
                _, row_reason = self._row_available(selected_row, cutoff, mode, replay_at)
                if row_reason:
                    unavailable = True
                    break
                selected.append(selected_row)
            if unavailable:
                continue
            side = "home" if team == index["home_team_id"] else "away"
            selected = [row for row in selected if row["side"] == side]
            if not any(row["role"] == "team" for row in selected) or any(
                row["team_id"] != team
                or row["match_id"] != index["match_id"]
                or row["provider_team_id"] != index[side + "_provider_id"]
                for row in selected
            ):
                return None, provenance, "previous_lineup_missing"
            starters = [row for row in selected if row["role"] == "start"]
            ids = {row["player_id"] for row in starters}
            if None in ids or len(ids) != len(starters) or len(ids) != 11:
                return None, provenance, "previous_starters_incomplete"
            provenance["evidence"] = [self._evidence(row) for row in starters]
            return ids, provenance, None
        return None, {}, "previous_lineup_not_known" if receipts else "previous_lineup_missing"

    @staticmethod
    def _evidence(row):
        return {
            key: row[key]
            for key in (
                "id",
                "match_id",
                "team_id",
                "player_id",
                "captured_utc",
                "available_utc",
                "availability_basis",
                "raw_sha256",
                "raw_location",
                "version",
            )
        }

    def build(self, target, *, as_of, mode="strict"):
        game = dict(self.matches.get(target["match_id"], {}), **target)
        cutoff = min(instant(as_of), instant(game["kickoff_utc"]))
        view = self._proof_view(cutoff)
        earliest = view.group_clocks.get(view._group(game["match_id"]), (cutoff, cutoff))[0]
        cutoff = min(cutoff, earliest, instant(game.get("earliest_kickoff_utc", cutoff)))
        return self._proof_view(cutoff)._build(target, as_of=cutoff, mode=mode)

    def _build(self, target, *, as_of, mode="strict"):
        if mode not in {"strict", "backfill_replay"}:
            raise ValueError("伤停可知口径须为 strict/backfill_replay")
        game = dict(self.matches.get(target["match_id"], {}), **target)
        kickoff = instant(game["kickoff_utc"])
        earliest = self.group_clocks.get(self._group(game["match_id"]), (kickoff, kickoff))[0]
        # The host keeps the latest group kickoff for completed-match history,
        # but target evidence must be known before the earliest aligned kickoff.
        kickoff = min(kickoff, earliest, instant(game.get("earliest_kickoff_utc", kickoff)))
        cutoff = min(instant(as_of), kickoff)
        index, reason = self._index(game)
        rows, provenance = [], {}
        if reason is None:
            rows, provenance, reason = self._snapshot(index, cutoff, mode, kickoff)
        result = dict(
            feature_version=VERSION,
            provider="apif",
            match_id=game["match_id"],
            feature_cutoff=cutoff.isoformat(),
            availability_mode=mode,
            validation_status="historical_replay_unvalidated"
            if mode == "backfill_replay"
            else "experimental_unvalidated",
            replay_assumption=REPLAY_ASSUMPTION if mode == "backfill_replay" else None,
        )
        for side in ("home", "away"):
            side_rows = [row for row in rows if row["side"] == side]
            side_reason = reason or ("team_absent_from_capture" if not side_rows else None)
            evidence = [
                dict(self._evidence(row), injury_type=row["injury_type"]) for row in side_rows
            ]
            if side_reason:
                result[side] = {
                    key: _cell(reason=side_reason, provenance=provenance) for key in METRICS
                }
                continue
            missing = {
                row["player_id"] for row in side_rows if row["injury_type"] == "Missing Fixture"
            }
            questionable = {
                row["player_id"] for row in side_rows if row["injury_type"] == "Questionable"
            }
            cells = {
                "missing_n": _cell(
                    len(missing), n=len(missing), evidence=evidence, provenance=provenance
                ),
                "questionable_n": _cell(
                    len(questionable), n=len(questionable), evidence=evidence, provenance=provenance
                ),
            }
            previous, previous_reason = self._previous(game, game[side + "_team_id"], cutoff)
            starters, lineup = None, {}
            if previous is not None:
                starters, lineup, previous_reason = self._starters(
                    previous, game[side + "_team_id"], cutoff, mode
                )
            cells["missing_last_starters_n"] = _cell(
                len(missing & starters) if starters is not None else None,
                n=len(starters) if starters is not None else 0,
                reason=previous_reason,
                evidence=evidence,
                provenance=provenance,
                previous_lineup=lineup,
                previous_match_id=previous["match_id"] if previous else None,
            )
            result[side] = cells
        return result


def injury_inputs(features):
    """The two frozen home-minus-away model inputs and their missingness reasons."""
    values, reasons = {}, []
    for metric, name in (
        ("missing_n", "missing_diff"),
        ("missing_last_starters_n", "missing_last_starters_diff"),
    ):
        home, away = (features[side][metric] for side in ("home", "away"))
        values[name] = (
            home["value"] - away["value"]
            if home["value"] is not None and away["value"] is not None
            else None
        )
        for side in ("home", "away"):
            cell = features[side][metric]
            if cell["value"] is None:
                reasons.append(f"{side}_{metric}:{cell['missing_reason'] or 'missing'}")
    return values, sorted(set(reasons))
