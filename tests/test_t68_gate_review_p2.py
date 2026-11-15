"""PR143 review attacks through the real archive/build-register entry."""

import json
import shutil
from datetime import datetime, timedelta

import pytest

from test_t68_daily_gate_entry import (  # noqa: F401
    bundle as gate_bundle,
)
from test_t68_daily_gate_entry import (
    change,
    document,
    fixture_rows,
    ref,
    refresh_bundle_exports,
    reseal_fixture,
    run,
)
from test_t68_identity_archive_entry import archive as frozen_archive  # noqa: F401
from test_t68_identity_archive_entry import raw, table

bundle = gate_bundle


def test_repeat_must_be_a_distinct_invocation(bundle):
    _, root, evidence = bundle
    current = evidence["current_season_replay"]
    repeat = evidence["persistence"]
    repeat.update(
        repeat_report=current["report"],
        replay_options=current["replay_options"],
        before_manifest=current["before_manifest"],
        after_manifest=current["after_manifest"],
    )
    assert run(bundle)["status"] != "ready_for_review"


def test_immutable_inventory_cannot_name_mutable_matches(bundle):
    _, root, evidence = bundle
    for snapshot_key, export_key in [
        ("before_repeat", "before_manifest"),
        ("after_repeat", "after_manifest"),
        ("restored", "restored_manifest"),
    ]:
        state = evidence["persistence"]
        m = json.loads((root / state[export_key]["path"]).read_bytes())
        change(
            root,
            state[snapshot_key],
            lambda d, m=m: d.update(
                immutable_sha256={"state/matches.jsonl": m["files"]["state/matches.jsonl"]}
            ),
        )
    assert run(bundle)["status"] != "ready_for_review"


def test_held_cannot_point_at_other_source_audit(bundle):
    archive, root, evidence = bundle
    other = archive / "state/audit/fd_held_unbound.csv"
    shutil.copyfile(archive / "state/audit/apif_held_unbound.csv", other)
    reseal_fixture(archive)
    refresh_bundle_exports(bundle)
    evidence["current_season_replay"]["held_before"] = ref(root, other)
    result = run(bundle)
    assert "current_season_replay" in result["missing_evidence"]


def test_all_suspected_cannot_become_ready_even_with_review(bundle):
    _, root, evidence = bundle
    change(
        root,
        evidence["recent_replay"]["results"],
        lambda d: d.update(
            outcomes=[dict(fixture_id="apif:10", outcome="suspected")], risk_ids=["apif:10"]
        ),
    )
    evidence["risk_review"] = document(
        root,
        "risk-review.json",
        [
            dict(
                fixture_id="apif:10",
                reason="x",
                disposition="quarantined",
                review_evidence=document(root, "tiny.json", {}),
            )
        ],
    )
    assert run(bundle)["status"] != "ready_for_review"


def test_newer_season_original_cannot_be_ignored(bundle):
    archive, root, evidence = bundle
    path = archive / "state/catalogue/backfill/backfill/api-football/day/fixtures_one.json"
    doc = json.loads(path.read_bytes())
    doc["parameters"]["season"] = "2028"
    doc["response"][0]["league"]["season"] = 2028
    doc["response"][0]["fixture"]["id"] = 30
    raw(
        archive,
        "state/catalogue/backfill/backfill/api-football/day/fixtures_new.json",
        doc,
        "https://v3.football.api-sports.io/fixtures?league=98&season=2028",
    )
    reseal_fixture(archive)
    refresh_bundle_exports(bundle)
    assert "newer_season_originals_require_review" in run(bundle)["blockers"]


def test_contradictory_second_owner_in_recent_exports_blocks(bundle):
    _, root, evidence = bundle
    # Only recent exports are changed: the current/repeat importer still has clean proof.
    for stage in ["before_manifest", "after_manifest"]:
        source = (root / evidence["recent_replay"][stage]["path"]).parent
        folder = root / ("recent-" + stage)
        shutil.copytree(source, folder)
        matches = fixture_rows(folder, "matches")
        results = fixture_rows(folder, "results")
        original = matches[0]
        direct = dict(original, match_id="apif:10", source="apif", counterpart_id=None)
        original.update(home_team_id=2, away_team_id=1)
        table(folder, "matches", [*matches, direct])
        table(folder, "results", [*results, dict(results[0], match_id="apif:10")])
        reseal_fixture(folder)
        evidence["recent_replay"][stage] = ref(root, folder / "feed_manifest.json")
    assert run(bundle)["status"] != "ready_for_review"


def test_unlinked_same_fixture_beyond_three_hours_is_pending(bundle):
    archive, root, evidence = bundle
    # A current JC row was duplicated under another source, 3h45 later, without linkage.
    for folder in [archive, root / "preimport", root / "after", root / "restored"]:
        matches = fixture_rows(folder, "matches")
        results = fixture_rows(folder, "results")
        extra = dict(
            matches[0],
            match_id="fd:duplicate",
            source="fd",
            counterpart_id=None,
            kickoff_utc=(
                datetime.fromisoformat(matches[0]["kickoff_utc"]) + timedelta(hours=3, minutes=45)
            ).isoformat(),
        )
        table(folder, "matches", [*matches, extra])
        table(folder, "results", [*results, dict(results[0], match_id="fd:duplicate")])
        reseal_fixture(folder)
    refresh_bundle_exports(bundle)
    result = run(bundle)
    assert "unreviewed_cross_source_near_duplicate" in result["blockers"]


def test_catalogue_never_stored_is_information_not_gate_denominator(bundle):
    archive, root, evidence = bundle
    path = archive / "state/catalogue/catalogue/jc/2026-10/2026-10-31-p1.json"
    doc = json.loads(path.read_bytes())
    doc["value"]["matchResult"].append(dict(doc["value"]["matchResult"][0], matchId=2042499))
    doc["value"]["total"] = 2
    raw(
        archive,
        path.relative_to(archive),
        doc,
        json.loads(path.with_suffix(".json.meta.json").read_bytes())["url"],
    )
    reseal_fixture(archive)
    refresh_bundle_exports(bundle)
    result = run(bundle)
    assert result["matches"]["catalogue_12_month_total"] == 2
    assert result["matches"]["total"] == 1
    assert result["matches"]["catalogue_never_stored_ids"] == ["jc:2042499"]


@pytest.mark.parametrize("problem", ["one_letter", "tiny_proof"])
@pytest.mark.parametrize("bundle", [True], indirect=True)
def test_unbound_explanation_requires_scoped_original_and_reason(bundle, problem):
    archive, root, evidence = bundle
    evidence["unbound_explanations"] = document(
        root,
        "unbound-review.json",
        [
            dict(
                official_team_id="900",
                reason_code="no_unique_approved_scoped_binding",
                reason="x"
                if problem == "one_letter"
                else "Historic official club remains unbound.",
                evidence=ref(
                    root, archive / "state/catalogue/catalogue/jc/2026-06/2026-06-30-p1.json"
                )
                if problem == "one_letter"
                else document(root, "tiny-proof.json", {}),
            )
        ],
    )
    result = run(bundle)
    assert "unbound_explanations" in result["missing_evidence"]


def test_recent_importer_rejects_self_declared_risk(bundle):
    _, root, evidence = bundle
    change(
        root,
        evidence["recent_replay"]["results"],
        lambda d: d.update(
            outcomes=[dict(fixture_id="apif:10", outcome="rejected")], risk_ids=["apif:10"]
        ),
    )
    result = run(bundle)
    assert "recent_replay" in result["missing_evidence"]
    assert "actual importer" in result["evidence"]["recent_replay"]["reason"]
