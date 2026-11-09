"""Protect additive history against overwrites, ambiguous identities and time leakage."""

import csv
import hashlib
import json
import shutil
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parents[1] / "data/fixtures/international_results"
REGISTRY = Path(__file__).parents[1] / "data/api_football/nations.json"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(path):
    return {str(p.relative_to(path)): p.read_bytes() for p in path.rglob("*") if p.is_file()}


@pytest.fixture
def inputs(tmp_path):
    shutil.copytree(FIXTURE, tmp_path / "input")
    return dict(
        plan_path=tmp_path / "input/plan.json",
        source_path=tmp_path / "input/results.csv",
        archive_path=tmp_path / "input/archive",
        registry_path=REGISTRY,
        output_path=tmp_path / "supplement",
    )


def run(inputs):
    from zucai.history_supplement import build_supplement

    return build_supplement(**inputs)


def test_add_three_keep_sixteen_and_preserve_extra_time_distinction(inputs):
    original = snapshot(inputs["archive_path"])
    result = run(inputs)
    assert result == {"added": 3, "retained": 16, "source_score_differences": 1}
    assert snapshot(inputs["archive_path"]) == original
    from zucai.history_supplement import read_supplement

    rows = read_supplement(inputs["output_path"])
    assert [(r["match_id"], r["home_goals_90"], r["away_goals_90"]) for r in rows] == [
        ("uefa:2036206", 0, 0),
        ("uefa:2036209", 2, 1),
        ("uefa:2036211", 2, 1),
    ]
    for r in rows:
        assert r["neutral"] is True
        assert r["kickoff_utc"].endswith("T19:00:00Z")
        assert r["historical_available_utc"] is None
        assert r["purpose"] == "historical-reference-only"
        assert r["source"]["fetched_at_utc"].startswith("2026-11-09")
    audit = json.loads((inputs["output_path"] / "audit.json").read_text())
    old = next(r for r in audit if r["project_match_id"] == "apif:1316546")
    assert old["decision"] == "retain_existing"
    assert old["source_score"] == [3, 3]
    assert old["verified_score_90"] == [2, 2]


def test_replay_and_export_restore_do_not_rewrite_files(inputs, tmp_path):
    run(inputs)
    before = snapshot(inputs["output_path"])
    assert run(inputs)["added"] == 0
    assert snapshot(inputs["output_path"]) == before
    zipped = shutil.make_archive(str(tmp_path / "saved"), "zip", inputs["output_path"])
    shutil.unpack_archive(zipped, tmp_path / "restored")
    from zucai.history_supplement import read_supplement

    assert read_supplement(tmp_path / "restored") == read_supplement(inputs["output_path"])


@pytest.mark.parametrize(
    "change",
    [
        "raw_hash",
        "archive_hash",
        "registry_hash",
        "unknown_team",
        "wrong_team",
        "missing_clock",
        "wrong_clock",
        "naive_time",
        "wrong_score",
        "wrong_period",
        "duplicate_identity",
        "reversed_teams",
        "pretend_new",
        "wrong_existing_id",
        "missing_month",
        "unversioned_source",
        "future_capture",
        "duplicate_source",
    ],
)
def test_invalid_input_stops_entire_batch_without_partial_output(inputs, change):
    plan = json.loads(inputs["plan_path"].read_text())
    new = next(m for m in plan["matches"] if m["reference_id"] == "uefa:2036209")
    old = next(m for m in plan["matches"] if m["project_match_id"])
    if change == "raw_hash":
        inputs["source_path"].write_text(inputs["source_path"].read_text() + "\n")
    elif change == "archive_hash":
        next(inputs["archive_path"].rglob("*.csv")).write_text("corrupt")
    elif change == "registry_hash":
        plan["registry_sha256"] = "0" * 64
    elif change == "unknown_team":
        new["home_apif_team_id"] = "9999"
    elif change == "wrong_team":
        new["home_apif_team_id"] = "10"
    elif change == "missing_clock":
        del new["kickoff_evidence"]
    elif change == "wrong_clock":
        new["kickoff_evidence"]["timezone"] = "UTC"
    elif change == "naive_time":
        new["kickoff_utc"] = "2024-07-09T19:00:00"
    elif change == "wrong_score":
        new["home_goals_90"] = 3
    elif change == "wrong_period":
        new["score_period"] = "including_extra_time"
    elif change == "duplicate_identity":
        new["reference_id"] = old["reference_id"]
    elif change == "reversed_teams":
        new["home_apif_team_id"], new["away_apif_team_id"] = "2", "9"
    elif change == "pretend_new":
        old["project_match_id"] = None
    elif change == "wrong_existing_id":
        old["project_match_id"] = "apif:9999999"
    elif change == "missing_month":
        del plan["baseline"]["files"]["matches/2024-07.csv"]
    elif change == "unversioned_source":
        plan["source"]["source_url"] = "https://example.com/results.csv"
    elif change == "future_capture":
        plan["source"]["fetched_at_utc"] = "2099-01-01T00:00:00Z"
    elif change == "duplicate_source":
        with inputs["source_path"].open("a", newline="") as f:
            csv.DictWriter(f, fieldnames=list(new["source_row"])).writerow(new["source_row"])
        plan["source"]["sha256"] = digest(inputs["source_path"])
    inputs["plan_path"].write_text(json.dumps(plan))
    before = snapshot(inputs["archive_path"])
    with pytest.raises(ValueError):
        run(inputs)
    assert snapshot(inputs["archive_path"]) == before
    assert not inputs["output_path"].exists()


def test_changed_existing_result_is_not_overwritten_even_with_new_archive_hash(inputs):
    plan = json.loads(inputs["plan_path"].read_text())
    path = inputs["archive_path"] / "matches/2025-03.csv"
    with path.open() as f:
        reader = csv.DictReader(f)
        fields, rows = reader.fieldnames, list(reader)
    next(r for r in rows if r["match_id"] == "apif:1316546")["home_goals"] = "3"
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    plan["baseline"]["files"]["matches/2025-03.csv"] = digest(path)
    inputs["plan_path"].write_text(json.dumps(plan))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        run(inputs)
    assert path.read_bytes() == before
    assert not inputs["output_path"].exists()


def test_output_cannot_alias_formal_archive(inputs):
    inputs["output_path"] = inputs["archive_path"] / "history"
    with pytest.raises(ValueError):
        run(inputs)


def test_tampered_export_and_changed_replay_are_rejected(inputs):
    run(inputs)
    path = inputs["output_path"] / "supplement.jsonl"
    path.write_text(path.read_text().replace('"home_goals_90":0', '"home_goals_90":9'))
    from zucai.history_supplement import read_supplement

    with pytest.raises(ValueError):
        read_supplement(inputs["output_path"])
    before = snapshot(inputs["output_path"])
    with pytest.raises(ValueError):
        run(inputs)
    assert snapshot(inputs["output_path"]) == before


@pytest.mark.parametrize("swap_proof_too", [False, True])
def test_reference_ids_cannot_be_swapped_between_distinct_fixtures(inputs, swap_proof_too):
    plan = json.loads(inputs["plan_path"].read_text())
    one, two = plan["matches"][:2]
    one["reference_id"], two["reference_id"] = two["reference_id"], one["reference_id"]
    if swap_proof_too:
        for field in ("score_source_url", "venue_source_url"):
            one[field], two[field] = two[field], one[field]
    inputs["plan_path"].write_text(json.dumps(plan))
    with pytest.raises(ValueError):
        run(inputs)
    assert not inputs["output_path"].exists()


@pytest.mark.parametrize(
    "change", ["invalid_audit", "wrong_decision", "short_plan", "plan_purpose", "wrong_counts"]
)
def test_restored_bundle_revalidates_plan_and_audit_even_with_recomputed_hashes(inputs, change):
    run(inputs)
    root = inputs["output_path"]
    manifest = json.loads((root / "manifest.json").read_text())
    if change == "invalid_audit":
        (root / "audit.json").write_text("not JSON")
    elif change == "wrong_decision":
        audit = json.loads((root / "audit.json").read_text())
        audit[0]["decision"] = "replace_existing"
        (root / "audit.json").write_text(json.dumps(audit))
    elif change in ("short_plan", "plan_purpose"):
        plan = json.loads((root / "plan.json").read_text())
        if change == "short_plan":
            plan["matches"] = [m for m in plan["matches"] if m["project_match_id"] is None]
        else:
            plan["purpose"] = "production-training"
        (root / "plan.json").write_text(json.dumps(plan))
    else:
        manifest["counts"]["retained"] = 0
    manifest["files"] = {name: digest(root / name) for name in manifest["files"]}
    (root / "manifest.json").write_text(json.dumps(manifest))
    from zucai.history_supplement import read_supplement

    with pytest.raises(ValueError):
        read_supplement(root)
