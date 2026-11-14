"""Offline acceptance for the immutable T61 APIF original; no transport or database."""

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from zucai.cloud_catalogue import _previous_pointers, parse_apif_catalogue
from zucai.cloud_catalogue_backfill import catalogue_scopes

SOURCE_COMMIT = "e5848e5d7de83a0cd024fa3e03e08cf6db6c7ee4"
SOURCE_SHA = "173c44cd57bc04a0df30b56105dbe9ac077cdbb123e4e1145b7f620342f5d27a"
CONFLICTS = {"397": 2018, "276": 2018, "379": 2020, "402": 2019, "542": 2021}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def payload(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def snapshot(folder):
    return {p.name: sha(p.read_bytes()) for p in sorted(folder.iterdir()) if p.is_file()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    source, output = args.source_dir.resolve(), args.out_dir.resolve()
    assert not output.exists(), "Output must be new"
    assert not output.is_relative_to(source), "Output must be outside immutable source"
    before = snapshot(source)
    raw = (source / "apif-raw.json").read_bytes()
    meta_raw = (source / "apif-meta.json").read_bytes()
    metadata = json.loads(meta_raw)
    assert sha(raw) == SOURCE_SHA == metadata["sha256"]
    assert metadata["status_code"] == 200
    assert metadata["url"] == "https://v3.football.api-sports.io/leagues"
    captured = datetime.fromisoformat(metadata["fetched_at_utc"].replace("Z", "+00:00"))
    assert captured.utcoffset().total_seconds() == 0
    original = json.loads(raw)
    assert len(raw) == 3090059
    assert len(original["response"]) == 1251
    assert sum(len(row["seasons"]) for row in original["response"]) == 8816
    run_id = "t65-full-apif-offline"
    relative = Path("state/catalogue") / run_id
    raw_path = (relative / "apif-raw.json").as_posix()
    attempts = []

    def no_network(*args, **kwargs):
        attempts.append("blocked network attempt")
        raise AssertionError("Offline evidence cannot open a network transport")

    with (
        patch("requests.sessions.Session.request", side_effect=no_network),
        patch("socket.create_connection", side_effect=no_network),
    ):
        parsed = parse_apif_catalogue(raw, raw_path=raw_path)
        assert parsed["raw_season_count"] == 8816
        assert parsed["conflicted_season_count"] == 5
        assert len(parsed["leagues"]) == 1251
        assert sum(len(row["seasons"]) for row in parsed["leagues"]) == 8811
        assert len(parsed["warnings"]) == 6
        raw_by_id = {str(row["league"]["id"]): row for row in original["response"]}
        normal_count = 0
        for row in parsed["leagues"]:
            source_seasons = raw_by_id[row["id"]]["seasons"]
            expected_years = {
                s["year"] for s in source_seasons if s["year"] != CONFLICTS.get(row["id"])
            }
            normal = [s for s in row["seasons"] if s.get("status") != "conflict"]
            assert {s["year"] for s in normal} == expected_years
            assert all(s["access"] == "unverified" for s in normal)
            normal_count += len(normal)
        assert normal_count == 8806
        conflict_warnings = [w for w in parsed["warnings"] if w["code"] == "duplicate_season_year"]
        assert {(w["league_id"], w["season_year"]) for w in conflict_warnings} == set(CONFLICTS.items())
        for warning in conflict_warnings:
            originals = [s for s in raw_by_id[warning["league_id"]]["seasons"] if s["year"] == warning["season_year"]]
            assert warning["original_records"] == originals
            assert warning["raw_path"] == raw_path and warning["raw_sha256"] == SOURCE_SHA
            assert len(warning["record_pointers"]) == len(originals) == 2
            for pointer, record in zip(warning["record_pointers"], originals, strict=True):
                value = original
                for key in pointer.split("/")[1:]:
                    value = value[int(key)] if isinstance(value, list) else value[key]
                assert value == record
        provenance = dict(
            raw_sha256=SOURCE_SHA,
            raw_path=raw_path,
            meta_path=(relative / "apif-meta.json").as_posix(),
            fetched_at_utc=metadata["fetched_at_utc"],
            url=metadata["url"],
            http_status=200,
            source_commit=SOURCE_COMMIT,
            source_path="state/catalogue/43539439430-1/apif-raw.json",
        )
        parsed["provenance"] = provenance
        target = output / relative
        target.mkdir(parents=True, exist_ok=False)
        (target / "apif-raw.json").write_bytes(raw)
        (target / "apif-meta.json").write_bytes(meta_raw)
        normalized = payload(parsed)
        (target / "apif.json").write_bytes(normalized)
        pointer = dict(path=(relative / "apif.json").as_posix(), sha256=sha(normalized), raw_sha256=SOURCE_SHA, run_id=run_id)
        (target.parent / "latest.json").write_bytes(payload(dict(version=1, providers={"apif": pointer})))
        # Exercise the production pointer validator, including reparsing the raw evidence.
        pointers, errors = _previous_pointers(output)
        assert errors == [] and pointers["apif"] == pointer
        approved_plan = catalogue_scopes(output, "apif")
        assert not {(code, scope["season"]) for code, scope in approved_plan["scopes"]} & set(CONFLICTS.items())
        # These five leagues are not approved routes. Inject authorization only into
        # this offline planner call to prove exclusion cannot depend on that fact.
        planner_registry = {
            "download_policy": {"scope": "related"},
            "providers": {"apif": {
                code: dict(status="verified", mode="import", backfill=True, relevant=True,
                           config=dict(api_name=raw_by_id[code]["league"]["name"]))
                for code in CONFLICTS
            }},
        }
        conflict_plan = catalogue_scopes(output, "apif", registry=planner_registry)
        assert len(conflict_plan["scopes"]) == 52
        assert {(e["code"], e["season"]) for e in conflict_plan["excluded"]} == set(CONFLICTS.items())
        assert conflict_plan["missing"] == [] and conflict_plan["access_is_verified"] is False
        for code, year in CONFLICTS.items():
            try:
                catalogue_scopes(output, "apif", code, year, year, registry=planner_registry)
            except ValueError as exc:
                assert "赛季" in str(exc)
            else:
                raise AssertionError(f"Conflict {code}/{year} was explicitly planned")
        assert not attempts
    assert snapshot(source) == before
    (output / "approved-backfill-plan.json").write_bytes(payload(approved_plan))
    (output / "conflict-backfill-plan.json").write_bytes(payload(dict(
        authorization="Offline planner-only fixture injection; no real registry approval or download",
        plan=conflict_plan,
    )))
    report = dict(
        version=1,
        collection="catalogue-offline-replay",
        run_id=run_id,
        status="complete",
        requests=0,
        network_attempts=len(attempts),
        database_imports=0,
        captured_utc=metadata["fetched_at_utc"],
        replayed_utc=datetime.now(UTC).isoformat(),
        source_commit=SOURCE_COMMIT,
        source_dir=str(source),
        source_unchanged=True,
        source_file_sha256=before,
        source_raw_sha256=SOURCE_SHA,
        warnings=[dict(w, provider="apif") for w in parsed["warnings"]],
        errors=[],
        providers={"apif": dict(status="complete", league_count=1251, raw_season_count=8816,
                               season_count=8811, conflicted_season_count=5,
                               non_conflicting_season_count=8806, **provenance)},
        latest=pointer,
        production_pointer_validation="passed",
        approved_scope_count=len(approved_plan["scopes"]),
        conflict_leagues_normal_scope_count=52,
        conflict_years_excluded=conflict_plan["excluded"],
        limitations=[
            "Local replay only; the production latest.json is unchanged.",
            "Reviewer Actions catalogue probe is still required after merge.",
            "No catalogue season metadata grants account entitlement.",
            "No backfill, daily collection or production write occurred.",
        ],
    )
    (target / "summary.json").write_bytes(payload(report))
    (output / "replay-summary.json").write_bytes(payload(report))
    print(json.dumps({"status": report["status"], "leagues": 1251, "original_seasons": 8816,
                      "normalized_years": 8811, "non_conflicting_years": 8806,
                      "conflicts": 5, "warnings": len(report["warnings"]), "requests": 0,
                      "approved_scopes": report["approved_scope_count"], "output": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
