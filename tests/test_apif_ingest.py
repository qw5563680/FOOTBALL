import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from zucai.db import get_engine, init_db, session_scope, upsert_match
from zucai.models import ClosingOdds, Match, Odds, Result, Team

SAMPLE = json.loads(
    (Path(__file__).resolve().parents[1] / "data/fixtures/t10/synthetic_ingest.json").read_bytes()
)
WHEN = datetime(2026, 10, 10, 14, tzinfo=UTC)
SEEN = datetime(2026, 10, 12, 10, tzinfo=UTC)
DELAY = timedelta(hours=3)
KEY = "apif:91001"


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    engine = get_engine(tmp_path / "fixtures.db")
    init_db(engine)
    with session_scope(engine) as s:
        s.add_all(Team(name=n) for n in ("北港", "东岸", "南川", "西山"))
    yield engine
    engine.dispose()


def game(**changes):
    from zucai.sources.api_common import ApiGame

    values = SAMPLE["fixture"] | changes
    if isinstance(values["kickoff_utc"], str):
        values["kickoff_utc"] = datetime.fromisoformat(values["kickoff_utc"])
    for key in ("full", "half"):
        if values[key] is not None:
            values[key] = tuple(values[key])
    return ApiGame(**values)


def ingest(s, *games, mapping=None, seen=SEEN, assume=None):
    from zucai.sources.apif_ingest import import_apif_games

    return import_apif_games(
        s,
        games,
        seen=seen,
        assume=assume,
        aliases=SAMPLE["aliases"] if mapping is None else mapping,
    )


def team(s, name):
    return s.scalars(select(Team).where(Team.name == name)).one()


def seed(s, key, home="北港", away="东岸", when=WHEN, league="日职", counterpart=None):
    upsert_match(
        s,
        match_id=key,
        source=key.split(":")[0],
        league=league,
        kickoff_utc=when,
        home_team_id=team(s, home).id,
        away_team_id=team(s, away).id,
        counterpart_id=counterpart,
        status="finished",
    )
    s.add(
        Result(
            match_id=key,
            home_goals=9,
            away_goals=0,
            available_utc=when + DELAY,
            available_source="published",
        )
    )
    s.flush()


def test_approved_and_auto_verified_use_only_numeric_ids_even_after_label_change(db):
    with session_scope(db) as s:
        result = ingest(s, game(home_raw="Renamed North", away_raw="Renamed East"))
        assert result["inserted"] == 1 and not result["unmatched_names"]
        match = s.get(Match, KEY)
        assert (match.home_team_id, match.away_team_id) == (team(s, "北港").id, team(s, "东岸").id)
        assert match.source == "apif" and match.status == "finished"
        score = s.get(Result, KEY)
        assert (
            score.available_source == "captured"
            and datetime.fromisoformat(score.available_utc) == SEEN
        )
        assert (score.home_goals, score.away_goals) == (2, 1)
        assert (score.ht_home_goals, score.ht_away_goals) == (1, 0)
        for table in (Odds, ClosingOdds):
            assert s.scalar(select(func.count()).select_from(table)) == 0


def test_proposed_ids_and_global_english_names_do_not_activate_aliases(db):
    mapping = deepcopy(SAMPLE["aliases"])
    mapping["日职"]["551"]["status"] = "proposed"
    mapping["日职"]["Eastbank FC"] = mapping["日职"].pop("552")
    with session_scope(db) as s:
        team(s, "北港").aliases.append("Northport FC")
        first = ingest(s, game(), mapping=mapping)
        assert first["inserted"] == 1
        assert first["unmatched_names"] == [
            {"league": "日职", "id": 551, "name": "Northport FC"},
            {"league": "日职", "id": 552, "name": "Eastbank FC"},
        ]
        match = s.get(Match, KEY)
        sides = match.home_team_id, match.away_team_id
        assert sides[0] != team(s, "北港").id and sides[1] != team(s, "东岸").id
        assert ingest(s, game(home_raw="New Name FC"), mapping=mapping)["unchanged"] == 1
        assert (match.home_team_id, match.away_team_id) == sides
        another = game(
            provider_match_id=91003,
            home_provider_id=553,
            kickoff_utc=WHEN - timedelta(days=7),
        )
        assert ingest(s, another, mapping=mapping)["inserted"] == 1
        assert s.get(Match, "apif:91003").home_team_id != sides[0]


@pytest.mark.parametrize("external,expected", [("jc:1", "linked_existing"), ("fd:1", "linked_fd")])
@pytest.mark.parametrize("hours", [-3, 3])
def test_exact_oriented_overlap_includes_both_endpoints_and_preserves_external_rows(
    db, external, expected, hours
):
    when = WHEN + timedelta(hours=hours)
    with session_scope(db) as s:
        seed(s, external, when=when)
        for _ in range(2):
            item = ingest(s, game())
            assert item[expected] == 1 and item["inserted"] == 0 and item["assumed"] == 0
            assert item["links"][0]["match_id"] == external
        assert s.get(Match, KEY) is None and s.get(Result, KEY) is None
        incumbent = s.get(Match, external)
        assert incumbent.counterpart_id == (KEY if external.startswith("jc:") else None)
        assert datetime.fromisoformat(incumbent.kickoff_utc) == when
        score = s.get(Result, external)
        assert (score.home_goals, score.away_goals, score.available_source) == (9, 0, "published")


def test_jc_wins_over_fd_and_preserves_existing_backup_link(db):
    with session_scope(db) as s:
        seed(s, "jc:1", counterpart="c500:42")
        seed(s, "fd:1")
        item = ingest(s, game())
        assert item["linked_existing"] == 1 and item["linked_fd"] == 0
        assert s.get(Match, "jc:1").counterpart_id == "c500:42"
        assert s.get(Match, "fd:1").counterpart_id is None
        assert s.get(Result, "jc:1").home_goals == s.get(Result, "fd:1").home_goals == 9


@pytest.mark.parametrize("case", ["conflicting_fd", "two_matching_fd"])
def test_unique_official_target_wins_even_when_lower_priority_fd_is_ambiguous(db, case):
    with session_scope(db) as s:
        seed(s, "jc:1")
        seed(s, "fd:1", away="南川" if case == "conflicting_fd" else "东岸")
        if case == "two_matching_fd":
            seed(s, "fd:2")
        result = ingest(s, game())
        assert result["linked_existing"] == 1 and result["suspected_duplicates"] == 0
        assert s.get(Match, "jc:1").counterpart_id == KEY
        assert s.get(Match, "fd:1").counterpart_id is None and s.get(Match, KEY) is None


def test_single_approved_side_without_external_candidate_is_independent_and_audited(db):
    mapping = deepcopy(SAMPLE["aliases"])
    mapping["日职"]["552"]["status"] = "proposed"
    with session_scope(db) as s:
        result = ingest(s, game(), mapping=mapping)
        assert result["inserted"] == 1 and result["suspected_duplicates"] == 0
        assert result["unmatched_names"] == [{"league": "日职", "id": 552, "name": "Eastbank FC"}]
        assert s.get(Match, KEY).home_team_id == team(s, "北港").id
        assert s.get(Match, KEY).away_team_id != team(s, "东岸").id


def test_two_incoming_ids_cannot_both_link_one_incumbent_or_choose_the_first(db):
    with session_scope(db) as s:
        seed(s, "jc:1")
        result = ingest(s, game(), game(provider_match_id=91234))
        assert result["suspected_duplicates"] == 2 and result["linked_existing"] == 0
        assert s.get(Match, "jc:1").counterpart_id is None and s.get(Match, KEY) is None


@pytest.mark.parametrize("external", ["jc:1", "fd:1"])
def test_candidates_on_opposite_window_edges_still_contest_the_same_external_id(db, external):
    with session_scope(db) as s:
        seed(s, external)
        result = ingest(
            s,
            game(kickoff_utc=WHEN - DELAY),
            game(provider_match_id=91234, kickoff_utc=WHEN + DELAY),
        )
        assert result["suspected_duplicates"] == 2
        assert result["linked_existing"] == result["linked_fd"] == 0
        assert s.get(Match, external).counterpart_id is None


def test_two_active_provider_ids_sharing_one_canonical_cannot_link_official_match(db):
    mapping = deepcopy(SAMPLE["aliases"])
    mapping["日职"]["553"] = {"name": "Another", "canonical": "北港", "status": "approved"}
    with session_scope(db) as s:
        seed(s, "jc:1")
        result = ingest(s, game(), mapping=mapping)
        assert result["rejected_rows"] == 1 and result["linked_existing"] == 0
        assert s.get(Match, "jc:1").counterpart_id is None


def test_unverified_english_fd_teams_with_matching_labels_are_suspected_but_never_linked(db):
    with session_scope(db) as s:
        s.add_all([Team(name="Northport"), Team(name="Eastbank")])
        seed(s, "fd:1", home="Northport", away="Eastbank")
        result = ingest(s, game())
        assert result["suspected_duplicates"] == 1 and result["linked_fd"] == 0
        assert s.get(Match, KEY) is None and s.get(Result, "fd:1").home_goals == 9


@pytest.mark.parametrize("external", ["jc:1", "fd:1"])
@pytest.mark.parametrize("case", ["reversed", "one_team", "proposed", "no_aliases", "ambiguous"])
def test_conflicting_partial_and_ambiguous_overlaps_are_audited_before_any_team_write(
    db, external, case
):
    mapping = deepcopy(SAMPLE["aliases"])
    with session_scope(db) as s:
        home, away = {
            "reversed": ("东岸", "北港"),
            "one_team": ("南川", "东岸"),
        }.get(case, ("北港", "东岸"))
        seed(s, external, home, away)
        if case == "ambiguous":
            seed(s, external + "2")
        if case == "proposed":
            mapping["日职"]["551"]["status"] = "proposed"
        if case == "no_aliases":
            mapping = {}
        result = ingest(s, game(), mapping=mapping)
        assert (
            result["suspected_duplicates"] == 1 and external in result["suspected"][0]["candidates"]
        )
        assert s.get(Match, KEY) is None and s.get(Match, external).counterpart_id is None
        assert s.scalar(select(func.count()).select_from(Team)) == 4
        if case == "no_aliases":
            assert {row["id"] for row in result["unmatched_names"]} == {551, 552}


def test_different_pairs_in_same_round_and_just_outside_window_are_independent(db):
    with session_scope(db) as s:
        seed(s, "jc:1", home="南川", away="西山")
        seed(s, "fd:1", when=WHEN + DELAY + timedelta(microseconds=1))
        seed(s, "jc:2", league="英冠")
        assert ingest(s, game())["inserted"] == 1
        assert s.get(Result, KEY).home_goals == 2


@pytest.mark.parametrize("external", ["jc:late", "fd:late"])
def test_new_external_duplicate_of_existing_apif_result_requires_reconciliation(db, external):
    with session_scope(db) as s:
        assert ingest(s, game())["inserted"] == 1
        seed(s, external)
        result = ingest(s, game(full=(6, 1)), seen=SEEN + DELAY)
        assert result["suspected_duplicates"] == 1
        assert s.get(Match, external).counterpart_id is None
        assert s.get(Result, external).home_goals == 9 and s.get(Result, KEY).home_goals == 2


def test_existing_apif_counterpart_cannot_move_to_another_fixture(db):
    with session_scope(db) as s:
        seed(s, "jc:earlier", when=WHEN - timedelta(days=1), counterpart=KEY)
        result = ingest(s, game())
        assert result["suspected_duplicates"] == 1 and s.get(Match, KEY) is None
        assert s.get(Match, "jc:earlier").counterpart_id == KEY


def test_two_provider_match_ids_cannot_claim_same_external_or_native_fixture(db):
    with session_scope(db) as s:
        seed(s, "jc:1", counterpart="apif:456")
        assert ingest(s, game())["suspected_duplicates"] == 1
        assert s.get(Match, "jc:1").counterpart_id == "apif:456"
        earlier = WHEN - timedelta(days=1)
        assert ingest(s, game(kickoff_utc=earlier))["inserted"] == 1
        assert (
            ingest(s, game(kickoff_utc=earlier, provider_match_id=91234))["suspected_duplicates"]
            == 1
        )
        assert s.get(Match, "apif:91234") is None


def test_activating_alias_after_own_history_requires_reconciliation_even_on_new_game(db):
    with session_scope(db) as s:
        assert ingest(s, game(), mapping={})["inserted"] == 1
        new = game(provider_match_id=91002, kickoff_utc=WHEN - timedelta(days=7))
        rejected = ingest(s, new)
        assert rejected["rejected_rows"] == 1 and rejected["rejected"][0]["reason"]
        assert s.get(Match, "apif:91002") is None
        assert s.get(Match, KEY).home_team_id != team(s, "北港").id


def test_existing_provider_id_cannot_change_teams_or_bind_two_ids_to_one_team(db):
    remapped = deepcopy(SAMPLE["aliases"])
    remapped["日职"]["551"]["canonical"] = "南川"
    with session_scope(db) as s:
        assert ingest(s, game())["inserted"] == 1
        next_game = game(provider_match_id=91234, kickoff_utc=WHEN - timedelta(days=7))
        assert ingest(s, next_game, mapping=remapped)["rejected_rows"] == 1
        merged = deepcopy(SAMPLE["aliases"])
        merged["日职"]["553"] = merged["日职"].pop("551")
        assert (
            ingest(s, game(provider_match_id=91234, home_provider_id=553), mapping=merged)[
                "rejected_rows"
            ]
            == 1
        )
        assert s.get(Match, "apif:91234") is None
        assert s.get(Match, KEY).home_team_id == team(s, "北港").id


def test_schedule_postpone_relist_and_finish_never_regress_on_late_or_stale_schedules(db):
    expected = [
        ("scheduled", False),
        ("cancelled", False),
        ("scheduled", False),
        ("scheduled", False),
        ("finished", True),
        ("finished", True),
    ]
    for obs, (status, has_score) in zip(SAMPLE["observations"], expected, strict=True):
        when = datetime.fromisoformat(obs["seen"])
        fields = {k: v for k, v in obs.items() if k != "seen"}
        with session_scope(db) as s:
            item = ingest(s, game(**fields), seen=when)
            assert item["rejected_rows"] == item["suspected_duplicates"] == 0
            match = s.get(Match, KEY)
            assert match.status == status
            assert (s.get(Result, KEY) is not None) == has_score
    with session_scope(db) as s:
        assert datetime.fromisoformat(s.get(Match, KEY).kickoff_utc) == WHEN + timedelta(days=1)
        assert s.get(Result, KEY).available_utc.startswith("2026-10-12T12:00:00")


@pytest.mark.parametrize("status", ["PST", "CANC", "ABD", "AWD", "WO"])
def test_cancelled_fixtures_have_no_result_and_awarded_rows_are_rejected(db, status):
    with session_scope(db) as s:
        summary = ingest(s, game(status=status), mapping={})
        rejected = status in {"AWD", "WO"}
        assert summary["rejected_rows" if rejected else "cancelled"] == 1
        assert s.get(Result, KEY) is None
        if rejected:
            assert s.get(Match, KEY) is None
        else:
            assert s.get(Match, KEY).status == "cancelled"


@pytest.mark.parametrize("prior_schedule", [True, False])
@pytest.mark.parametrize(
    "status",
    ["1H", "HT", "ET", "P", "SUSP", "NS", "TBD"],
)
def test_observed_unsettled_fixture_blocks_an_earlier_assumed_finish(db, prior_schedule, status):
    timeline = {k: datetime.fromisoformat(v) for k, v in SAMPLE["delayed_finish"].items()}
    if prior_schedule:
        with session_scope(db) as s:
            ingest(s, game(status="NS"), seen=timeline["scheduled_seen"])
    with session_scope(db) as s:
        summary = ingest(s, game(status=status), seen=timeline["unsettled_seen"])
        assert summary["rejected_rows"] == summary["suspected_duplicates"] == 0
        pending = s.get(Match, KEY)
        assert pending.status == "scheduled" and s.get(Result, KEY) is None
        assert datetime.fromisoformat(pending.kickoff_utc) == WHEN <= timeline["unsettled_seen"]
        assert datetime.fromisoformat(pending.result_checked_utc) == timeline["unsettled_seen"]
    with session_scope(db) as s:
        assert ingest(s, game(), seen=timeline["older_finish_seen"], assume=DELAY)["unchanged"] == 1
        assert s.get(Result, KEY) is None
    with session_scope(db) as s:
        summary = ingest(s, game(), seen=timeline["finish_seen"], assume=DELAY)
        assert summary["assumed"] == 0
        score = s.get(Result, KEY)
        assert score.available_source == "captured"
        assert datetime.fromisoformat(score.available_utc) == timeline["finish_seen"]


def test_live_before_kickoff_or_with_changed_identity_or_kickoff_cannot_advance_the_clock(db):
    seen = WHEN - timedelta(hours=1)
    with session_scope(db) as s:
        assert ingest(s, game(status="1H"), seen=seen)["rejected_rows"] == 1
        assert s.get(Match, KEY) is None
        ingest(s, game(status="NS"), seen=seen)
        for changed in (
            {"kickoff_utc": WHEN + timedelta(hours=1)},
            {"home_provider_id": 553},
        ):
            result = ingest(s, game(status="SUSP", **changed), seen=WHEN + 2 * DELAY)
            assert result["rejected_rows"] == 1
            assert datetime.fromisoformat(s.get(Match, KEY).result_checked_utc) == seen
        assert s.get(Result, KEY) is None


@pytest.mark.parametrize("status", ["1H", "NS"])
def test_replaying_disproved_assumption_uses_later_confirmed_finish_without_changing_score(
    db, status
):
    with session_scope(db) as s:
        assert ingest(s, game(), seen=SEEN, assume=DELAY)["assumed"] == 1
        later_than_assumed = WHEN + timedelta(hours=4)
        replay = ingest(s, game(status=status), seen=later_than_assumed)
        assert (
            replay["availability_corrected"] == 1
            and replay["assumed"] == replay["rejected_rows"] == 0
        )
        score = s.get(Result, KEY)
        assert (score.home_goals, score.away_goals, score.available_source) == (2, 1, "captured")
        assert (score.ht_home_goals, score.ht_away_goals) == (1, 0)
        assert datetime.fromisoformat(score.available_utc) == SEEN
        assert s.get(Match, KEY).status == "finished"
        assert datetime.fromisoformat(s.get(Match, KEY).result_checked_utc) == SEEN
        ingest(s, game(status=status), seen=later_than_assumed)
        assert datetime.fromisoformat(score.available_utc) == SEEN


@pytest.mark.parametrize(
    "recorded,provider", [(None, "apif"), (SEEN, None), (WHEN + DELAY, "apif")]
)
def test_disproved_assumption_without_later_final_is_quarantined_until_a_fresh_finish(
    db, recorded, provider
):
    with session_scope(db) as s:
        ingest(s, game(), seen=SEEN, assume=DELAY)
        match = s.get(Match, KEY)
        match.result_checked_utc, match.result_provider = recorded, provider
    disputed = WHEN + DELAY
    with session_scope(db) as s:
        first = ingest(s, game(status="SUSP"), seen=disputed)
        assert first["rejected_rows"] == first["availability_quarantined"] == 1
        assert first["rejected"][0]["reason"]
        score = s.get(Result, KEY)
        assert score.available_utc is score.available_source is None
        assert (score.home_goals, score.away_goals, s.get(Match, KEY).status) == (2, 1, "finished")
    with session_scope(db) as s:
        ingest(s, game(status="1H"), seen=SEEN + DELAY)
        ingest(s, game(), seen=SEEN, assume=DELAY)
        assert s.get(Result, KEY).available_utc is None
        later = SEEN + 2 * DELAY
        fresh = ingest(s, game(), seen=later, assume=DELAY)
        score = s.get(Result, KEY)
        assert fresh["assumed"] == 0 and score.available_source == "captured"
        assert datetime.fromisoformat(score.available_utc) == later


@pytest.mark.parametrize(
    "changed", [{"kickoff_utc": WHEN + timedelta(hours=1)}, {"home_provider_id": 553}]
)
def test_conflicting_identity_cannot_retime_a_stored_assumed_finish(db, changed):
    with session_scope(db) as s:
        ingest(s, game(), assume=DELAY)
        ingest(s, game(status="1H", **changed), seen=WHEN + timedelta(hours=4))
        score = s.get(Result, KEY)
        assert (
            score.available_source == "assumed"
            and datetime.fromisoformat(score.available_utc) == WHEN + DELAY
        )


@pytest.mark.parametrize("status", ["ET", "P"])
def test_live_overtime_status_does_not_replace_a_final_regulation_score(db, status):
    with session_scope(db) as s:
        ingest(s, game())
        result = ingest(s, game(status=status, full=(5, 4)), seen=SEEN + DELAY)
        assert result["skipped"] == 1 and result["rejected_rows"] == 0
        match, score = s.get(Match, KEY), s.get(Result, KEY)
        assert (score.home_goals, score.away_goals) == (2, 1)
        assert (
            match.status == "finished" and datetime.fromisoformat(match.result_checked_utc) == SEEN
        )


def test_unchanged_keeps_assumption_and_half_correction_uses_new_capture(db):
    with session_scope(db) as s:
        assert ingest(s, game(), assume=DELAY)["assumed"] == 1
        assert ingest(s, game(half=None), seen=SEEN + DELAY)["unchanged"] == 1
        result = s.get(Result, KEY)
        assert (
            result.available_source == "assumed"
            and datetime.fromisoformat(result.available_utc) == WHEN + DELAY
        )
        assert result.ht_home_goals == 1
        newer = SEEN + 2 * DELAY
        fixed = ingest(s, game(half=(0, 0)), seen=newer, assume=DELAY)
        assert fixed["corrected"] == 1 and fixed["assumed"] == 0
        assert result.ht_home_goals == 0 and result.available_source == "captured"
        assert datetime.fromisoformat(result.available_utc) == newer
        assert ingest(s, game(), seen=SEEN)["unchanged"] == 1
        assert ingest(s, game(), seen=newer)["rejected_rows"] == 1
        assert result.ht_home_goals == 0 and result.home_goals == 2
        void = ingest(s, game(status="CANC"), seen=newer + DELAY)
        assert void["cancelled"] == 1 and s.get(Result, KEY) is None
        assert ingest(s, game(), seen=newer)["unchanged"] == 1
        assert s.get(Match, KEY).status == "cancelled"


def test_scheduled_observation_cannot_supersede_newer_fixture_or_equal_time_conflict(db):
    with session_scope(db) as s:
        next_week = WHEN + timedelta(days=7)
        assert ingest(s, game(status="NS", kickoff_utc=next_week))["inserted"] == 1
        assert ingest(s, game(status="NS"), seen=SEEN - DELAY)["unchanged"] == 1
        assert ingest(s, game(status="NS"), seen=SEEN)["rejected_rows"] == 1
        assert datetime.fromisoformat(s.get(Match, KEY).kickoff_utc) == next_week


def test_legacy_result_with_no_check_clock_cannot_be_replaced_by_older_capture(db):
    with session_scope(db) as s:
        seed(s, KEY)
        old = s.get(Result, KEY)
        old.available_utc = SEEN + DELAY
        ingest(s, game(), seen=SEEN)
        assert old.home_goals == 9 and old.available_source == "published"
        s.get(Match, KEY).result_provider = "jc"
        ingest(s, game(status="CANC"), seen=SEEN + 2 * DELAY)
        assert s.get(Result, KEY).home_goals == 9 and s.get(Match, KEY).status == "finished"


def test_legacy_captured_result_with_missing_time_cannot_be_downgraded_to_assumed(db):
    with session_scope(db) as s:
        seed(s, KEY)
        old = s.get(Result, KEY)
        old.available_utc = None
        old.available_source = "captured"
        fresh = ingest(s, game(full=(9, 0), half=None), assume=DELAY)
        assert fresh["assumed"] == 0 and s.get(Result, KEY).available_source == "captured"
        assert datetime.fromisoformat(s.get(Result, KEY).available_utc) == SEEN


@pytest.mark.parametrize("delay", [timedelta(0), timedelta(days=-1), timedelta(days=15), "3h"])
def test_invalid_assumptions_fail_before_any_writes(db, delay):
    with session_scope(db) as s, pytest.raises(ValueError, match="assume"):
        ingest(s, assume=delay)


def test_row_savepoint_rolls_back_teams_result_and_audit_counter_on_constraint_error(db):
    with session_scope(db) as s:
        s.connection().exec_driver_sql(
            "CREATE TRIGGER refuse_one BEFORE INSERT ON results WHEN NEW.match_id='apif:91002' "
            "BEGIN SELECT RAISE(ABORT, 'test constraint'); END"
        )
        bad = game(provider_match_id=91002, home_provider_id=553, away_provider_id=554)
        item = ingest(s, bad, game(), mapping={})
        assert (item["inserted"], item["rejected_rows"], len(item["outcomes"])) == (1, 1, 1)
        assert s.get(Match, "apif:91002") is None and s.get(Result, KEY) is not None
        assert {row["id"] for row in item["unmatched_names"]} == {551, 552}
        assert s.scalar(select(func.count()).select_from(Team)) == 6


def test_outer_transaction_still_owns_valid_rows_after_savepoint_release(db):
    with pytest.raises(RuntimeError), session_scope(db) as s:
        assert ingest(s, game())["inserted"] == 1
        raise RuntimeError("caller aborts the file")
    with session_scope(db) as s:
        assert s.get(Match, KEY) is None and s.get(Result, KEY) is None


@pytest.mark.parametrize(
    "higher,expected", [("jc", "linked_existing"), ("fd", "linked_fd"), ("fdorg", "linked_fdorg")]
)
def test_four_source_priority_preserves_higher_score(db, higher, expected):
    with session_scope(db) as s:
        seed(s, higher + ":first")
        result = ingest(s, game())
        assert result[expected] == 1
        assert s.get(Match, KEY) is None
        assert s.get(Result, higher + ":first").home_goals == 9


@pytest.mark.parametrize("state", ["AET", "PEN"])
def test_extra_time_and_penalties_import_only_regulation_score(db, state):
    with session_scope(db) as s:
        result = ingest(s, game(status=state))
        assert result["inserted"] == 1
        assert s.get(Result, KEY).home_goals == 2


def test_unresolved_fdorg_identity_vetoes_even_fully_mapped_apif(db):
    with session_scope(db) as s:
        s.add_all([Team(name="fdorg:team:71"), Team(name="fdorg:team:72")])
        s.flush()
        seed(s, "fdorg:pending", home="fdorg:team:71", away="fdorg:team:72")
        summary = ingest(s, game())
        assert summary["suspected_duplicates"] == 1 and s.get(Match, KEY) is None
