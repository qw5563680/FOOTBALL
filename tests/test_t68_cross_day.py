"""T68 read-time isolation; saved real rows plus explicitly synthetic counterexamples."""

import copy
import json
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import select

from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Result, ResultSourceIdentity, ResultSourceLink, Team
from zucai.predict.backtest import read_history
from zucai.predict.pooled import select_training_history
from zucai.result_source_identity import record_result_identity
from zucai.result_source_links import record_result_link

FIXTURE = Path(__file__).parents[1] / "data/fixtures/t68/cross_day/real12.json"
SEED = json.loads(FIXTURE.read_text())
API = "apif:695164"
FD = "fd:USA:20210424:los-angeles-fc:seattle-sounders"
CUTOFF = pd.Timestamp("2026-11-14T12:00:00Z")
KNOWN = pd.Timestamp("2026-11-14T09:00:00Z")


@pytest.fixture
def db(tmp_path):
    engine = get_engine(tmp_path / "real12.db")
    init_db(engine)
    with session_scope(engine) as session:
        for model in (Team, Match, Result, ResultSourceIdentity):
            for row in copy.deepcopy(SEED["tables"][model.__tablename__]):
                if model is Team:
                    row["aliases"] = json.loads(row["aliases"])
                session.add(model(**row))
            session.flush()
    yield engine
    engine.dispose()


def history(session, league="美职", sources="fd,jc,apif"):
    return read_history(session, league, sources)


def select_ids(session, **kwargs):
    return set(select_training_history(history(session, **kwargs), CUTOFF).match_id)


def clone(session, source_id, new_id, *, source=None, kickoff=None, result=True):
    model = session.get(Match, source_id)
    values = {c.name: getattr(model, c.name) for c in Match.__table__.columns}
    values.update(match_id=new_id, source=source or new_id.split(":")[0], counterpart_id=None)
    if kickoff is not None:
        values["kickoff_utc"] = kickoff
    added = Match(**values)
    session.add(added)
    session.flush()
    if result:
        old = session.get(Result, source_id)
        values = {c.name: getattr(old, c.name) for c in Result.__table__.columns}
        values.update(match_id=new_id)
        session.add(Result(**values))
        session.flush()
    return added


def refresh_identity(session, key):
    session.flush()
    record_result_identity(session, session.get(Match, key), KNOWN.to_pydatetime())


def test_real_twelve_pairs_hold_only_added_rows_without_mutation(db):
    with session_scope(db) as s:
        before = [dict(row) for row in s.execute(select(Match.__table__)).mappings()]
        selected = select_training_history(history(s), CUTOFF)
        assert len(selected) == 12
        assert set(selected.source) == {"fd"}
        held = selected.attrs["unverified_fd_identity_overlaps"]
        assert {row["match_id"] for row in held} == {
            f"apif:{key}"
            for key in (
                695164,
                695206,
                695213,
                816780,
                816884,
                980819,
                981244,
                981246,
                1150767,
                1151233,
                1151235,
                1151237,
            )
        }
        assert {row["reason"] for row in held} == {"unverified_same_identity_overlap"}
        assert [dict(row) for row in s.execute(select(Match.__table__)).mappings()] == before
        assert not list(s.scalars(select(ResultSourceLink)))


@pytest.mark.parametrize("hours,held", [(3, True), (24, True), (36, True), (36 + 1 / 3600, False)])
def test_window_is_inclusive_without_using_score(db, hours, held):
    with session_scope(db) as s:
        old = s.get(Match, FD)
        old.kickoff_utc = (
            pd.Timestamp(s.get(Match, API).kickoff_utc) - pd.Timedelta(hours=hours)
        ).isoformat()
        s.get(Result, FD).home_goals = 9
        s.get(Result, FD).available_utc = KNOWN.isoformat()
        s.flush()
        assert (API not in select_ids(s)) is held


@pytest.mark.parametrize("field", ["result", "identity", "old_result"])
def test_both_sides_and_identity_must_be_known_at_same_cutoff(db, field):
    with session_scope(db) as s:
        if field == "identity":
            for proof in s.scalars(
                select(ResultSourceIdentity).where(ResultSourceIdentity.match_id == API)
            ):
                proof.verified_utc = CUTOFF.isoformat()
        else:
            s.get(Result, API if field == "result" else FD).available_utc = CUTOFF.isoformat()
        s.flush()
        selected = select_training_history(history(s), CUTOFF)
        assert not any(
            r["match_id"] == API for r in selected.attrs["unverified_fd_identity_overlaps"]
        )
        assert (API in set(selected.match_id)) is (field == "old_result")


def test_multiple_old_candidates_are_all_reported(db):
    with session_scope(db) as s:
        clone(s, FD, "fd:second")
        selected = select_training_history(history(s), CUTOFF)
        assert API not in set(selected.match_id)
        assert {
            r["fd_candidate_id"]
            for r in selected.attrs["unverified_fd_identity_overlaps"]
            if r["match_id"] == API
        } == {FD, "fd:second"}


def test_unrelated_third_link_does_not_exempt_crossday_pair(db):
    with session_scope(db) as s:
        third = clone(s, API, "jc:third")
        record_result_link(s, s.get(Match, API), third, KNOWN.to_pydatetime())
        # Do not select the third source; the complete component still must be inspected.
        assert API not in select_ids(s, sources="fd,apif")


def test_old_fd_collapsed_to_jc_still_holds_added_api(db):
    with session_scope(db) as s:
        third = clone(s, FD, "jc:old")
        third.counterpart_id = FD
        s.flush()
        ids = select_ids(s)
        assert "jc:old" in ids and FD not in ids and API not in ids


def test_future_relationship_cannot_exempt_earlier_cutoff(db):
    with session_scope(db) as s:
        s.get(Match, FD).kickoff_utc = s.get(Match, API).kickoff_utc
        s.get(Result, FD).available_utc = "2021-04-26T01:00:00Z"
        s.flush()
        record_result_link(s, s.get(Match, API), s.get(Match, FD), CUTOFF.to_pydatetime())
        selected = select_training_history(history(s), CUTOFF)
        assert API not in set(selected.match_id)
        assert any(r["match_id"] == API for r in selected.attrs["unverified_fd_identity_overlaps"])
        later = select_training_history(history(s), CUTOFF + pd.Timedelta(seconds=1))
        assert not any(r["match_id"] == API for r in later.attrs["unverified_fd_identity_overlaps"])


def test_two_added_sources_without_old_source_are_both_held(db):
    with session_scope(db) as s:
        paid = s.get(Match, API)
        paid.league = "英超"
        second = clone(s, API, "fdorg:second", kickoff="2021-04-24T22:00:00Z")
        for key in (paid.home_team_id, paid.away_team_id):
            team = s.get(Team, key)
            team.aliases = [*team.aliases, f"apif:英超:team:{key}", f"fdorg:英超:team:{key}"]
        refresh_identity(s, API)
        refresh_identity(s, second.match_id)
        selected = select_training_history(history(s, "英超", "apif,fdorg"), CUTOFF)
        assert selected.empty
        assert {r["match_id"] for r in selected.attrs["unverified_fd_identity_overlaps"]} == {
            API,
            "fdorg:second",
        }


@pytest.mark.parametrize("change", ["reverse", "league", "source"])
def test_wrong_orientation_league_or_same_source_not_same_id_risk(db, change):
    with session_scope(db) as s:
        row = s.get(Match, FD)
        if change == "reverse":
            row.home_team_id, row.away_team_id = row.away_team_id, row.home_team_id
        elif change == "league":
            row.league = "英超"
        else:
            clone(s, FD, "apif:same-source")
            s.delete(s.get(Result, FD))
            s.flush()
            s.delete(row)
        s.flush()
        assert API in select_ids(s)


def decision_module():
    import importlib

    return importlib.import_module("zucai.cross_day_decisions")


def approval(db, tmp_path, monkeypatch, *, action="different_match", pair=(API, FD)):
    from datetime import UTC, datetime

    module = decision_module()
    evidence = tmp_path / "review-original.txt"
    evidence.write_text("Synthetic reviewer evidence for test only.\n")
    import hashlib

    with session_scope(db) as s:
        snapshot = module.decision_snapshot(s, list(pair))
    approved = datetime.now(UTC).isoformat()
    document = {
        "schema_version": 1,
        "decisions": [
            {
                "decision_id": "test-only-1",
                "decision": action,
                "match_ids": sorted(pair),
                "snapshot": snapshot,
                "approved_utc": approved,
                "approved_by": "qw5563680",
                "approval_ref": "https://github.com/qw5563680/FOOTBALL/pull/999999999",
                "evidence": [
                    {
                        "path": str(evidence),
                        "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
                        "observed_utc": approved,
                    }
                ],
            }
        ],
    }
    registry = tmp_path / "decisions.json"
    registry.write_text(json.dumps(document))
    monkeypatch.setattr(module, "REGISTRY_PATH", registry)
    return module, registry, document


def now_cutoff():
    from datetime import UTC, datetime

    return pd.Timestamp(datetime.now(UTC)) + pd.Timedelta(seconds=1)


def current_ids(db):
    with session_scope(db) as s:
        return set(select_training_history(history(s), now_cutoff()).match_id)


def test_approval_is_inert_until_real_application_and_does_not_rewrite_past(
    db, tmp_path, monkeypatch
):
    module, registry, _ = approval(db, tmp_path, monkeypatch)
    assert API not in current_ids(db)
    before = now_cutoff() - pd.Timedelta(seconds=1)
    receipt = module.apply_decisions(db.url.database, registry)
    assert pd.Timestamp(receipt["receipts"][0]["applied_utc"]) >= before
    assert API in current_ids(db)
    with session_scope(db) as s:
        assert API not in set(select_training_history(history(s), CUTOFF).match_id)
        assert not list(s.scalars(select(ResultSourceLink)))
    raw = (Path(db.url.database).parent / module.RECEIPT_FILE).read_bytes()
    assert module.apply_decisions(db.url.database, registry) == receipt
    assert (Path(db.url.database).parent / module.RECEIPT_FILE).read_bytes() == raw


def test_same_match_receipt_selects_original_priority_without_global_link(
    db, tmp_path, monkeypatch
):
    module, registry, _ = approval(db, tmp_path, monkeypatch, action="same_match")
    module.apply_decisions(db.url.database, registry)
    with session_scope(db) as s:
        selected = select_training_history(history(s), now_cutoff())
        assert FD in set(selected.match_id) and API not in set(selected.match_id)
        assert not any(
            r["match_id"] == API for r in selected.attrs["unverified_fd_identity_overlaps"]
        )
        assert selected.attrs["cross_day_decisions_applied"][0]["decision"] == "same_match"
        assert not list(s.scalars(select(ResultSourceLink)))


def test_different_match_does_not_release_another_unresolved_pair(db, tmp_path, monkeypatch):
    with session_scope(db) as s:
        clone(s, FD, "fd:other-risk")
    module, registry, _ = approval(db, tmp_path, monkeypatch)
    module.apply_decisions(db.url.database, registry)
    assert API not in current_ids(db)


@pytest.mark.parametrize("drift", ["score", "pointer", "alias", "registry"])
def test_changed_snapshot_or_registry_revokes_receipt(db, tmp_path, monkeypatch, drift):
    module, registry, document = approval(db, tmp_path, monkeypatch)
    module.apply_decisions(db.url.database, registry)
    if drift == "registry":
        document["decisions"][0]["approval_ref"] = (
            "https://github.com/qw5563680/FOOTBALL/pull/999999998"
        )
        registry.write_text(json.dumps(document))
    else:
        with session_scope(db) as s:
            if drift == "score":
                s.get(Result, FD).home_goals = 8
                s.get(Result, FD).available_utc = KNOWN.isoformat()
            elif drift == "pointer":
                s.get(Match, FD).counterpart_id = "jc:unrelated"
            else:
                team = s.get(Team, s.get(Match, FD).home_team_id)
                team.aliases = [*team.aliases, "synthetic extra alias"]
    assert API not in current_ids(db)


@pytest.mark.parametrize("bad", ["future", "duplicate", "raw_tamper", "snapshot", "three_hours"])
def test_invalid_approval_cannot_be_applied(db, tmp_path, monkeypatch, bad):
    module, registry, document = approval(db, tmp_path, monkeypatch)
    entry = document["decisions"][0]
    if bad == "future":
        entry["approved_utc"] = "2099-01-01T00:00:00Z"
    elif bad == "duplicate":
        document["decisions"].append(copy.deepcopy(entry))
        document["decisions"][1]["decision"] = "same_match"
    elif bad == "raw_tamper":
        Path(entry["evidence"][0]["path"]).write_text("changed")
    elif bad == "snapshot":
        entry["snapshot"] = {}
    else:
        with session_scope(db) as s:
            s.get(Match, FD).kickoff_utc = (
                pd.Timestamp(s.get(Match, API).kickoff_utc) - pd.Timedelta(hours=3)
            ).isoformat()
            s.get(Result, FD).available_utc = KNOWN.isoformat()
            s.flush()
            entry["snapshot"] = module.decision_snapshot(s, [API, FD])
    registry.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        module.apply_decisions(db.url.database, registry)
    assert not (Path(db.url.database).parent / module.RECEIPT_FILE).exists()


def test_receipt_tampering_is_rejected(db, tmp_path, monkeypatch):
    module, registry, _ = approval(db, tmp_path, monkeypatch)
    module.apply_decisions(db.url.database, registry)
    path = Path(db.url.database).parent / module.RECEIPT_FILE
    document = json.loads(path.read_text())
    document["receipts"][0]["applied_utc"] = "2020-01-01T00:00:00Z"
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        current_ids(db)


def test_optional_receipt_roundtrip_and_old_archive_clears_residue(db, tmp_path, monkeypatch):
    from datetime import date

    from zucai.cloud_feeds import export_feeds, restore_feeds

    module, registry, _ = approval(db, tmp_path, monkeypatch)
    old = tmp_path / "old-archive"
    export_feeds(Path(db.url.database), old, date(2026, 11, 14), "old")
    module.apply_decisions(db.url.database, registry)
    archive = tmp_path / "new-archive"
    meta = export_feeds(Path(db.url.database), archive, date(2026, 11, 14), "new")
    assert module.RECEIPT_FILE in meta["files"]
    dest = tmp_path / "restored" / "zucai.db"
    restore_feeds(archive, dest)
    restored = get_engine(dest)
    try:
        assert current_ids(restored) == current_ids(db)
        restore_feeds(old, dest)
        assert not (dest.parent / module.RECEIPT_FILE).exists()
        assert API not in current_ids(restored)
    finally:
        restored.dispose()


def test_real_twelve_crossday_risks_remain_per_match_in_audit(db, tmp_path):
    import csv

    from zucai.source_results_audit import audit_source_results

    target = tmp_path / "audit.csv"
    report = audit_source_results(db.url.database, target)
    rows = list(csv.DictReader(target.open()))
    assert report["rows"] == 12
    assert {r["apif_match_id"] for r in rows} == {p["paid_id"] for p in SEED["pairs"]}
    assert all("unverified_same_identity_overlap" in r["risk_reasons"] for r in rows)


def test_only_pure_temporary_identity_rows_are_aggregated(tmp_path):
    import csv

    from test_t63_source_audit import database
    from zucai.source_results_audit import audit_source_results

    path = database(tmp_path, "temporary_identity", "unknown_team_prefix")
    out = tmp_path / "audit.csv"
    report = audit_source_results(path, out)
    rows = list(csv.DictReader(out.open()))
    assert len(rows) == 1
    assert rows[0]["classification"] == "temporary_identity_summary"
    assert report["temporary_identity_rows"] == 2
    assert report["temporary_identity_summaries"][0]["apif_matches"] == 2
    assert report["comparison_rows"] == 2


@pytest.mark.parametrize(
    "risk", ["invalid_time", "reverse", "score", "time", "missing", "multiple"]
)
def test_temporary_name_does_not_hide_independent_audit_risk(tmp_path, risk):
    import csv
    import sqlite3

    from test_t63_source_audit import database
    from zucai.source_results_audit import audit_source_results

    path = database(tmp_path, "consistent")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE teams SET name='apif:team:temporary' WHERE id=1")
        if risk == "invalid_time":
            conn.execute("UPDATE matches SET kickoff_utc='invalid' WHERE source='apif'")
            conn.execute(
                "UPDATE matches SET counterpart_id="
                "(SELECT match_id FROM matches WHERE source='fd') "
                "WHERE source='apif'"
            )
        elif risk == "reverse":
            conn.execute("UPDATE matches SET home_team_id=2,away_team_id=1 WHERE source='fd'")
        elif risk == "score":
            conn.execute("UPDATE results SET home_goals=8 WHERE match_id LIKE 'fd:%'")
        elif risk == "time":
            conn.execute("UPDATE matches SET kickoff_utc='2026-09-01T16:00:00Z' WHERE source='fd'")
        elif risk == "missing":
            conn.execute("UPDATE matches SET counterpart_id='fd:missing' WHERE source='apif'")
        else:
            conn.execute(
                "INSERT INTO matches SELECT 'fd:duplicate', source,league,home_team_id,"
                "away_team_id,kickoff_utc,NULL FROM matches WHERE source='fd'"
            )
            conn.execute(
                "INSERT INTO results SELECT 'fd:duplicate',home_goals,away_goals,available_utc,"
                "available_source FROM results WHERE match_id LIKE 'fd:%'"
            )
    out = tmp_path / "audit.csv"
    report = audit_source_results(path, out)
    rows = list(csv.DictReader(out.open()))
    assert report["temporary_identity_rows"] == 0
    assert all(r["classification"] != "temporary_identity_summary" for r in rows)
    assert all(r["risk_reasons"] != "[]" for r in rows)


def test_temporary_identity_cannot_hide_other_side_missing_provider_proof(tmp_path):
    import csv
    import sqlite3

    from test_t63_source_audit import database
    from zucai.source_results_audit import audit_source_results

    path = database(tmp_path, "temporary_identity")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE teams SET aliases='[]' WHERE id=2")
    out = tmp_path / "audit.csv"
    report = audit_source_results(path, out)
    assert report["temporary_identity_rows"] == 0
    rows = list(csv.DictReader(out.open()))
    assert len(rows) == 1
    assert "provider_binding_invalid" in rows[0]["risk_reasons"]


def test_bound_transition_remains_held_even_if_score_disagrees(db):
    with session_scope(db) as s:
        old = s.get(Match, FD)
        away = old.away_team_id
        s.add(Team(id=999999, name="Synthetic unresolved old team", aliases=[]))
        s.flush()
        old.away_team_id = 999999
        s.flush()
        assert API in select_ids(s)  # 24h one-sided unknown is not the legacy 3h proof.
        old.away_team_id = away  # Simulates an independently approved identity binding.
        s.get(Result, FD).home_goals = 8
        s.get(Result, FD).available_utc = KNOWN.isoformat()
        s.flush()
        assert API not in select_ids(s)


def test_complete_component_through_unselected_source_exempts_exact_pair(db):
    with session_scope(db) as s:
        old = s.get(Match, FD)
        old.kickoff_utc = s.get(Match, API).kickoff_utc
        s.get(Result, FD).available_utc = KNOWN.isoformat()
        third = clone(s, API, "fdorg:anchor", result=False)
        record_result_link(s, s.get(Match, API), third, KNOWN.to_pydatetime())
        record_result_link(s, third, old, KNOWN.to_pydatetime())
        selected = select_training_history(history(s, sources="fd,apif"), CUTOFF)
        assert FD in set(selected.match_id) and API not in set(selected.match_id)
        assert not any(
            r["match_id"] == API for r in selected.attrs["unverified_fd_identity_overlaps"]
        )


def test_same_match_two_paid_score_conflict_does_not_choose_a_score(db, tmp_path, monkeypatch):
    with session_scope(db) as s:
        paid = s.get(Match, API)
        paid.league = "英超"
        second = clone(s, API, "fdorg:second", kickoff="2021-04-24T22:00:00Z")
        for key in (paid.home_team_id, paid.away_team_id):
            team = s.get(Team, key)
            team.aliases = [*team.aliases, f"apif:英超:team:{key}", f"fdorg:英超:team:{key}"]
        refresh_identity(s, API)
        refresh_identity(s, second.match_id)
        s.get(Result, second.match_id).home_goals = 9
        s.get(Result, second.match_id).available_utc = KNOWN.isoformat()
        s.get(Result, second.match_id).available_source = "captured"
    module, registry, _ = approval(
        db, tmp_path, monkeypatch, action="same_match", pair=(API, "fdorg:second")
    )
    module.apply_decisions(db.url.database, registry)
    with session_scope(db) as s:
        selected = select_training_history(history(s, "英超", "apif,fdorg"), now_cutoff())
        assert selected.empty
        assert {r["reason"] for r in selected.attrs["unverified_fd_identity_overlaps"]} == {
            "cross_day_same_match_score_conflict"
        }


def test_complete_component_includes_unselected_old_pointer_anchor(db):
    with session_scope(db) as s:
        old = s.get(Match, FD)
        old.kickoff_utc = s.get(Match, API).kickoff_utc
        s.get(Result, FD).available_utc = KNOWN.isoformat()
        anchor = clone(s, API, "jc:anchor", result=False)
        anchor.counterpart_id = FD
        s.flush()
        record_result_link(s, s.get(Match, API), anchor, KNOWN.to_pydatetime())
        selected = select_training_history(history(s, sources="fd,apif"), CUTOFF)
        assert FD in set(selected.match_id) and API not in set(selected.match_id)
        assert not any(
            r["match_id"] == API for r in selected.attrs["unverified_fd_identity_overlaps"]
        )


@pytest.mark.parametrize("league", ["韩职", "澳超", "中超", "丹超"])
def test_other_leagues_do_not_acquire_new_t63_policy(db, league):
    with session_scope(db) as s:
        paid = s.get(Match, API)
        paid.league = league
        old = clone(s, API, "jc:outside-policy", kickoff="2021-04-24T22:00:00Z")
        s.flush()
        frame = history(s, league, "apif,jc")
        selected = select_training_history(frame, CUTOFF)
        assert set(selected.match_id) == {API, old.match_id}
        assert not selected.attrs.get("pending_fd_identity_overlaps")


@pytest.mark.parametrize(
    "field,value",
    [
        ("approval_ref", "approved"),
        ("approval_ref", "https://github.com/other/FOOTBALL/pull/1"),
        ("approval_ref", "https://github.com/qw5563680/FOOTBALL/issues/1"),
        ("approval_ref", "https://github.com/qw5563680/FOOTBALL/pull/0"),
        ("approval_ref", "https://github.com/qw5563680/FOOTBALL/pull/1?approved=true"),
        ("approved_by", "test reviewer"),
        ("approved_utc", "offset"),
    ],
)
def test_approval_requires_owner_and_repository_pr_provenance(
    db, tmp_path, monkeypatch, field, value
):
    module, registry, document = approval(db, tmp_path, monkeypatch)
    if field == "approved_utc":
        from datetime import datetime, timedelta, timezone

        value = (
            datetime.fromisoformat(document["decisions"][0][field])
            .astimezone(timezone(timedelta(hours=1)))
            .isoformat()
        )
    document["decisions"][0][field] = value
    registry.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        module.apply_decisions(db.url.database, registry)
    assert not (Path(db.url.database).parent / module.RECEIPT_FILE).exists()


@pytest.mark.parametrize(
    "bad",
    ["reversed_claim", "outside_window_claim", "cross_league_claim", "broken_outgoing"],
)
def test_old_bridge_rejects_all_conflicting_component_claims(db, bad):
    with session_scope(db) as s:
        old = s.get(Match, FD)
        old.kickoff_utc = s.get(Match, API).kickoff_utc
        s.get(Result, FD).available_utc = KNOWN.isoformat()
        anchor = clone(s, API, "jc:review-anchor", result=False)
        anchor.counterpart_id = FD
        if bad == "broken_outgoing":
            old.counterpart_id = "jc:review-missing"
        else:
            rival = clone(s, API, "jc:review-rival", result=False)
            rival.counterpart_id = FD
            if bad == "reversed_claim":
                rival.home_team_id, rival.away_team_id = rival.away_team_id, rival.home_team_id
            elif bad == "outside_window_claim":
                rival.kickoff_utc = (
                    pd.Timestamp(anchor.kickoff_utc) + pd.Timedelta(days=10)
                ).isoformat()
            else:
                rival.league = "日职"
        s.flush()
        record_result_link(s, s.get(Match, API), anchor, KNOWN.to_pydatetime())
        with pytest.raises(ValueError, match="组件|声明"):
            history(s, sources="fd,apif")


def test_two_paid_results_cannot_use_partially_valid_old_bridge(db):
    with session_scope(db) as s:
        paid = s.get(Match, API)
        paid.league = "英超"
        second = clone(s, API, "fdorg:review-paid")
        for key in (paid.home_team_id, paid.away_team_id):
            team = s.get(Team, key)
            team.aliases = [*team.aliases, f"apif:英超:team:{key}", f"fdorg:英超:team:{key}"]
        refresh_identity(s, API)
        refresh_identity(s, second.match_id)
        jc = clone(s, API, "jc:review-anchor", result=False)
        fd = clone(s, API, "fd:review-anchor", result=False)
        rival = clone(s, API, "jc:review-rival", result=False)
        jc.counterpart_id = fd.match_id
        rival.counterpart_id = fd.match_id
        rival.home_team_id, rival.away_team_id = rival.away_team_id, rival.home_team_id
        s.flush()
        record_result_link(s, paid, jc, KNOWN.to_pydatetime())
        record_result_link(s, second, fd, KNOWN.to_pydatetime())
        with pytest.raises(ValueError, match="组件|声明"):
            history(s, "英超", "apif,fdorg")


@pytest.mark.parametrize("case", ["consistent", "no_apif_result"])
def test_audit_exports_independent_pointer_risk_even_without_score_conflict(tmp_path, case):
    import csv
    import sqlite3

    from test_t63_source_audit import database
    from zucai.source_results_audit import audit_source_results

    path = database(tmp_path, case)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE matches SET counterpart_id='jc:review-missing' WHERE source='fd'")
    out = tmp_path / "audit.csv"
    report = audit_source_results(path, out)
    rows = list(csv.DictReader(out.open()))
    assert any("counterpart_missing" in row["risk_reasons"] for row in rows)
    assert report["unverified"] >= 1
    assert report["reasons"]["counterpart_missing"] >= 1
    assert all(row["classification"] == "unverified" for row in rows)


@pytest.mark.parametrize(
    "reference",
    ["fd:E1:20260901:swansea:watford", "apif:1494789", "fdorg:523456"],
)
def test_jc_absorbed_source_reference_is_not_a_broken_match_pointer(tmp_path, reference):
    import csv
    import sqlite3

    from test_t63_source_audit import database
    from zucai.source_results_audit import audit_source_results

    path = database(tmp_path, "consistent")
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM results")
        conn.execute("DELETE FROM matches")
        conn.execute(
            "INSERT INTO matches VALUES ('jc:reference','jc','英超',1,2,'2026-09-01T14:00:00Z',?)",
            (reference,),
        )
        conn.execute(
            "INSERT INTO results VALUES ('jc:reference',2,1,'2026-09-01T18:00:00Z','captured')"
        )
    out = tmp_path / "audit.csv"
    report = audit_source_results(path, out)
    assert not list(csv.DictReader(out.open()))
    assert report["unverified"] == 0
    assert report["unmatched"] == 1
    assert report["non_entity_source_references"] == {reference.split(":")[0]: 1}


def test_unrelated_invalid_old_component_does_not_block_valid_selected_bridge(db):
    with session_scope(db) as s:
        old = s.get(Match, FD)
        old.kickoff_utc = s.get(Match, API).kickoff_utc
        s.get(Result, FD).available_utc = KNOWN.isoformat()
        anchor = clone(s, API, "jc:valid-anchor", result=False)
        anchor.counterpart_id = FD
        remote_jc = clone(s, API, "jc:unrelated", result=False)
        remote_fd = clone(s, API, "fd:unrelated", result=False)
        remote_jc.counterpart_id = remote_fd.match_id
        remote_fd.counterpart_id = "jc:unrelated-missing"
        s.flush()
        record_result_link(s, s.get(Match, API), anchor, KNOWN.to_pydatetime())
        selected = select_training_history(history(s, sources="fd,apif"), CUTOFF)
        assert FD in set(selected.match_id) and API not in set(selected.match_id)
        assert not any(
            row["match_id"] == API for row in selected.attrs["unverified_fd_identity_overlaps"]
        )


def test_shared_external_source_reference_still_keeps_competing_claims(tmp_path):
    import csv
    import sqlite3

    from test_t63_source_audit import database
    from zucai.source_results_audit import audit_source_results

    path = database(tmp_path, "consistent")
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM results")
        conn.execute("DELETE FROM matches")
        for key in ("jc:first", "jc:second"):
            conn.execute(
                "INSERT INTO matches VALUES (?,'jc','英超',1,2,"
                "'2026-09-01T14:00:00Z','apif:999999')",
                (key,),
            )
            conn.execute(
                "INSERT INTO results VALUES (?,2,1,'2026-09-01T18:00:00Z','captured')", (key,)
            )
    out = tmp_path / "audit.csv"
    report = audit_source_results(path, out)
    rows = list(csv.DictReader(out.open()))
    assert len(rows) == report["unverified"] == 2
    assert all("multiple_counterpart_claims" in row["risk_reasons"] for row in rows)
