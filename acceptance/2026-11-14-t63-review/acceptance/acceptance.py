#!/usr/bin/env python3
"""Codex internal, offline T63 acceptance; not Claude approval or performance evidence.

Uses one mutable copy of the completed T66 replay DB.  The reconcile timestamp
and training cutoff are synthetic regression clocks, never historical claims.
"""

import argparse
import hashlib
import importlib.metadata
import json
import shutil
import sqlite3
import sys
import time
import traceback
from contextlib import ExitStack, closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path("/workspace/work/t63-review-20261114/acceptance")
LIVE_CODE = Path("/workspace/work/football-t63")
T66 = Path("/workspace/work/t66-review-20261114/real-acceptance/fixed")
BASE_DB = T66 / "work/data/zucai.db"
BASE_SHA = "e668f1ee847aff20361cd90f10c181d76a75d5e123a03655dc21e89f1a9a3934"
ARCHIVE = Path("/workspace/work/t65-fixed-archive")
CODE = ROOT / "code-training"
DB = ROOT / "work/data/zucai.db"
CUTOFF = datetime.fromisoformat("2026-11-14T06:30:00+00:00")
OBSERVED = datetime.fromisoformat("2026-11-14T06:29:00+00:00")
OLD7 = ("英超", "西甲", "意甲", "德甲", "法甲", "葡超", "巴甲")
FROZEN = (
    "forecasts",
    "models",
    "scoreboard",
    "sfc",
    "daily",
    "experiments",
    "feed_manifest.json",
    "forecast_manifest.json",
)
SWE = "apif:1494327"
SWE_FD = "fd:SWE:20261103:sirius:halmstad"


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def read(path):
    return json.loads(Path(path).read_text())


def code_hashes(code):
    return {
        str(p.relative_to(code)): sha(p)
        for prefix in ("src", "data")
        for p in sorted((code / prefix).rglob("*"))
        if p.is_file() and p.suffix in (".py", ".json")
    }


def frozen_hashes():
    return {
        str(p.relative_to(ARCHIVE)): sha(p)
        for prefix in FROZEN
        for p in (
            [ARCHIVE / prefix]
            if (ARCHIVE / prefix).is_file()
            else sorted((ARCHIVE / prefix).rglob("*"))
        )
        if p.is_file()
    }


def connect(database):
    return sqlite3.connect(f"file:{database}?mode=ro", uri=True)


def snapshot(database):
    result = {}
    with closing(connect(database)) as db:
        for (table,) in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ):
            columns = [r[1] for r in db.execute(f'PRAGMA table_info("{table}")')]
            cols = ",".join(f'"{c}"' for c in columns)
            digest, count = hashlib.sha256(), 0
            for row in db.execute(f'SELECT {cols} FROM "{table}" ORDER BY {cols}'):
                digest.update(
                    (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
                )
                count += 1
            result[table] = dict(columns=columns, rows=count, sha256=digest.hexdigest())
    return result


def imported_modules():
    result = {}
    for name, module in sorted(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if name.startswith("zucai") and path and Path(path).suffix == ".py":
            path = Path(path).resolve()
            result[name] = dict(path=str(path), sha256=sha(path))
    return result


def protect():
    assert sha(BASE_DB) == BASE_SHA, "Original T66 DB modified"
    expected = read(ROOT / "inputs.json")
    frozen = frozen_hashes()
    assert frozen == read(ROOT / "frozen-before.json"), "Frozen e584 files modified"
    return dict(
        original_t66_db_sha256=BASE_SHA,
        frozen_files_unchanged=len(frozen),
        old_results_unchanged=expected["original_archive_results"],
    )


def prepare():
    assert sha(BASE_DB) == BASE_SHA
    assert not DB.exists() and not CODE.exists()
    ROOT.mkdir(parents=True, exist_ok=True)
    before_code = code_hashes(LIVE_CODE)
    for folder in ("src", "data"):
        shutil.copytree(
            LIVE_CODE / folder, CODE / folder, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )
    assert before_code == code_hashes(CODE) == code_hashes(LIVE_CODE)
    shutil.copytree(BASE_DB.parent, DB.parent)
    assert sha(DB) == BASE_SHA
    frozen = frozen_hashes()
    assert len(frozen) == 2615, len(frozen)
    write(ROOT / "frozen-before.json", frozen)
    before = snapshot(DB)
    write(ROOT / "tables-before.json", before)
    with closing(connect(DB)) as db:
        originals = list(
            map(json.loads, (ARCHIVE / "state/results.jsonl").read_text().splitlines())
        )
        columns = read(ARCHIVE / "feed_manifest.json")["columns"]["results"]
        current = {r[0]: list(r) for r in db.execute(f"SELECT {','.join(columns)} FROM results")}
        assert all(current[row[0]] == row for row in originals)
    write(
        ROOT / "inputs.json",
        dict(
            purpose=(
                "Codex internal offline acceptance; not Claude approval or performance experiment"
            ),
            base_database=str(BASE_DB),
            original_t66_db_sha256=BASE_SHA,
            original_archive="e5848e5d7de83a0cd024fa3e03e08cf6db6c7ee4",
            original_archive_results=len(originals),
            original_frozen_files=len(frozen),
            synthetic_validation_observed_utc=OBSERVED.isoformat(),
            synthetic_training_cutoff_utc=CUTOFF.isoformat(),
            source_hashes=before_code,
            runtime={
                "python": sys.version,
                "packages": {
                    n: importlib.metadata.version(n)
                    for n in ("numpy", "pandas", "scipy", "penaltyblog", "SQLAlchemy")
                },
            },
        ),
    )
    make_targets()
    print(
        "Prepared immutable training code and one T66 DB copy; 2615 frozen files hashed", flush=True
    )


def make_targets():
    targets, future_excluded = {}, []
    with closing(connect(DB)) as db:
        db.row_factory = sqlite3.Row
        for league in OLD7:
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT m.match_id,m.source,m.league,m.kickoff_utc,"
                    "h.name AS home,a.name AS away FROM matches m "
                    "JOIN teams h ON h.id=m.home_team_id JOIN teams a ON a.id=m.away_team_id "
                    "WHERE m.league=? ORDER BY m.kickoff_utc,m.match_id",
                    (league,),
                )
            ]
            future = [r for r in rows if datetime.fromisoformat(r["kickoff_utc"]) > CUTOFF]
            future_excluded.extend(r for r in future if r["source"] not in ("fd", "jc"))
            actual = [
                dict(r, input_kind="real_applicable_future_target")
                for r in future
                if r["source"] in ("fd", "jc")
            ]
            pairs = {}
            for row in rows:
                if datetime.fromisoformat(row["kickoff_utc"]) < CUTOFF and row["source"] in (
                    "fd",
                    "jc",
                ):
                    pairs.setdefault((row["home"], row["away"]), row)
            diagnostic = [
                dict(
                    r,
                    original_match_id=r["match_id"],
                    original_kickoff_utc=r["kickoff_utc"],
                    kickoff_utc=(CUTOFF + timedelta(days=1)).isoformat(),
                    input_kind="real_historical_pair_with_synthetic_future_kickoff",
                )
                for _, r in sorted(pairs.items())
            ]
            targets[league] = actual + diagnostic
    write(ROOT / "prediction-targets.json", targets)
    write(
        ROOT / "prediction-target-coverage.json",
        dict(
            applicable_real_future_targets=sum(
                sum(r["input_kind"] == "real_applicable_future_target" for r in rows)
                for rows in targets.values()
            ),
            excluded_future_targets=future_excluded,
            synthetic_time_diagnostic_counts={k: len(v) for k, v in targets.items()},
            explanation=(
                "Only numerical regression: historical pair names, "
                "synthetic future kickoff; no performance claim"
            ),
        ),
    )


def reconcile_and_roundtrip(calls):
    from zucai.cloud_feeds import check_db, export_feeds, restore_feeds
    from zucai.source_result_reconcile import reconcile_source_results

    before = read(ROOT / "tables-before.json")
    assert snapshot(DB) == before
    first = reconcile_source_results(DB, OBSERVED)
    after = snapshot(DB)
    assert all(after[table] == value for table, value in before.items())
    assert set(after) - set(before) == {"result_source_identities", "result_source_links"}
    write(ROOT / "reconcile-first.json", first)
    write(ROOT / "tables-after-reconcile.json", after)
    second = reconcile_source_results(DB, OBSERVED + timedelta(seconds=30))
    assert second["linked"] == 0
    assert snapshot(DB) == after
    write(ROOT / "reconcile-repeat.json", second)
    with closing(connect(DB)) as db:
        check_db(db)
        assert db.execute(
            "SELECT counterpart_id FROM matches WHERE match_id=?", (SWE,)
        ).fetchone() == (SWE_FD,)
        assert not db.execute(
            "SELECT 1 FROM result_source_identities WHERE match_id=?", (SWE,)
        ).fetchone()
        assert not db.execute(
            "SELECT 1 FROM result_source_links WHERE provider_match_id=? OR counterpart_id=?",
            (SWE, SWE),
        ).fetchone()
        for table in ("result_source_identities", "result_source_links"):
            timestamps = [r[0] for r in db.execute(f"SELECT DISTINCT verified_utc FROM {table}")]
            assert all(datetime.fromisoformat(t) == OBSERVED for t in timestamps)
    write(
        ROOT / "swe-partial-proof.json",
        dict(
            apif_match_id=SWE,
            legacy_counterpart=SWE_FD,
            partial_pointer_preserved=True,
            bilateral_identity_proofs=0,
            dated_source_links=0,
            original_matches_teams_results_unchanged=True,
        ),
    )
    export_dir = ROOT / "temporary-export"
    assert not export_dir.exists()
    manifest = export_feeds(DB, export_dir, CUTOFF.date(), "t63-offline-synthetic-validation")
    write(ROOT / "export-manifest-copy.json", manifest)
    DB.unlink()
    restore_feeds(export_dir, DB)
    restored = snapshot(DB)
    assert restored == after
    with closing(connect(DB)) as db:
        check_db(db)
    write(ROOT / "tables-after-roundtrip.json", restored)
    shutil.rmtree(export_dir)
    result = dict(
        first_reconcile=first,
        repeated_reconcile=second,
        all_old_tables_unchanged=True,
        all_table_roundtrip_hashes_equal=True,
        table_count=len(restored),
        database_sha256=sha(DB),
        http_calls=calls,
        **protect(),
    )
    write(ROOT / "roundtrip-summary.json", result)
    write(ROOT / "reconcile-imported-modules.json", imported_modules())
    print(json.dumps(result, ensure_ascii=False), flush=True)


def corpus(calls):
    from zucai.predict.command import load_corpus
    from zucai.predict.pooled import select_training_history

    params = CODE / "data/model/params.json"
    config = read(params)
    baseline = read(T66 / "training-ids-after.json")
    summaries, ids = {}, {}
    for league, entry in config["leagues"].items():
        try:
            games, _, _, digest = load_corpus(
                DB, league, ",".join(entry["sources"]), params_file=params, as_of=CUTOFF
            )
            eligible = select_training_history(games, CUTOFF)
        except Exception as exc:
            summaries[league] = dict(status="failed", error=f"{type(exc).__name__}: {exc}")
            print(f"CORPUS FAILED {league}: {exc}", flush=True)
            continue
        ids[league] = sorted(eligible.match_id)
        summaries[league] = dict(
            status="read",
            corpus_rows=len(games),
            training_rows=len(eligible),
            sources=entry["sources"],
            source_counts={str(k): int(v) for k, v in eligible.source.value_counts().items()},
            database_sha256=digest,
            linked_source_selection=games.attrs.get("linked_source_selection", False),
            legacy_source_policy=games.attrs.get("legacy_source_policy", False),
            source_identity_exclusions=eligible.attrs.get("unverified_fd_identity_overlaps", []),
            training_league_scope=games.attrs.get("training_leagues"),
            removed_ids=sorted(set(baseline[league]) - set(ids[league])),
            added_ids=sorted(set(ids[league]) - set(baseline[league])),
        )
        if league in OLD7:
            assert ids[league] == baseline[league], league
        if league == "欧战":
            assert SWE not in ids[league] and SWE_FD in ids[league]
        if league == "美职":
            held = summaries[league]["source_identity_exclusions"]
            assert {r["match_id"] for r in held} == {"apif:1490585", "apif:1490596"}
            assert all(
                r["match_id"] not in ids[league] and r["fd_candidate_id"] in ids[league]
                for r in held
            )
            with closing(connect(DB)) as db:
                assert not db.execute(
                    "SELECT 1 FROM result_source_links "
                    "WHERE provider_match_id IN ('apif:1490585','apif:1490596')"
                ).fetchone()
                assert all(
                    r[0] is None
                    for r in db.execute(
                        "SELECT counterpart_id FROM matches "
                        "WHERE match_id IN ('apif:1490585','apif:1490596')"
                    )
                )
        print(f"CORPUS {league}: {len(eligible)}", flush=True)
    write(ROOT / "corpus.json", summaries)
    write(ROOT / "training-ids.json", ids)
    write(ROOT / "corpus-imported-modules.json", imported_modules())
    assert len(ids) == 19
    assert not calls


def validate_final(calls):
    from zucai.cloud_feeds import check_db, export_feeds, restore_feeds
    from zucai.source_result_reconcile import reconcile_source_results

    expected = read(ROOT / "tables-after-roundtrip.json")
    assert snapshot(DB) == expected
    before_hash = sha(DB)
    repeated = reconcile_source_results(DB, OBSERVED + timedelta(seconds=45))
    assert repeated["linked"] == 0 and repeated["unchanged"] == 165
    assert snapshot(DB) == expected
    with closing(connect(DB)) as db:
        check_db(db)
    archive = ROOT / "temporary-final-export"
    assert not archive.exists()
    exported = export_feeds(DB, archive, CUTOFF.date(), "t63-final-source-offline-validation")
    write(ROOT / "final-export-manifest-copy.json", exported)
    DB.unlink()
    restore_feeds(archive, DB)
    assert snapshot(DB) == expected
    with closing(connect(DB)) as db:
        check_db(db)
    shutil.rmtree(archive)
    result = dict(
        final_source_reconcile=repeated,
        all_16_tables_equal_before_after=True,
        database_sha256_before=before_hash,
        database_sha256_after=sha(DB),
        imported_modules=imported_modules(),
        http_calls=calls,
        **protect(),
    )
    write(ROOT / "final-source-validation.json", result)
    print("Final-source reconcile/check/export/restore passed with all 16 table hashes equal")


def train_models(calls):
    from zucai.predict.command import train

    params = CODE / "data/model/params.json"
    config = read(params)
    ids = read(ROOT / "training-ids.json")
    before = sha(DB)
    hashes = code_hashes(CODE)
    tree_sha = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    outcomes = {}
    for league, entry in config["leagues"].items():
        model = ROOT / f"training/{league}.pkl"
        report = model.with_suffix(".train.json")
        if report.exists():
            saved = read(report)
            if saved.get("status") == "trained":
                assert saved["database_sha256"] == before
                assert saved["source_tree_sha256"] == tree_sha
                assert saved["model_sha256"] == sha(model)
                outcomes[league] = saved
                continue
        start = time.monotonic()
        print(f"TRAIN START {league}", flush=True)
        try:
            result = train(
                db=DB,
                league=league,
                source=",".join(entry["sources"]),
                since=CUTOFF - timedelta(days=56),
                as_of=CUTOFF,
                output=model,
                params_file=params,
            )
            assert result["training_rows"] == len(ids[league])
            if league == "美职":
                assert {r["match_id"] for r in result["source_identity_exclusions"]} == {
                    "apif:1490585",
                    "apif:1490596",
                }
            result.update(
                status="trained",
                synthetic_regression_clock=True,
                source_tree_sha256=tree_sha,
                model_sha256=sha(model),
            )
        except Exception as exc:
            traceback.print_exc()
            result = dict(status="failed", error=f"{type(exc).__name__}: {exc}")
        result["seconds"] = time.monotonic() - start
        write(report, result)
        outcomes[league] = result
        assert sha(DB) == before
        print(f"TRAIN END {league}: {result['status']} ({result['seconds']:.2f}s)", flush=True)
    assert code_hashes(CODE) == hashes
    write(ROOT / "training-imported-modules.json", imported_modules())
    summary = dict(
        database_sha256=before,
        database_unchanged=True,
        synthetic_regression_cutoff_utc=CUTOFF.isoformat(),
        trained=sum(v["status"] == "trained" for v in outcomes.values()),
        failed=sum(v["status"] != "trained" for v in outcomes.values()),
        outcomes={
            k: {
                p: v.get(p) for p in ("status", "error", "training_rows", "seconds", "model_sha256")
            }
            for k, v in outcomes.items()
        },
        http_calls=calls,
        **protect(),
    )
    write(ROOT / "training-summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    assert summary["trained"] == 19 and summary["failed"] == 0


def signatures(label, calls):
    import numpy as np
    from zucai.predict.dixon_coles import DixonColes

    directory = T66 / "training" if label == "t66" else ROOT / "training"
    output = ROOT / f"signatures-{label}"
    output.mkdir(exist_ok=True)
    targets = read(ROOT / "prediction-targets.json")
    all_numeric = {}
    for league in OLD7:
        model = DixonColes.load(directory / f"{league}.pkl")
        assert model.as_of.to_pydatetime() == CUTOFF
        numeric = dict(
            engine_parameters=model.engine.get_params(),
            prior_parameters=model.prior_parameters,
            draw_offset=model.draw_offset,
            draw_calibration=model.draw_calibration,
            half_life_days=model.half_life_days,
            training_rows=model.training_rows,
            calibration_training_rows=model.calibration_training_rows,
            calibration_skipped_training_rows=model.calibration_skipped_training_rows,
            unknown_team_prior=model.unknown_team_prior,
            teams=sorted(model.teams),
            training_source_counts=model.training_source_counts,
        )
        grids = np.stack([model.predict_score_matrix(r) for r in targets[league]])
        probabilities = np.stack(
            [
                np.tril(grids, -1).sum(axis=(1, 2)),
                np.trace(grids, axis1=1, axis2=2),
                np.triu(grids, 1).sum(axis=(1, 2)),
            ],
            axis=1,
        )
        np.savez_compressed(output / f"{league}.npz", matrices=grids, probabilities=probabilities)
        all_numeric[league] = numeric
        print(f"SIGNATURE {label} {league}: {len(grids)} diagnostic inputs", flush=True)
    write(output / "parameters.json", all_numeric)
    write(
        output / "provenance.json",
        dict(
            label=label,
            models={k: sha(directory / f"{k}.pkl") for k in OLD7},
            imported_modules=imported_modules(),
            targets_sha256=sha(ROOT / "prediction-targets.json"),
            http_calls=calls,
        ),
    )


def compare(calls):
    import numpy as np

    left = read(ROOT / "signatures-t66/parameters.json")
    right = read(ROOT / "signatures-t63/parameters.json")
    summary = {}
    for league in OLD7:
        assert left[league] == right[league], f"{league}: fitted parameters changed"
        with (
            np.load(ROOT / f"signatures-t66/{league}.npz") as a,
            np.load(ROOT / f"signatures-t63/{league}.npz") as b,
        ):
            assert set(a.files) == set(b.files)
            for key in a.files:
                assert np.array_equal(a[key], b[key]), (
                    league,
                    key,
                    float(np.max(np.abs(a[key] - b[key]))),
                )
            summary[league] = dict(
                numeric_parameters_equal=True,
                engine_parameter_count=len(left[league]["engine_parameters"]),
                diagnostic_pairs=len(a["matrices"]),
                score_matrix_values=int(a["matrices"].size),
                all_score_matrix_values_exactly_equal=True,
                all_1x2_values_exactly_equal=True,
                max_absolute_difference=0.0,
            )
    result = dict(
        old_seven=summary,
        prediction_target_coverage=read(ROOT / "prediction-target-coverage.json"),
        applicable_real_future_targets=0,
        synthetic_future_diagnostics_only=True,
        no_performance_experiment=True,
        http_calls=calls,
        **protect(),
    )
    write(ROOT / "old-seven-comparison.json", result)
    print(json.dumps(result, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "phase",
        choices=(
            "prepare",
            "reconcile",
            "corpus",
            "train",
            "validate-final",
            "signatures-t66",
            "signatures-t63",
            "compare",
        ),
    )
    args = parser.parse_args()
    calls = []

    def blocked(*args, **kwargs):
        calls.append(repr(args[:2]))
        raise AssertionError("Network disabled by independent acceptance runner")

    with ExitStack() as stack:
        for name in (
            "requests.sessions.Session.request",
            "socket.create_connection",
            "socket.socket.connect",
        ):
            stack.enter_context(patch(name, side_effect=blocked))
        if args.phase == "prepare":
            prepare()
        elif args.phase == "reconcile":
            reconcile_and_roundtrip(calls)
        elif args.phase == "corpus":
            corpus(calls)
        elif args.phase == "train":
            train_models(calls)
        elif args.phase == "validate-final":
            validate_final(calls)
        elif args.phase.startswith("signatures-"):
            signatures(args.phase.split("-")[1], calls)
        elif args.phase == "compare":
            compare(calls)
    assert not calls


if __name__ == "__main__":
    main()
