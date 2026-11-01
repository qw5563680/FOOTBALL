"""Verify T13 export independently against frozen T04 and pinned feed tables.

Run from the repository root:
    PYTHONPATH=src work/venv/bin/python work/t13-acceptance/verify_acceptance.py
"""

import argparse
import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
INPUT = ROOT / "work/t13-input"
DATABASE = INPUT / "restored/zucai.db"
AS_OF = "2026-11-01T16:25:56.611980Z"
AS_OF_DT = datetime.fromisoformat(AS_OF)
CLI_COMMAND = (
    "PYTHONPATH=src work/venv/bin/python -m zucai.cli features build "
    "--db work/t13-input/restored/zucai.db --as-of " + AS_OF
    + " --out work/t13-acceptance/features.parquet"
)


def stamp(value):
    return datetime.fromisoformat(value)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path):
    return json.loads(path.read_bytes())


def counts(frame, mask):
    part = frame.loc[mask]
    count = len(part)

    def fraction(value):
        return {"n": value, "N": count, "ratio": value / count if count else None}

    result = {
        "rows": count,
        "home_rest_nonnull": fraction(int(part.home_rest_days.notna().sum())),
        "away_rest_nonnull": fraction(int(part.away_rest_days.notna().sum())),
        "both_rest_nonnull": fraction(
            int(part[["home_rest_days", "away_rest_days"]].notna().all(axis=1).sum())
        ),
        "had_nonnull": fraction(int(part.market_p_home.notna().sum())),
    }
    if "market_hhad_line" in part.columns:
        result["hhad_line_nonnull"] = fraction(int(part.market_hhad_line.notna().sum()))
    if "market_line" in part.columns:
        result["old_constant_market_line_nonnull"] = fraction(int(part.market_line.notna().sum()))
    return result


def main():
    global ROOT, HERE, INPUT, DATABASE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--artifact-dir", type=Path, default=HERE)
    parser.add_argument("--input-dir", type=Path)
    args = parser.parse_args()
    ROOT = args.repo_root.resolve()
    HERE = args.artifact_dir.resolve()
    INPUT = args.input_dir.resolve() if args.input_dir else ROOT / "work/t13-input"
    DATABASE = INPUT / "restored/zucai.db"
    provenance = read_json(INPUT / "input-provenance.json")
    assert sha256(DATABASE) == provenance["database_sha256_before_and_after"]
    input_sha = sha256(DATABASE)
    for key, expected in provenance["archive_files_sha256"].items():
        assert sha256(INPUT / "archive" / key) == expected, key
    assert sha256(INPUT / "archive/feed_manifest.json") == provenance["archive_manifest_sha256"]

    old = pd.read_parquet(ROOT / "reports/2026-11-02-t04-acceptance/features.parquet")
    v1 = pd.read_parquet(INPUT / "restored-v1.parquet")
    v1_attrs = read_json(INPUT / "restored-v1.attrs.json")
    v2 = pd.read_parquet(HERE / "features.parquet")
    metadata = read_json(HERE / "features.metadata.json")
    manifest = read_json(HERE / "features.manifest.json")
    # Full audit dictionaries live in Parquet attrs too. Pandas copies attrs for
    # derived Series; retain the separately read evidence instead of repeatedly
    # deep copying a megabyte per scalar check.
    for frame in (old, v1, v2):
        frame.attrs.clear()
    for key, expected in manifest["files"].items():
        assert sha256(HERE / key) == expected, key
    assert metadata["feature_version"] == "t04-v2"
    assert len(old) == len(v1) == len(v2) == metadata["rows"] == 866
    assert metadata["model_feature_columns"] == v1_attrs["model_feature_columns"]
    assert metadata["as_of_utc"] == AS_OF
    assert "market_line" not in v2.columns
    assert str(v2.market_hhad_line.dtype) == "Int64"
    assert not any(
        name.startswith(("market_", "meta_")) for name in metadata["model_feature_columns"]
    )

    model_columns = metadata["model_feature_columns"]
    stable_model = [name for name in model_columns if name not in ("home_rest_days", "away_rest_days")]
    stable_had = ["market_p_home", "market_p_draw", "market_p_away"]
    assert len(model_columns) == 43 and len(stable_model) == 41
    index_frames = [frame.set_index("meta_match_id").sort_index() for frame in (old, v1, v2)]
    old_indexed, v1_indexed, v2_indexed = index_frames
    pd.testing.assert_frame_equal(old_indexed[model_columns], v1_indexed[model_columns])
    pd.testing.assert_frame_equal(v1_indexed[stable_model], v2_indexed[stable_model])
    pd.testing.assert_frame_equal(v1_indexed[stable_had], v2_indexed[stable_had])
    assert list(v1_indexed.index) == list(v2_indexed.index)

    with sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True) as database:
        database.row_factory = sqlite3.Row
        matches = {row["match_id"]: dict(row) for row in database.execute("SELECT * FROM matches")}
        results = {row["match_id"]: dict(row) for row in database.execute("SELECT * FROM results")}
        quote_rows = [dict(row) for row in database.execute("SELECT * FROM odds")]
        assert database.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert database.execute("PRAGMA foreign_key_check").fetchone() is None
    quotes = defaultdict(lambda: defaultdict(dict))
    for quote in quote_rows:
        if quote["pool"] not in {"had", "hhad"} or quote["selection"] not in {"h", "d", "a"}:
            continue
        line = quote["line"]
        if not math.isfinite(line) or not float(line).is_integer():
            continue
        if quote["pool"] == "had" and line != 0:
            continue
        key = (stamp(quote["snapshot_utc"]), int(line))
        quotes[quote["match_id"], quote["pool"]][key][quote["selection"]] = quote["price"]

    checked = {
        "targets": 0,
        "performance_windows": 0,
        "performance_evidence_rows": 0,
        "had_quotes": 0,
        "hhad_quotes": 0,
        "rest_present": 0,
        "rest_absent": 0,
        "schedule_rest_without_pre_cutoff_result": 0,
    }
    performance_windows = (
        "home_last5", "home_last10", "home_home5", "away_last5", "away_last10", "away_away5", "h2h_last3"
    )
    for target_id, row in v2_indexed.iterrows():
        checked["targets"] += 1
        target = matches[target_id]
        kickoff = stamp(target["kickoff_utc"])
        cutoff = min(kickoff, AS_OF_DT)
        evidence = metadata["audit"][target_id]
        assert stamp(evidence["cutoff_utc"]) == cutoff
        for window in performance_windows:
            assert evidence["windows"][window] == v1_attrs["audit"][target_id]["windows"][window]
            checked["performance_windows"] += 1
            for game in evidence["windows"][window]:
                checked["performance_evidence_rows"] += 1
                assert game["match_id"] != target_id
                assert stamp(game["kickoff_utc"]) < cutoff
                assert stamp(game["available_utc"]) < cutoff
                assert game["source"] == target["source"]
                assert game["league"] == target["league"]
        for pool, audit_key, probability_cols, timestamp_col in (
            ("had", "market", stable_had, "meta_market_snapshot_utc"),
            (
                "hhad", "market_hhad",
                ["market_hhad_p_home", "market_hhad_p_draw", "market_hhad_p_away"],
                "meta_market_hhad_snapshot_utc",
            ),
        ):
            complete = [
                (key, prices)
                for key, prices in quotes[target_id, pool].items()
                if key[0] < cutoff and set(prices) == {"h", "d", "a"}
                and all(math.isfinite(price) and price > 1 for price in prices.values())
            ]
            actual = evidence[audit_key]
            if not complete:
                assert row[probability_cols].isna().all()
                assert actual["snapshot_utc"] is None and actual["prices"] is None
                assert pd.isna(row[timestamp_col])
                if pool == "hhad":
                    assert pd.isna(row.market_hhad_line) and actual["line"] is None
                continue
            (when, line), prices = max(complete, key=lambda item: item[0])
            assert stamp(actual["snapshot_utc"]) == when < cutoff
            assert stamp(row[timestamp_col]) == when
            assert actual["prices"] == prices
            assert actual["pool"] == pool and actual["model_input"] is False
            inverse = [1 / prices[selection] for selection in "hda"]
            expected = [value / sum(inverse) for value in inverse]
            for column, value in zip(probability_cols, expected, strict=True):
                assert row[column] == value, (target_id, column)
            assert math.isclose(sum(row[probability_cols]), 1, abs_tol=1e-15)
            if pool == "hhad":
                assert type(actual["line"]) is int
                assert actual["line"] == row.market_hhad_line == line
            checked[pool + "_quotes"] += 1
        for side in ("home", "away"):
            team_id = target[side + "_team_id"]
            candidates = [
                game for game in matches.values()
                if game["match_id"] != target_id and game["source"] == target["source"]
                and game["status"] != "cancelled"
                and team_id in (game["home_team_id"], game["away_team_id"])
                and stamp(game["kickoff_utc"]) < kickoff
            ]
            games = evidence["windows"][side + "_rest"]
            if not candidates:
                assert not games and pd.isna(row["meta_" + side + "_rest_match_id"])
                assert pd.isna(row[side + "_rest_days"])
                checked["rest_absent"] += 1
                continue
            latest = max(candidates, key=lambda game: (stamp(game["kickoff_utc"]), game["match_id"]))
            assert len(games) == 1
            game = games[0]
            assert game["match_id"] == row["meta_" + side + "_rest_match_id"] == latest["match_id"]
            assert stamp(game["kickoff_utc"]) == stamp(latest["kickoff_utc"]) < kickoff
            assert game["source"] == latest["source"] == target["source"]
            assert game["status"] == latest["status"] != "cancelled"
            assert game["league"] == latest["league"]
            assert game["venue"] == ("home" if team_id == latest["home_team_id"] else "away")
            assert "available_utc" not in game and "available_source" not in game
            expected_rest = (kickoff - stamp(latest["kickoff_utc"])).total_seconds() / 86400
            assert row[side + "_rest_days"] == expected_rest > 0
            result = results.get(latest["match_id"])
            if not result or not result["available_utc"] or stamp(result["available_utc"]) >= cutoff:
                checked["schedule_rest_without_pre_cutoff_result"] += 1
            checked["rest_present"] += 1

    assert sha256(DATABASE) == input_sha
    coverage = {}
    for name, frame in (("frozen_t04", old), ("restored_v1", v1), ("restored_v2", v2)):
        kickoff = pd.to_datetime(frame.meta_kickoff_utc)
        masks = {
            "all": pd.Series(True, index=frame.index),
            "historical": kickoff <= pd.Timestamp(AS_OF),
            "future": kickoff > pd.Timestamp(AS_OF),
        }
        coverage[name] = {group: counts(frame, mask) for group, mask in masks.items()}
    changed_rest = {}
    for side in ("home", "away"):
        column = side + "_rest_days"
        before, after = v1_indexed[column], v2_indexed[column]
        equal = before.eq(after) | (before.isna() & after.isna())
        changed_rest[side] = {
            "changed": int((~equal).sum()),
            "newly_nonnull": int((before.isna() & after.notna()).sum()),
            "changed_existing": int((~equal & before.notna()).sum()),
        }
    files = {
        "features.parquet": sha256(HERE / "features.parquet"),
        "features.metadata.json": sha256(HERE / "features.metadata.json"),
        "features.manifest.json": sha256(HERE / "features.manifest.json"),
        "verify_acceptance.py": sha256(Path(__file__).resolve()),
        "../t13-input/archive/feed_manifest.json": sha256(INPUT / "archive/feed_manifest.json"),
        "../t13-input/build_v1.py": sha256(INPUT / "build_v1.py"),
        "../t13-input/restored-v1.parquet": sha256(INPUT / "restored-v1.parquet"),
        "../t13-input/restored-v1.attrs.json": sha256(INPUT / "restored-v1.attrs.json"),
    }
    summary = {
        "as_of_utc": AS_OF,
        "cli_command": CLI_COMMAND,
        "input_provenance": provenance,
        "input_database_unchanged_sha256": input_sha,
        "feature_version": metadata["feature_version"],
        "generated_utc": metadata["generated_utc"],
        "schema": {"rows": len(v2), "columns": len(v2.columns), "model_features": 43, "market_columns": len(metadata["market_columns"])},
        "validation": {
            "all_checks_passed": True,
            "model_whitelist_unchanged": True,
            "other_41_numeric_model_columns_equal_rebuilt_v1": True,
            "had_probabilities_equal_rebuilt_v1": True,
            "performance_evidence_equal_rebuilt_v1": True,
            "frozen_t04_43_model_columns_equal_rebuilt_v1": True,
            "hhad_integer_dtype": str(v2.market_hhad_line.dtype),
            "counts": checked,
        },
        "coverage": coverage,
        "rest_changes_from_rebuilt_v1": changed_rest,
        "sha256": files,
        "archive_files_sha256": provenance["archive_files_sha256"],
    }
    output = HERE / "acceptance-summary.json"
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"summary": str(output), "validation": summary["validation"], "coverage": coverage, "rest_changes": changed_rest}, indent=2))


if __name__ == "__main__":
    main()
