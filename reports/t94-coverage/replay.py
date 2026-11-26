"""Recreate T94 coverage against the two pinned production archives in disposable SQLite."""

import argparse
import hashlib
import json
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from itertools import islice
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote
from urllib.request import urlopen

from sqlalchemy.orm import Session

from zucai.db import get_engine, init_db
from zucai.digest_context import audit_rows
from zucai.forecast_coverage import build_coverage, coverage_summary
from zucai.models import SfcIssue
from zucai.predict.pooled import CUPS, POOL
from zucai.report.batch import predict_batch
from zucai.report.sfc import _pick_nine_eligible, build_sfc_report
from zucai.sfc_club_names import load as load_sfc_clubs

RAW = "https://raw.githubusercontent.com/qw5563680/FOOTBALL/"
FORECAST_COMMIT = "c4c52b06d34c94c8c31f4b2933dd6c2dd6f1eeb7"
FORECAST_MANIFEST_SHA = "4d56bff2dbc7ba01504b482fc67ea8ea123c925096a03c75e153b0d7a3de1323"
REPLAYS = {"26170": "45537628477-1", "26171": "45769244983-1"}
EXCLUDED_26171 = {
    5: "apif:1636329",
    9: "apif:1636277",
    10: "apif:1636294",
    14: "apif:1636349",
}


def read_verified(root, commit, name, expected, *, fetch):
    target = root / name
    if fetch and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        with urlopen(f"{RAW}{commit}/{quote(name)}", timeout=60) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(f"{name}: downloaded SHA-256 differs from pinned manifest")
        target.write_bytes(data)
    with target.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
            raise ValueError(f"{name}: local SHA-256 differs from pinned manifest")
    return target


def read_json(path):
    return json.loads(path.read_bytes())


def insert(connection, table, columns, path):
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
    with path.open("rb") as stream:
        while chunk := list(islice(stream, 4096)):
            connection.exec_driver_sql(sql, [tuple(json.loads(line)) for line in chunk])


def load_replay(root, issue, run, manifest, *, fetch):
    feed_commit = run["feed_snapshot"]["commit"]
    feed = read_json(
        read_verified(
            root, feed_commit, "feed_manifest.json", run["feed_manifest_sha256"], fetch=fetch
        )
    )
    snapshot_paths = sorted(name for name in feed["files"] if name.startswith("state/sfc/"))
    audit_paths = sorted(
        name
        for name in feed["files"]
        if name.startswith(("state/audit/jc_catalog", "state/audit/jc_skipped"))
    )
    paths = [
        "state/teams.jsonl",
        "state/matches.jsonl",
        "state/results.jsonl",
        *snapshot_paths,
        *audit_paths,
    ]
    if feed.get("result_source_links"):
        paths.append("state/result_source_links.jsonl")
    if issue == "26171":
        paths.append("state/odds/2026-11.jsonl")
    inputs = [(feed_commit, name, feed["files"][name]) for name in paths]
    batch_paths = [f"{row['path']}/forecast.json" for row in run["forecasts"].values()]
    inputs.extend((FORECAST_COMMIT, name, manifest["files"][name]) for name in batch_paths)
    if issue == "26171":
        for league in ("欧战", "澳超"):
            model = run["leagues"][league]
            assert manifest["files"][model["model_path"]] == model["sha256"]
            inputs.append((FORECAST_COMMIT, model["model_path"], model["sha256"]))

    def verify(spec):
        return read_verified(root, *spec, fetch=fetch)

    with ThreadPoolExecutor(max_workers=8) as workers:
        list(workers.map(verify, inputs))
    return feed, snapshot_paths, [read_json(root / name) for name in batch_paths]


def rerun_t93(session, root, run, batches, params):
    with params.open("rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == run["params_sha256"]
    index = next(
        i
        for i, batch in enumerate(batches)
        if batch["input"]["kickoff_date_beijing"] == "2026-11-27"
    )
    old = batches[index]
    assert old["input"]["market_weight"] == 0
    targets = set(EXCLUDED_26171.values())
    old_targets = {row["match_id"]: row for row in old["predictions"] if row.get("sfc_only")}
    assert set(old_targets) == targets and all(
        p["status"] == "excluded" for p in old_targets.values()
    )
    models = {league: run["leagues"][league] for league in ("欧战", "澳超")}
    as_of = datetime.fromisoformat(old["as_of_utc"])
    batch = predict_batch(
        session,
        model_files=[root / item["model_path"] for item in models.values()],
        as_of=as_of,
        generated=as_of,
        match_ids=old["input"]["match_ids"],
        sfc_only_ids=targets,
        market_weight=0,
        expected_identities=old["input"]["expected_identities"],
        params_file=params,
        persist=False,
        allow_empty=True,
        prospective_evidence=old["prospective_evidence"],
        half_share_hints={league: version["half_share"] for league, version in models.items()},
    )
    batch["input"]["kickoff_date_beijing"] = old["input"]["kickoff_date_beijing"]
    batch["batch_id"] = old["batch_id"]
    batch["generated_utc"] = old["generated_utc"]
    original_rows = {p["match_id"]: p for p in old["predictions"] if not p.get("sfc_only")}
    for i, row in enumerate(batch["predictions"]):
        if row["match_id"] in targets:
            assert row["status"] == "predicted" and row["sfc_only"] is True
        else:
            # Keep every archived JC row identical. Only re-run T93's four routes.
            batch["predictions"][i] = original_rows[row["match_id"]]
    batches[index] = batch


def selection_input(session, issue, batches, generated):
    models = {(m["league"], m["model_version"]): m for batch in batches for m in batch["models"]}
    combined = {
        **batches[-1],
        "models": list(models.values()),
        "predictions": [r for batch in batches for r in batch["predictions"]],
        "input": {"reference_only": True},
    }
    return build_sfc_report(
        session,
        session.get(SfcIssue, issue),
        combined,
        forecast_sha256="0" * 64,
        generated=generated,
        allow_missing=True,
    )


def replay(root, issue, manifest, *, fetch, params):
    run = manifest["runs"][REPLAYS[issue]]
    feed, snapshot_paths, batches = load_replay(root, issue, run, manifest, fetch=fetch)
    generated = datetime.fromisoformat(run["generated_utc"])
    as_of = datetime.fromisoformat(batches[0]["as_of_utc"])
    assert all(datetime.fromisoformat(b["as_of_utc"]) == as_of for b in batches)
    assert datetime.fromisoformat(feed["created_utc"]) <= as_of <= generated
    aliases = {
        key: value
        for key, value in load_sfc_clubs().items()
        if datetime.fromisoformat(value["approved_utc"]) <= as_of
    }
    engine = get_engine(":memory:")
    init_db(engine)
    try:
        with engine.begin() as conn:
            for table in ("teams", "matches", "results"):
                insert(conn, table, feed["columns"][table], root / f"state/{table}.jsonl")
            if feed.get("result_source_links"):
                insert(
                    conn,
                    "result_source_links",
                    feed["result_source_links"]["columns"],
                    root / "state/result_source_links.jsonl",
                )
            if issue == "26171":
                insert(conn, "odds", feed["columns"]["odds"], root / "state/odds/2026-11.jsonl")
        with Session(engine) as session, patch("zucai.sfc_club_names._table", return_value=aliases):
            session.add_all(SfcIssue(**read_json(root / name)["issue"]) for name in snapshot_paths)
            session.flush()
            if issue == "26171":
                rerun_t93(session, root, run, batches, params)
            model_leagues = set(run["leagues"])
            if POOL in model_leagues:
                model_leagues.update(CUPS)
            coverage = build_coverage(
                session,
                run_id=run["run_id"],
                as_of=as_of,
                generated=generated,
                catalog=audit_rows(root, feed, "jc_catalog", as_of, all_shards=True),
                skipped=audit_rows(root, feed, "jc_skipped", as_of, all_shards=True),
                schedule=feed["collection"]["sfc"]["official_schedule"],
                batches=batches,
                configured_leagues=model_leagues,
            )
            selections = selection_input(session, issue, batches, generated)
    finally:
        engine.dispose()
    rows = coverage["sfc"][issue]
    formal = {
        p["match_id"]: p["model_probabilities"]
        for b in batches
        for p in b["predictions"]
        if p["status"] == "predicted"
    }
    covered = []
    for row in rows:
        assert row["formal"] == (row["baseline"] is None), row
        if row["formal"]:
            probabilities = row["formal_probabilities"]
            assert probabilities == formal[row["match_id"]]
        else:
            probabilities = row["baseline"]["probabilities"]
        assert math.isclose(sum(probabilities.values()), 1.0, rel_tol=0, abs_tol=1e-12)
        covered.append({**row, "probabilities": probabilities})
    assert [row["seq"] for row in covered] == list(range(1, 15))
    gaps = {row["seq"] for row in covered if not row["formal"]}
    assert gaps == ({1, 2, 11, 13} if issue == "26170" else {3}), gaps
    assert all(row["reason"] and row["action"] for row in covered if not row["formal"])
    assert all(
        r["probabilities"] is None and r["selection"] == list("hda")
        for r in selections["rows"]
        if r["seq"] in gaps
    )
    eligible = {r["seq"] for r in _pick_nine_eligible(selections["rows"], generated.isoformat())}
    assert not eligible & gaps
    if issue == "26170":
        assert all("没有配置正式模型" in covered[i - 1]["reason"] for i in (1, 2))
        assert all("队名" in covered[i - 1]["reason"] for i in (11, 13))
        assert coverage["jc_leagues_without_model"]["亚冠精英"] == 2
    else:
        assert "占位编号" in covered[2]["reason"]
        assert all(covered[seq - 1]["match_id"] == key for seq, key in EXCLUDED_26171.items())
    return {
        "issue": issue,
        "forecast_archive_commit": FORECAST_COMMIT,
        "forecast_run": run["run_id"],
        "feed_archive_commit": run["feed_snapshot"]["commit"],
        "as_of_utc": coverage["as_of_utc"],
        "generated_utc": coverage["generated_utc"],
        "summary": coverage_summary(coverage),
        "jc_leagues_without_model": coverage["jc_leagues_without_model"],
        "selection_input": {
            "formal_pick_nine_slots": sorted(eligible),
            "forced_three": sorted(gaps),
        },
        "rows": covered,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", type=Path, help="Output JSON file for offline replay")
    parser.add_argument(
        "--fetch", action="store_true", help="Prepare the pinned repository files only"
    )
    args = parser.parse_args()
    manifest = read_json(
        read_verified(
            args.input,
            FORECAST_COMMIT,
            "forecast_manifest.json",
            FORECAST_MANIFEST_SHA,
            fetch=args.fetch,
        )
    )
    if args.fetch:
        for issue, run_id in REPLAYS.items():
            load_replay(args.input / issue, issue, manifest["runs"][run_id], manifest, fetch=True)
        return
    if args.output is None:
        parser.error("offline replay requires --output")
    params = Path("data/model/params.json")
    report = [
        replay(args.input / issue, issue, manifest, fetch=False, params=params) for issue in REPLAYS
    ]
    args.output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    for item in report:
        print(item["forecast_run"], item["summary"])
        for row in item["rows"]:
            probabilities = " / ".join(f"{row['probabilities'][key]:.2%}" for key in "hda")
            print(
                item["issue"],
                row["seq"],
                row["home"],
                row["away"],
                "正式" if row["formal"] else "基准",
                probabilities,
                row["reason"] or "",
            )


if __name__ == "__main__":
    main()
