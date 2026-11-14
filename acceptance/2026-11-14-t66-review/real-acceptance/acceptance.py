#!/usr/bin/env python3
"""Offline real-artifact T66 replay and optional actual daily-training acceptance.

The cutoff is a synthetic regression clock, not a historical prediction claim.
Only this runner's output/work directories are written; sports HTTP is blocked.
"""

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import sqlite3
import sys
import time
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

FROZEN_PREFIXES = ("forecasts", "models", "scoreboard", "sfc", "daily", "experiments", "feed_manifest.json", "forecast_manifest.json")
ARCHIVE_COMMIT = "e5848e5d7de83a0cd024fa3e03e08cf6db6c7ee4"
ARCHIVE_MANIFEST_SHA256 = "c95a7e636f2d4e9005cc85f6b6514146c81c3d83eb460e06fdfb28c2e2a3ca83"


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def tree_hashes(root, prefixes):
    return {
        str(path.relative_to(root)): sha(path)
        for prefix in prefixes
        for path in ([root / prefix] if (root / prefix).is_file() else sorted((root / prefix).rglob("*")))
        if path.is_file()
    }


def source_hashes(code):
    return {
        str(path.relative_to(code)): sha(path)
        for prefix in ("src", "data")
        for path in sorted((code / prefix).rglob("*"))
        if path.is_file() and path.suffix in (".py", ".json")
    }


def snapshot(database):
    tables = {}
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as db:
        for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            columns = [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]
            digest, count = hashlib.sha256(), 0
            cols = ",".join(f'"{column}"' for column in columns)
            for row in db.execute(f'SELECT {cols} FROM "{table}" ORDER BY {cols}'):
                digest.update((json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
                count += 1
            tables[table] = dict(columns=columns, rows=count, sha256=digest.hexdigest())
    return tables


def rows(database, table):
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        return {row[0]: dict(row) for row in db.execute(f'SELECT * FROM "{table}"')}


def corpus(database, code, cutoff):
    from zucai.predict.command import load_corpus

    params = code / "data/model/params.json"
    config = json.loads(params.read_text())
    summaries, identifiers = {}, {}
    for league, entry in config["leagues"].items():
        try:
            games, _, _, digest = load_corpus(
                db=database, league=league, source=",".join(entry["sources"]),
                params_file=params, as_of=cutoff,
            )
            eligible = games.loc[
                (games.kickoff_utc < cutoff) & (games.available_utc < cutoff)
                & (games.available_utc > games.kickoff_utc)
            ]
            identifiers[league] = sorted(eligible.match_id)
            summaries[league] = dict(
                status="read", training_rows=len(eligible), corpus_rows=len(games),
                history_match_ids_sha256=hashlib.sha256("\n".join(sorted(games.match_id)).encode()).hexdigest(),
                sources=entry["sources"], database_sha256=digest,
                source_counts={str(k): int(v) for k, v in eligible.source.value_counts().items()},
                training_league_scope=games.attrs.get("training_leagues"),
                linked_rows_excluded=games.attrs.get("linked_rows_excluded", []),
            )
        except Exception as exc:
            summaries[league] = dict(status="failed", error=f"{type(exc).__name__}: {exc}")
    return summaries, identifiers


def source_inputs(raw):
    selected, rejected = [], []
    expected = json.loads(Path(__file__).with_name("source-manifest.json").read_text())["source_files"]
    for path in sorted(raw.glob("*.csv")):
        meta_path = path.with_suffix(path.suffix + ".meta.json")
        meta = json.loads(meta_path.read_text())
        row = dict(path=str(path), name=path.name, bytes=path.stat().st_size,
                   sha256=sha(path), meta_sha256=sha(meta_path), metadata=meta)
        assert row["sha256"] == meta["sha256"], path
        assert {key: row[key] for key in expected[path.name]} == expected[path.name], path
        (selected if meta["status_code"] == 200 and row["bytes"] else rejected).append(row)
    assert len(selected) == len(rejected) == 15
    assert {row["name"] for row in selected + rejected} == set(expected)
    return selected, rejected


def duplicate_pairs(before_matches, after_matches, old_results, new_results, new_fd):
    pairs = []
    for key in new_fd:
        fd = after_matches[key]
        for apif in before_matches.values():
            if apif["source"] != "apif" or apif["league"] != fd["league"]:
                continue
            delta = abs((datetime.fromisoformat(apif["kickoff_utc"]) - datetime.fromisoformat(fd["kickoff_utc"])).total_seconds())
            if delta > 3 * 3600:
                continue
            home = apif["home_team_id"] == fd["home_team_id"]
            away = apif["away_team_id"] == fd["away_team_id"]
            if not (home or away):
                continue
            ar, fr = old_results.get(apif["match_id"]), new_results.get(key)
            pairs.append(dict(
                apif_id=apif["match_id"], fd_id=key, league=fd["league"],
                match_basis="both_team_ids" if home and away else "one_team_id",
                kickoff_difference_seconds=delta,
                original_apif=apif, original_apif_result=ar, fd=fd, fd_result=fr,
                same_full_score=bool(ar and fr and (ar["home_goals"], ar["away_goals"]) == (fr["home_goals"], fr["away_goals"])),
            ))
    return sorted(pairs, key=lambda row: row["apif_id"])


def finish_baseline(args, calls):
    """Keep completed first-import evidence; avoid redundant old-code round trips."""
    out, archive = args.out, args.archive
    before = json.loads((out / "tables-before.json").read_text())
    after = json.loads((out / "tables-after.json").read_text())
    database = out / "work/data/zucai.db"
    assert snapshot(database) == after
    manifest = json.loads((archive / "feed_manifest.json").read_text())
    old_matches = {
        row[0]: dict(zip(manifest["columns"]["matches"], row))
        for row in map(json.loads, (archive / "state/matches.jsonl").read_text().splitlines())
    }
    changes = {row["match_id"]: row for row in json.loads((out / "old-match-changes.json").read_text())}
    proof_match_ids = [row[1] for row in map(json.loads, (archive / "state/fd_stats_links.jsonl").read_text().splitlines())]
    proof_pointers = [dict(
        match_id=key, before=old_matches[key]["counterpart_id"],
        after=changes[key]["after"]["counterpart_id"] if key in changes else old_matches[key]["counterpart_id"],
    ) for key in sorted(proof_match_ids)]
    write(out / "fd-proof-pointer-comparison.json", proof_pointers)
    frozen_before = json.loads((out / "frozen-before.json").read_text())
    frozen_after = {key: sha(archive / key) for key in frozen_before}
    write(out / "frozen-after.json", frozen_after)
    assert frozen_before == frozen_after
    assert not json.loads((out / "old-result-changes.json").read_text())
    assert before["fd_stats_links"] == after["fd_stats_links"]
    totals = Counter()
    for row in json.loads((out / "first-import-details.json").read_text()):
        totals.update({key: value for key, value in row["result"].items() if type(value) is int})
    new_fd = json.loads((out / "new-fd-matches.json").read_text())
    pairs = json.loads((out / "apif-fd-candidate-pairs.json").read_text())
    corpora = json.loads((out / "corpus-after.json").read_text())
    summary = dict(
        code_label=args.code_label, phase="baseline_first_import_only",
        synthetic_regression_cutoff_utc=args.cutoff,
        first_import=dict(totals), new_fd_rows=len(new_fd),
        new_fd_by_league=dict(Counter(row["match"]["league"] for row in new_fd)),
        old_results_unchanged=before["results"]["rows"], fd_links_unchanged=52,
        original_51_fdorg_pointers_unchanged=sum(str(row["before"]).startswith("fdorg:") and row["after"] == row["before"] for row in proof_pointers),
        candidate_pair_bases=dict(Counter(row["match_basis"] for row in pairs)),
        full_candidate_pairs_by_league=dict(Counter(row["league"] for row in pairs if row["match_basis"] == "both_team_ids")),
        latest_fd_kickoff_before=max(row["kickoff_utc"] for row in old_matches.values() if row["source"] == "fd"),
        latest_fd_kickoff_after=max(row["match"]["kickoff_utc"] for row in new_fd),
        corpus_failures={key: value for key, value in corpora.items() if value["status"] == "failed"},
        frozen_files_unchanged=len(frozen_before), http_calls=calls,
        repeated_import_and_roundtrip="intentionally skipped for baseline; required for fixed-code acceptance",
        completed_first_import_tables_preserved=True,
    )
    write(out / "summary.json", summary)
    if not args.keep_work:
        shutil.rmtree(out / "work")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


def replay(args, calls):
    from zucai.cloud_feeds import export_feeds, restore_feeds
    from zucai.sources.fd_ingest import COUNT_KEYS, fetch_fd

    out, code, archive, raw = args.out, args.code, args.archive, args.raw
    assert sha(archive / "feed_manifest.json") == ARCHIVE_MANIFEST_SHA256
    out.mkdir(parents=True, exist_ok=True)
    work = out / "work"
    database = work / "data/zucai.db"
    if database.exists():
        raise ValueError("Replay work database already exists; choose a new output directory")
    cutoff = datetime.fromisoformat(args.cutoff)
    selected, excluded = source_inputs(raw)
    assert all(datetime.fromisoformat(row["metadata"]["fetched_at_utc"]) < cutoff for row in selected)
    inputs = dict(
        code_label=args.code_label, source_hashes=source_hashes(code),
        runtime={"python": sys.version, "packages": {
            name: importlib.metadata.version(name)
            for name in ("penaltyblog", "pandas", "numpy", "scipy", "sqlalchemy")
        }},
        archive_commit=ARCHIVE_COMMIT, archive_manifest_sha256=sha(archive / "feed_manifest.json"),
        archive_feed_hashes=json.loads((archive / "feed_manifest.json").read_text())["files"],
        selected_csvs=selected, excluded_csvs=excluded,
        artifact_sha256="ab410c4abdacfffc914d8833419a31c1d2ebbb22c4836ba79cbf61ae84da37e5",
        synthetic_regression_cutoff_utc=cutoff.isoformat(),
        time_scope="Fixed synthetic regression clock only; no historical prediction or publication claim. Original captures and availability times are preserved.",
    )
    write(out / "inputs.json", inputs)
    frozen_before = tree_hashes(archive, FROZEN_PREFIXES)
    write(out / "frozen-before.json", frozen_before)
    restore_feeds(archive, database)
    before = snapshot(database)
    before_matches, before_results = rows(database, "matches"), rows(database, "results")
    proof_targets = [row["match_id"] for row in rows(database, "fd_stats_links").values()]
    assert before["fd_stats_links"]["rows"] == 52
    write(out / "tables-before.json", before)
    initial_corpus, initial_ids = corpus(database, code, cutoff)
    write(out / "corpus-before.json", initial_corpus)
    write(out / "training-ids-before.json", initial_ids)
    os.chdir(work)

    def import_all(label):
        totals = Counter()
        details = []
        for row in selected:
            path = Path(row["path"])
            token = path.name.split("-", 1)[0]
            scope = dict(country=token, from_season=2026, to_season=2026) if "-all_" in path.name else dict(div=token, seasons=["2627"])
            result = fetch_fd(
                **scope, assume=timedelta(hours=3), db=database, raw_file=path,
                audit_dir=database.parent / "state/audit", raw_root=work / "raw-unused",
            )
            details.append(dict(source=path.name, scope=scope, result=result))
            totals.update({key: result[key] for key in (*COUNT_KEYS, "downloaded_files", "processed_files")})
            print(f"{label} {token}: {result['inserted']} inserted, {result['linked_existing']} existing, {result['rejected_rows']} rejected, {result['suspected_duplicates']} suspected", flush=True)
        write(out / f"{label}-details.json", details)
        return dict(totals)

    first = import_all("first-import")
    after_matches, after_results = rows(database, "matches"), rows(database, "results")
    after = snapshot(database)
    write(out / "tables-after.json", after)
    old_result_changes = [dict(match_id=key, before=row, after=after_results.get(key)) for key, row in before_results.items() if row != after_results.get(key)]
    match_changes = [dict(match_id=key, before=row, after=after_matches.get(key)) for key, row in before_matches.items() if row != after_matches.get(key)]
    write(out / "old-match-changes.json", match_changes)
    proof_pointers = [dict(match_id=key, before=before_matches[key]["counterpart_id"],
                          after=after_matches[key]["counterpart_id"]) for key in sorted(proof_targets)]
    write(out / "fd-proof-pointer-comparison.json", proof_pointers)
    write(out / "old-result-changes.json", old_result_changes)
    assert not old_result_changes
    assert before["fd_stats_links"] == after["fd_stats_links"]
    new_fd = sorted(key for key in after_matches if key.startswith("fd:") and key not in before_matches)
    write(out / "new-fd-matches.json", [dict(match=after_matches[key], result=after_results.get(key)) for key in new_fd])
    pairs = duplicate_pairs(before_matches, after_matches, before_results, after_results, new_fd)
    write(out / "apif-fd-candidate-pairs.json", pairs)
    apif_changes = [row for row in match_changes if row["match_id"].startswith("apif:")]
    for change in apif_changes:
        assert {key: value for key, value in change["before"].items() if key != "counterpart_id"} == {
            key: value for key, value in change["after"].items() if key != "counterpart_id"
        }
        assert change["before"]["counterpart_id"] is None
        assert change["after"]["counterpart_id"].startswith("fd:")
    write(out / "linked-apif-changes.json", apif_changes)
    after_corpus, after_ids = corpus(database, code, cutoff)
    write(out / "corpus-after.json", after_corpus)
    write(out / "training-ids-after.json", after_ids)
    deltas = {
        league: dict(before=len(ids), after=len(after_ids[league]),
                     added=sorted(set(after_ids[league]) - set(ids)),
                     removed=sorted(set(ids) - set(after_ids[league])))
        for league, ids in initial_ids.items() if league in after_ids
    }
    write(out / "training-id-deltas.json", deltas)
    if args.baseline_only:
        finish_baseline(args, calls)
        return
    second = import_all("repeat-import")
    repeated = snapshot(database)
    write(out / "tables-repeat.json", repeated)
    assert second["inserted"] == 0
    assert after == repeated
    export_dir = out / "export"
    exported = export_feeds(database, export_dir, cutoff.date(), f"t66-offline-{args.code_label}")
    write(out / "export-manifest-copy.json", exported)
    # Free the owned DB before restore's temporary copy to keep disk usage bounded.
    database.unlink()
    restore_feeds(export_dir, database)
    roundtrip = snapshot(database)
    write(out / "tables-roundtrip.json", roundtrip)
    assert roundtrip == after
    frozen_after = tree_hashes(archive, FROZEN_PREFIXES)
    write(out / "frozen-after.json", frozen_after)
    assert frozen_before == frozen_after
    assert source_hashes(code) == inputs["source_hashes"]
    newest = lambda matches: max(row["kickoff_utc"] for row in matches.values() if row["source"] == "fd")
    summary = dict(
        code_label=args.code_label, synthetic_regression_cutoff_utc=cutoff.isoformat(),
        first_import=first, repeat_import=second, new_fd_rows=len(new_fd),
        new_fd_by_league=dict(Counter(after_matches[key]["league"] for key in new_fd)),
        old_results_unchanged=len(before_results), fd_links_unchanged=52,
        original_51_fdorg_pointers_unchanged=sum(str(row["before"]).startswith("fdorg:") and row["after"] == row["before"] for row in proof_pointers),
        old_match_change_count=len(match_changes), linked_apif_changes=len(apif_changes),
        candidate_pair_bases=dict(Counter(row["match_basis"] for row in pairs)),
        latest_fd_kickoff_before=newest(before_matches), latest_fd_kickoff_after=newest(after_matches),
        table_roundtrip_equal=True, table_repeat_equal=True, frozen_files_unchanged=len(frozen_before),
        corpus_failures={k: v for k, v in after_corpus.items() if v["status"] == "failed"},
        http_calls=calls, database_sha256=sha(database),
    )
    write(out / "summary.json", summary)
    shutil.rmtree(export_dir)
    if not args.keep_work:
        shutil.rmtree(work)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


def train_models(args, calls):
    from zucai.predict.command import train

    out, code = args.out, args.code
    database = out / "work/data/zucai.db"
    params = code / "data/model/params.json"
    config = json.loads(params.read_text())
    selected = args.only or list(config["leagues"])
    cutoff = datetime.fromisoformat(args.cutoff)
    before_hash, before_code = sha(database), source_hashes(code)
    validation_path = out / "final-source-validation.json"
    validated = json.loads((validation_path if validation_path.exists() else out / "inputs.json").read_text())
    assert before_code == validated["source_hashes"]
    if validation_path.exists():
        assert before_hash == validated["database_sha256"]
    code_digest = hashlib.sha256(json.dumps(before_code, sort_keys=True).encode()).hexdigest()
    recorded_ids = json.loads((out / "training-ids-after.json").read_text())
    training_dir = out / "training"
    training_dir.mkdir(exist_ok=True)
    outcomes = {}
    for league in selected:
        started = time.monotonic()
        target = training_dir / f"{league}.train.json"
        if args.resume and target.exists():
            saved = json.loads(target.read_text())
            if saved.get("status") == "trained":
                assert saved["database_sha256"] == before_hash
                assert saved["source_tree_sha256"] == code_digest
                assert datetime.fromisoformat(saved["cutoff_utc"]) == cutoff
                assert sha(training_dir / f"{league}.pkl") == saved["model_sha256"]
                outcomes[league] = saved
                continue
        print(f"TRAIN START {league}", flush=True)
        try:
            result = train(
                db=database, league=league, source=",".join(config["leagues"][league]["sources"]),
                since=cutoff - timedelta(days=56), as_of=cutoff,
                output=training_dir / f"{league}.pkl", params_file=params,
            )
            assert result["training_rows"] == len(recorded_ids[league])
            result.update(status="trained", synthetic_regression_clock=True,
                          seconds=time.monotonic() - started,
                          source_tree_sha256=code_digest,
                          model_sha256=sha(training_dir / f"{league}.pkl"))
        except Exception as exc:
            result = dict(status="failed", error=f"{type(exc).__name__}: {exc}", seconds=time.monotonic() - started)
        write(target, result)
        outcomes[league] = result
        assert sha(database) == before_hash
        print(f"TRAIN END {league}: {result['status']} ({result['seconds']:.2f}s)", flush=True)
    assert source_hashes(code) == before_code
    summary = dict(
        synthetic_regression_cutoff_utc=cutoff.isoformat(),
        same_train_arguments_as_cloud_predict=True,
        database_sha256=before_hash, database_unchanged=True,
        source_hashes=before_code, http_calls=calls,
        trained=sum(row["status"] == "trained" for row in outcomes.values()),
        failed=sum(row["status"] != "trained" for row in outcomes.values()), outcomes=outcomes,
    )
    write(out / "training-summary.json", summary)
    print(json.dumps({k: v for k, v in summary.items() if k not in ("source_hashes", "outcomes")}, ensure_ascii=False), flush=True)
    if summary["failed"]:
        raise SystemExit(1)


def fd_configuration():
    from zucai.league_registry import provider_config
    from zucai.sources.football_data import verified_league

    manifest = json.loads(Path(__file__).with_name("source-manifest.json").read_text())
    selected = sorted({name.split("-", 1)[0] for name, row in manifest["source_files"].items()
                       if row["metadata"]["status_code"] == 200 and row["bytes"]})
    config = provider_config("fd")
    return {code: verified_league(config, code) for code in selected}


def checkpoint(args, calls):
    out, database = args.out, args.out / "work/data/zucai.db"
    expected = json.loads((out / "inputs.json").read_text())
    assert source_hashes(args.code) == expected["source_hashes"]
    before = sha(database)
    summaries, identifiers = corpus(database, args.code, datetime.fromisoformat(args.cutoff))
    assert identifiers == json.loads((out / "training-ids-after.json").read_text())
    assert not [row for row in summaries.values() if row["status"] != "read"]
    assert sha(database) == before
    write(out / "import-source-reader-checkpoint.json", dict(
        code_label=args.code_label, source_hashes=source_hashes(args.code),
        database_sha256=before, fd_configuration=fd_configuration(), corpora=summaries,
        params_sha256=sha(args.code / "data/model/params.json"), http_calls=calls,
    ))
    print("Import-source full-history and FD15 configuration checkpoint verified", flush=True)


def validate_final(args, calls):
    from zucai.cloud_feeds import check_db, export_feeds, restore_feeds

    out, database = args.out, args.out / "work/data/zucai.db"
    prior = json.loads((out / "import-source-reader-checkpoint.json").read_text())
    before_tables = snapshot(database)
    assert before_tables == json.loads((out / "tables-after.json").read_text())
    config = fd_configuration()
    assert config == prior["fd_configuration"]
    assert sha(args.code / "data/model/params.json") == prior["params_sha256"]
    code_before = source_hashes(args.code)
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        check_db(db)
    export_dir = out / "final-source-export"
    assert not export_dir.exists()
    exported = export_feeds(database, export_dir, datetime.fromisoformat(args.cutoff).date(), f"t66-offline-{args.code_label}")
    write(out / "final-source-export-manifest-copy.json", exported)
    database.unlink()
    restore_feeds(export_dir, database)
    after_tables = snapshot(database)
    assert after_tables == before_tables
    write(out / "final-source-tables-roundtrip.json", after_tables)
    summaries, identifiers = corpus(database, args.code, datetime.fromisoformat(args.cutoff))
    assert identifiers == json.loads((out / "training-ids-after.json").read_text())
    for league, summary in summaries.items():
        assert summary["status"] == "read"
        previous = prior["corpora"][league]
        for key in ("history_match_ids_sha256", "corpus_rows", "training_rows", "source_counts", "training_league_scope", "linked_rows_excluded"):
            assert summary[key] == previous[key], (league, key)
    write(out / "corpus-final-source.json", summaries)
    write(out / "training-ids-final-source.json", identifiers)
    assert source_hashes(args.code) == code_before
    result = dict(
        code_label=args.code_label, source_hashes=code_before, database_sha256=sha(database),
        imported_source_label=prior["code_label"], imported_source_hashes=prior["source_hashes"],
        fd15_configuration_equal=True, fd_configuration=config, params_sha256=prior["params_sha256"],
        all_19_full_history_id_hashes_equal=True, all_19_eligible_training_ids_equal=True,
        all_table_roundtrip_hashes_equal=True, table_count=len(after_tables), http_calls=calls,
    )
    write(out / "final-source-validation.json", result)
    shutil.rmtree(export_dir)
    print("Final source DB validation, export/restore, FD15 scope and all 19 full-history/eligible-ID comparisons passed", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("replay", "train", "finish-baseline", "checkpoint", "validate-final"))
    parser.add_argument("--code", type=Path, required=True)
    parser.add_argument("--code-label", required=True)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--raw", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cutoff", default="2026-11-14T06:30:00+00:00")
    parser.add_argument("--keep-work", action="store_true")
    parser.add_argument("--baseline-only", action="store_true")
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    for name in ("code", "archive", "raw", "out"):
        if getattr(args, name) is not None:
            setattr(args, name, getattr(args, name).resolve())
    sys.path.insert(0, str(args.code / "src"))
    calls = []

    def blocked(*a, **kw):
        calls.append("blocked_network_attempt")
        raise AssertionError("Offline acceptance prohibits HTTP and socket network access")

    with patch("requests.sessions.Session.request", blocked), patch("urllib.request.urlopen", blocked), patch("socket.create_connection", blocked):
        {"replay": replay, "train": train_models, "finish-baseline": finish_baseline,
         "checkpoint": checkpoint, "validate-final": validate_final}[args.phase](args, calls)


if __name__ == "__main__":
    main()
