"""Pre-match injury lists are complete snapshots, never unions of old player rows."""

import json

import pytest

from test_injuries_storage import CAPTURE, FIX, KICKOFF, document, ingest
from test_injuries_storage import injury_database as injury_database
from zucai.db import get_engine, session_scope
from zucai.features.injuries import InjuryFeatures, injury_inputs
from zucai.injuries_storage import AUDIT_NAME
from zucai.models import Match
from zucai.predict.experiments.history import unified_history
from zucai.sources.apif_stats import instant
from zucai.sources.base import RawStore
from zucai.stats_storage import ingest_apif


def lineup(database, *, fixture_id=101, captured="2024-09-03T12:00:00Z", data=None, daily=False):
    if data is None:
        data = json.loads((FIX / "injuries_lineups.json").read_bytes())
        if fixture_id == 102:
            data["response"][0]["team"]["id"] = 2
            for field in ("startXI", "substitutes"):
                for row in data["response"][0][field]:
                    row["player"]["id"] += 10
    data["parameters"]["fixture"] = str(fixture_id)
    path = RawStore(database.parent / "raw").save(
        "api-football",
        "lineups",
        json.dumps(data).encode(),
        "json",
        captured_at=instant(captured),
        status_code=200,
        url=f"https://v3.football.api-sports.io/fixtures/lineups?fixture={fixture_id}",
    )
    return ingest_apif(database, path, endpoint="lineups", daily=daily)


def build(database, *, mode="strict", as_of=KICKOFF, match_id="fd:target"):
    engine = get_engine(database)
    with session_scope(engine) as session:
        features = InjuryFeatures(session)
    engine.dispose()
    return features.build(features.matches[match_id], as_of=as_of, mode=mode)


def setup_complete(database, **kwargs):
    lineup(database)
    lineup(database, fixture_id=102)
    ingest(database, **kwargs)


def test_counts_and_previous_starter_intersection_with_counterpart_dedup(injury_database):
    setup_complete(injury_database)
    row = build(injury_database)
    assert row["home"]["missing_n"]["value"] == 2
    assert row["home"]["questionable_n"]["value"] == 1
    assert row["away"]["questionable_n"]["value"] == 0
    assert row["home"]["missing_last_starters_n"]["value"] == 1
    assert row["away"]["missing_last_starters_n"]["value"] == 1
    assert row["home"]["missing_last_starters_n"]["previous_match_id"] in (
        "fd:homeprev",
        "jc:homeprev",
    )
    assert injury_inputs(row) == ({"missing_diff": 1, "missing_last_starters_diff": 0}, [])
    evidence = row["home"]["missing_n"]["evidence"]
    assert evidence[0]["availability_basis"] == "captured"
    assert evidence[0]["raw_sha256"] and evidence[0]["captured_utc"]


@pytest.mark.parametrize("cutoff", [CAPTURE, "2024-09-08T12:00:00Z"])
def test_strict_capture_must_be_strictly_before_cutoff(injury_database, cutoff):
    setup_complete(injury_database)
    row = build(injury_database, as_of=cutoff)
    assert row["home"]["missing_n"]["value"] is None
    assert row["home"]["missing_n"]["missing_reason"] == "not_known_before_cutoff"
    assert row["feature_cutoff"] == instant(cutoff).isoformat()


def test_as_of_after_kickoff_still_caps_all_inputs_at_kickoff(injury_database):
    setup_complete(injury_database, captured=KICKOFF)
    row = build(injury_database, as_of="2024-09-12T12:00:00Z")
    assert row["feature_cutoff"] == instant(KICKOFF).isoformat()
    assert row["home"]["missing_n"]["value"] is None


def test_strict_uses_pre_cutoff_backfill_without_relabeling_and_replay_is_explicit(
    injury_database,
):
    setup_complete(injury_database, daily=False)
    strict = build(injury_database)
    assert strict["home"]["missing_n"]["value"] == 2
    assert strict["home"]["missing_n"]["availability_basis"] == "backfilled"
    assert strict["home"]["missing_n"]["evidence"][0]["available_utc"] is None
    replay = build(injury_database, mode="backfill_replay")
    assert replay["home"]["missing_n"]["value"] == 2
    assert replay["replay_assumption"]
    assert replay["availability_mode"] == "backfill_replay"


def test_late_historical_page_is_excluded_by_strict_but_allowed_by_replay(injury_database):
    setup_complete(injury_database, daily=False, captured="2026-11-04T12:00:00Z")
    strict, replay = build(injury_database), build(injury_database, mode="backfill_replay")
    assert strict["home"]["missing_n"]["value"] is None
    assert strict["home"]["missing_n"]["missing_reason"] == "backfilled_not_known"
    assert replay["home"]["missing_n"]["value"] == 2
    assert replay["home"]["missing_last_starters_n"]["value"] == 1
    assert replay["home"]["missing_n"]["evidence"][0]["captured_utc"].startswith("2026")


def test_latest_capture_list_does_not_union_removed_players(injury_database):
    setup_complete(injury_database)
    data = document()
    data["response"] = data["response"][1:]
    data["results"] = 3
    ingest(injury_database, data=data, captured="2024-09-09T11:00:00Z")
    row = build(injury_database)
    assert row["home"]["missing_n"]["value"] == 1
    assert row["home"]["missing_last_starters_n"]["value"] == 0
    assert {e["player_id"] for e in row["home"]["missing_n"]["evidence"]} == {11, 12}
    assert row["home"]["missing_n"]["captured_utc"].startswith("2024-09-09T11")


def test_type_change_uses_latest_list_only(injury_database):
    setup_complete(injury_database)
    data = document()
    data["response"][1]["player"]["type"] = "Missing Fixture"
    ingest(injury_database, data=data, captured="2024-09-09T11:00:00Z")
    row = build(injury_database)
    assert row["home"]["missing_n"]["value"] == 3
    assert row["home"]["questionable_n"]["value"] == 0
    assert row["home"]["missing_last_starters_n"]["value"] == 2


def test_same_day_revision_selects_exact_content_before_each_cutoff(injury_database):
    setup_complete(injury_database)
    data = document()
    data["response"][0]["player"]["type"] = "Questionable"
    ingest(injury_database, data=data, captured="2024-09-09T11:00:00Z")
    assert build(injury_database)["home"]["missing_n"]["value"] == 1
    for cutoff in ("2024-09-09T10:30:00Z", "2024-09-09T11:00:00Z"):
        assert build(injury_database, as_of=cutoff)["home"]["missing_n"]["value"] == 2
    ingest(injury_database, captured="2024-09-09T12:00:00Z")
    assert build(injury_database)["home"]["missing_n"]["value"] == 2
    assert build(injury_database, as_of="2024-09-09T11:30:00Z")["home"]["missing_n"]["value"] == 1


@pytest.mark.parametrize(
    "variant,reason",
    [
        ("empty", "empty_capture"),
        ("other_fixture", "fixture_absent_from_capture"),
        ("unlinked", "unresolved_injury_identity"),
        ("unknown_type", "unresolved_injury_identity"),
        ("invalid_row", "incomplete_capture"),
    ],
)
def test_latest_incomplete_capture_never_means_healthy_zero(injury_database, variant, reason):
    setup_complete(injury_database)
    data = document()
    if variant == "empty":
        data.update(response=[], results=0)
    elif variant == "other_fixture":
        for row in data["response"]:
            row["fixture"]["id"] = 999
    elif variant == "unlinked":
        data["response"][0]["player"]["id"] = None
    elif variant == "unknown_type":
        data["response"][0]["player"]["type"] = "Unknown Provider Status"
    else:
        data["response"][0]["fixture"]["date"] = "not-a-time"
    ingest(injury_database, data=data, captured="2024-09-10T10:00:00Z")
    row = build(injury_database)
    assert row["home"]["missing_n"]["value"] is None
    assert row["home"]["missing_n"]["missing_reason"] == reason
    assert injury_inputs(row)[0]["missing_diff"] is None


def test_absent_team_and_missing_receipt_remain_null(injury_database):
    data = document()
    data["response"] = data["response"][:3]
    data["results"] = 3
    ingest(injury_database, data=data)
    row = build(injury_database)
    assert row["away"]["missing_n"]["missing_reason"] == "team_absent_from_capture"
    assert row["home"]["missing_n"]["value"] == 2
    (injury_database.parent / "state/audit" / AUDIT_NAME).unlink()
    assert (
        build(injury_database)["home"]["missing_n"]["missing_reason"] == "capture_receipt_missing"
    )


def test_latest_previous_lineup_does_not_reuse_removed_starter(injury_database):
    setup_complete(injury_database)
    data = json.loads((FIX / "injuries_lineups.json").read_bytes())
    data["response"][0]["startXI"][0]["player"]["id"] = 13
    lineup(injury_database, data=data, captured="2024-09-04T12:00:00Z")
    cell = build(injury_database)["home"]["missing_last_starters_n"]
    assert cell["value"] == 0
    ids = {r["player_id"] for r in cell["previous_lineup"]["evidence"]}
    assert len(ids) == 11 and 10 not in ids and {11, 13} <= ids


def test_different_lineup_pages_at_same_capture_instant_are_ambiguous(injury_database):
    setup_complete(injury_database)
    data = json.loads((FIX / "injuries_lineups.json").read_bytes())
    data["response"][0]["startXI"][0]["player"]["id"] = 13
    lineup(injury_database, data=data)
    cell = build(injury_database)["home"]["missing_last_starters_n"]
    assert cell["value"] is None
    assert cell["missing_reason"] == "ambiguous_previous_lineup_capture"


def test_caller_aligned_kickoff_controls_feature_cutoff_and_replay(injury_database):
    setup_complete(injury_database, daily=False)
    engine = get_engine(injury_database)
    with session_scope(engine) as session:
        builder = InjuryFeatures(session)
    engine.dispose()
    target = dict(builder.matches["fd:target"], kickoff_utc="2024-09-10T11:00:00Z")
    result = builder.build(target, as_of=KICKOFF, mode="backfill_replay")
    assert result["feature_cutoff"] == "2024-09-10T11:00:00+00:00"
    assert result["home"]["missing_n"]["value"] == 2


def test_empty_latest_lineup_does_not_reuse_older_complete_capture(injury_database):
    setup_complete(injury_database)
    data = json.loads((FIX / "injuries_lineups.json").read_bytes())
    data.update(response=[], results=0)
    lineup(injury_database, data=data, captured="2024-09-04T12:00:00Z")
    cell = build(injury_database)["home"]["missing_last_starters_n"]
    assert cell["value"] is None and cell["missing_reason"] == "empty_previous_lineup_capture"


def test_partial_previous_starting_eleven_is_unknown(injury_database):
    setup_complete(injury_database)
    data = json.loads((FIX / "injuries_lineups.json").read_bytes())
    data["response"][0]["startXI"] = data["response"][0]["startXI"][:2]
    lineup(injury_database, data=data, captured="2024-09-04T12:00:00Z")
    cell = build(injury_database)["home"]["missing_last_starters_n"]
    assert cell["value"] is None and cell["missing_reason"] == "previous_starters_incomplete"


def test_dual_player_status_in_selected_capture_is_unknown(injury_database):
    setup_complete(injury_database)
    data = document()
    extra = json.loads(json.dumps(data["response"][0]))
    extra["player"]["type"] = "Questionable"
    data["response"].append(extra)
    data["results"] += 1
    ingest(injury_database, data=data, captured="2024-09-09T11:00:00Z")
    cell = build(injury_database)["home"]["missing_n"]
    assert cell["value"] is None and cell["missing_reason"] == "ambiguous_player_status"


def test_strict_lineup_capture_at_cutoff_is_excluded(injury_database):
    ingest(injury_database)
    lineup(injury_database, captured=KICKOFF, daily=False)
    cell = build(injury_database)["home"]["missing_last_starters_n"]
    assert cell["value"] is None
    assert (
        build(injury_database, mode="backfill_replay")["home"]["missing_last_starters_n"]["value"]
        == 1
    )


def test_actual_previous_match_without_lineup_does_not_fall_back(injury_database):
    setup_complete(injury_database)
    engine = get_engine(injury_database)
    with session_scope(engine) as session:
        session.add(
            Match(
                match_id="fd:newprevious",
                source="fd",
                league="英超",
                home_team_id=1,
                away_team_id=3,
                kickoff_utc="2024-09-08T12:00:00Z",
                status="finished",
            )
        )
    engine.dispose()
    cell = build(injury_database)["home"]["missing_last_starters_n"]
    assert cell["value"] is None
    assert cell["previous_match_id"] == "fd:newprevious"
    assert cell["missing_reason"] == "previous_fixture_identity_missing"


def test_finished_match_plus_three_hours_is_strict_boundary(injury_database):
    ingest(injury_database, captured="2024-08-30T10:00:00Z")
    lineup(injury_database, captured="2024-08-31T12:00:00Z")
    lineup(injury_database, fixture_id=103, captured="2024-08-21T12:00:00Z")
    boundary = build(injury_database, as_of="2024-09-01T15:00:00Z")
    after = build(injury_database, as_of="2024-09-01T15:00:00.000001Z")
    assert boundary["home"]["missing_last_starters_n"]["previous_match_id"] == "fd:older"
    assert after["home"]["missing_last_starters_n"]["previous_match_id"] in (
        "fd:homeprev",
        "jc:homeprev",
    )


@pytest.mark.parametrize("later_match_id", ["jc:homeprev", "fd:homeprev"])
@pytest.mark.parametrize(
    "cutoff,expected",
    [
        ("2024-09-01T15:30:00Z", None),
        ("2024-09-01T16:00:00Z", None),
        ("2024-09-01T16:00:00.000001Z", 1),
    ],
)
def test_previous_counterparts_use_latest_group_kickoff_before_filtering(
    injury_database, later_match_id, cutoff, expected
):
    ingest(injury_database, captured="2024-08-30T10:00:00Z")
    lineup(injury_database, captured="2024-09-01T11:30:00Z")
    engine = get_engine(injury_database)
    with session_scope(engine) as session:
        session.get(Match, later_match_id).kickoff_utc = "2024-09-01T13:00:00Z"
    engine.dispose()
    cell = build(injury_database, as_of=cutoff)["home"]["missing_last_starters_n"]
    assert cell["value"] == expected
    if expected is not None:
        assert cell["previous_match_id"] == "jc:homeprev"


@pytest.mark.parametrize(
    "cutoff,eligible",
    [
        ("2024-09-01T15:30:00Z", False),
        ("2024-09-01T16:00:00Z", False),
        ("2024-09-01T16:00:00.000001Z", True),
    ],
)
def test_previous_lineup_replay_uses_latest_group_clock(injury_database, cutoff, eligible):
    lineup(injury_database, captured="2026-11-04T12:00:00Z")
    engine = get_engine(injury_database)
    with session_scope(engine) as session:
        session.get(Match, "fd:homeprev").kickoff_utc = "2024-09-01T13:00:00Z"
    with session_scope(engine) as session:
        builder = InjuryFeatures(session)
    engine.dispose()
    starters, _, reason = builder._starters(
        builder.matches["jc:homeprev"], 1, instant(cutoff), "backfill_replay"
    )
    assert (starters is not None) is eligible
    if eligible:
        assert len(starters) == 11 and reason is None


def aligned_target(database):
    """Use the exact row shape produced for T38 experiment feature calls."""
    engine = get_engine(database)
    with session_scope(engine) as session:
        session.get(Match, "fd:target").status = "finished"
        session.add(
            Match(
                match_id="jc:target",
                source="jc",
                league="英超",
                home_team_id=1,
                away_team_id=2,
                kickoff_utc="2024-09-10T13:00:00Z",
                status="finished",
                counterpart_id="fd:target",
            )
        )
    with session_scope(engine) as session:
        _, games = unified_history(session, provider="fd")
        row = next(game for game in games if game["match_id"] == "jc:target")
        builder = InjuryFeatures(session)
    engine.dispose()
    assert row["kickoff_utc"] == instant("2024-09-10T13:00:00Z")
    assert row["earliest_kickoff_utc"] == instant(KICKOFF)
    return builder, row


@pytest.mark.parametrize("captured", ["2024-09-10T12:30:00Z", "2026-11-04T12:00:00Z"])
def test_experiment_row_replay_uses_earliest_kickoff_while_strict_rejects_late_capture(
    injury_database, captured
):
    setup_complete(injury_database, daily=False, captured=captured)
    builder, row = aligned_target(injury_database)
    replay = builder.build(row, as_of=row["earliest_kickoff_utc"], mode="backfill_replay")
    assert replay["home"]["missing_n"]["value"] == 2
    assert replay["home"]["missing_last_starters_n"]["value"] == 1
    assert replay["feature_cutoff"] == instant(KICKOFF).isoformat()
    assert replay["home"]["missing_n"]["provenance"]["effective_available_utc"] == (
        "2024-09-10T11:59:59.999999+00:00"
    )
    strict = builder.build(row, as_of=row["kickoff_utc"], mode="strict")
    assert strict["home"]["missing_n"]["value"] is None
    assert strict["home"]["missing_n"]["missing_reason"] == "backfilled_not_known"
    assert strict["feature_cutoff"] == instant(KICKOFF).isoformat()
    early = builder.build(row, as_of="2024-09-10T11:59:59Z", mode="backfill_replay")
    assert early["home"]["missing_n"]["value"] is None
    assert early["feature_cutoff"] == "2024-09-10T11:59:59+00:00"


def test_aligned_replay_does_not_relax_actual_captured_time(injury_database):
    data = document()
    for injury in data["response"]:
        injury["fixture"]["date"] = "2024-09-10T13:00:00Z"
    ingest(injury_database, data=data, captured="2024-09-10T12:30:00Z", daily=True)
    builder, row = aligned_target(injury_database)
    for mode in ("strict", "backfill_replay"):
        features = builder.build(row, as_of=row["kickoff_utc"], mode=mode)
        assert features["feature_cutoff"] == instant(KICKOFF).isoformat()
        assert features["home"]["missing_n"]["value"] is None
        assert features["home"]["missing_n"]["missing_reason"] == "not_known_before_cutoff"


def test_late_captured_lineup_correction_cannot_enter_replay(injury_database):
    setup_complete(injury_database)
    data = json.loads((FIX / "injuries_lineups.json").read_bytes())
    data["response"][0]["startXI"][0]["player"]["id"] = 13
    lineup(injury_database, data=data, captured="2024-09-11T12:00:00Z", daily=True)
    for mode in ("strict", "backfill_replay"):
        assert build(injury_database, mode=mode)["home"]["missing_last_starters_n"]["value"] == 1


def test_target_uses_exact_or_explicit_consistent_counterpart_identity(injury_database):
    setup_complete(injury_database)
    engine = get_engine(injury_database)
    with session_scope(engine) as session:
        for match_id, counterpart in (
            ("jc:target", "fd:target"),
            ("fdorg:target", None),
            ("apif:unlinked", None),
        ):
            session.add(
                Match(
                    match_id=match_id,
                    source=match_id.split(":")[0],
                    league="英超",
                    home_team_id=1,
                    away_team_id=2,
                    kickoff_utc="2024-09-10T12:00:01Z" if match_id == "apif:unlinked" else KICKOFF,
                    status="scheduled",
                    counterpart_id=counterpart,
                )
            )
    engine.dispose()
    assert build(injury_database, match_id="jc:target")["home"]["missing_n"]["value"] == 2
    assert build(injury_database, match_id="fdorg:target")["home"]["missing_n"]["value"] == 2
    assert build(injury_database, match_id="apif:unlinked")["home"]["missing_n"]["value"] is None


def test_two_different_pages_with_same_capture_time_are_ambiguous(injury_database):
    setup_complete(injury_database)
    data = document()
    data["response"] = data["response"][1:]
    data["results"] = 3
    ingest(injury_database, data=data)
    assert build(injury_database)["home"]["missing_n"]["missing_reason"] == "ambiguous_capture_time"


def test_unchanged_new_day_uses_capture_clock_without_leaking_later_interval(injury_database):
    setup_complete(injury_database)
    before = build(injury_database, as_of="2024-09-09T10:30:00Z")
    ingest(injury_database, captured="2024-09-10T10:00:00Z")
    after = build(injury_database, as_of="2024-09-09T10:30:00Z")
    assert before == after
    latest = build(injury_database)["home"]["missing_n"]
    assert latest["value"] == 2
    assert instant(latest["captured_utc"]) == instant("2024-09-10T10:00:00Z")
    assert instant(latest["evidence"][0]["captured_utc"]) == instant(CAPTURE)


def test_invalid_latest_page_cannot_reuse_older_complete_capture(injury_database):
    setup_complete(injury_database)
    data = document()
    data["results"] += 1
    assert ingest(injury_database, data=data, captured="2024-09-09T11:00:00Z")["invalid"]
    assert build(injury_database)["home"]["missing_n"]["missing_reason"] == "incomplete_capture"
    assert build(injury_database, as_of="2024-09-09T10:30:00Z")["home"]["missing_n"]["value"] == 2


def test_missing_latest_gzip_does_not_reuse_old_capture(injury_database):
    from zucai.injuries_storage import read_audit

    setup_complete(injury_database)
    ingest(injury_database, captured="2024-09-09T11:00:00Z")
    page = read_audit(injury_database.parent / "state/audit" / AUDIT_NAME)[-1]
    (injury_database.parent / json.loads(page["detail"])["receipt_path"]).unlink()
    assert build(injury_database)["home"]["missing_n"]["missing_reason"] == "incomplete_capture"
    assert build(injury_database, as_of="2024-09-09T10:30:00Z")["home"]["missing_n"]["value"] == 2


def test_out_of_order_old_capture_keeps_latest_content_and_earlier_cutoff(injury_database):
    data = document()
    data["response"][0]["player"]["type"] = "Questionable"
    ingest(injury_database, data=data, captured="2024-09-09T11:00:00Z")
    ingest(injury_database)
    assert build(injury_database)["home"]["missing_n"]["value"] == 1
    assert build(injury_database, as_of="2024-09-09T10:30:00Z")["home"]["missing_n"]["value"] == 2
    ingest(injury_database, captured="2024-09-09T12:00:00Z")
    assert build(injury_database)["home"]["missing_n"]["value"] == 2
    assert build(injury_database, as_of="2024-09-09T11:30:00Z")["home"]["missing_n"]["value"] == 1
