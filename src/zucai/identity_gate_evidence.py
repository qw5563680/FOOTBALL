"""Content checks for the read-only daily gate's materialized replay exports."""

import csv
import io
import json
import sqlite3
from collections import Counter, defaultdict
from hashlib import sha256
from pathlib import Path


def proof(root, reference):
    if not isinstance(reference, dict):
        raise ValueError("missing artifact reference")
    path = root / reference["path"]
    if (
        Path(reference["path"]).is_absolute()
        or path.is_symlink()
        or not path.resolve().is_relative_to(root.resolve())
        or any(p.is_symlink() for p in path.parents)
    ):
        raise ValueError("artifact path escapes evidence root")
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != reference["sha256"]:
        raise ValueError("artifact SHA mismatch")
    return raw


def document(root, reference):
    return json.loads(proof(root, reference))


def export(root, reference):
    """Read schema-checked rows and recalculate the collector's source inventory."""
    from zucai.cloud_feeds import TABLES, source_inventory
    from zucai.identity_binding_archive import _tables

    manifest = document(root, reference)
    folder = (root / reference["path"]).parent
    for path, digest in manifest["files"].items():
        proof(folder, dict(path=path, sha256=digest))
    tables, _ = _tables(folder)
    if not all(tables[name] for name in ("teams", "matches", "results")):
        raise ValueError("export contains empty core tables")
    with sqlite3.connect(":memory:") as db:
        for name, columns in TABLES.items():
            if manifest["columns"].get(name) != list(columns):
                raise ValueError("export schema inventory missing or invalid")
            db.execute(f"CREATE TABLE {name} ({','.join(columns)})")
            rows = []
            paths = (
                sorted(p for p in manifest["files"] if p.startswith("state/odds/"))
                if name == "odds" and "state/odds.jsonl" not in manifest["files"]
                else [f"state/{name}.jsonl"]
            )
            lines = [
                line
                for path in paths
                for line in proof(
                    folder, dict(path=path, sha256=manifest["files"][path])
                ).splitlines()
            ]
            for line in lines:
                row = json.loads(line)
                if not isinstance(row, list) or len(row) != len(columns):
                    raise ValueError("export row schema mismatch")
                rows.append(row)
                db.execute(
                    f"INSERT INTO {name} VALUES ({','.join('?' for _ in columns)})",
                    [json.dumps(v) if isinstance(v, (dict, list)) else v for v in row],
                )
            if len(rows) != manifest["counts"].get(name):
                raise ValueError("export counts differ from actual rows")
        if source_inventory(db) != manifest.get("sources"):
            raise ValueError("export sources differ from actual rows")
    team_ids = {r["id"] for r in tables["teams"]}
    matches = {r["match_id"]: r for r in tables["matches"]}
    results = {r["match_id"]: r for r in tables["results"]}
    if (
        any(
            r[f"{side}_team_id"] not in team_ids
            for r in matches.values()
            for side in ("home", "away")
        )
        or not results.keys() <= matches.keys()
        or any((r["status"] == "finished") != (mid in results) for mid, r in matches.items())
    ):
        raise ValueError("export identity/result referential integrity differs")
    return dict(folder=folder, manifest=manifest, tables=tables, matches=matches, results=results)


def snapshot(state, cutoff=None, league=None):
    """Derive a stable inventory from exported rows; no caller's counts are trusted."""
    coverage = defaultdict(Counter)
    for row in state["matches"].values():
        coverage[row["league"]][row["source"]] += 1
    return dict(
        coverage={k: dict(v) for k, v in coverage.items()},
        relations=sorted(
            [r["match_id"], r["counterpart_id"]]
            for r in state["matches"].values()
            if r.get("counterpart_id")
        ),
        quarantine={
            p: digest
            for p, digest in state["manifest"]["files"].items()
            if "quarantine" in p or "cross_day" in p
        },
        training_selection=training_selection(
            state, cutoff or state["manifest"]["created_utc"], league
        ),
    )


def training_selection(state, cutoff, league):
    """Run existing history/availability/quarantine selection in a disposable memory DB.

    The connection is memory-only. Its URL carries the source export directory solely
    so the existing receipt reader resolves this export's cross-day evidence.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from zucai.cross_day_decisions import RECEIPT_FILE, read_receipts
    from zucai.fd_stats_archive import restore_fd_links
    from zucai.models import Base
    from zucai.predict.backtest import read_history
    from zucai.predict.pooled import select_training_history
    from zucai.result_source_identity import restore_result_identities
    from zucai.result_source_links import restore_result_links

    db = sqlite3.connect(":memory:")
    engine = create_engine(
        "sqlite:///" + str(state["folder"] / "memory-only.db"), creator=lambda: db
    )
    try:
        Base.metadata.create_all(engine)
        for name in ("teams", "matches", "results"):
            from zucai.cloud_feeds import TABLES

            columns = TABLES[name]
            for row in state["tables"][name]:
                values = [row[c] for c in columns]
                db.execute(
                    f"INSERT INTO {name} ({','.join(columns)}) "
                    f"VALUES ({','.join('?' for _ in columns)})",
                    [json.dumps(v) if isinstance(v, (list, dict)) else v for v in values],
                )
        restore_result_links(db, state["folder"], state["manifest"])
        restore_fd_links(db, state["folder"], state["manifest"])
        restore_result_identities(db, state["folder"], state["manifest"])
        if (state["folder"] / RECEIPT_FILE).exists() and RECEIPT_FILE not in state["manifest"][
            "files"
        ]:
            raise ValueError("unlisted cross-day receipt cannot affect training selection")
        if RECEIPT_FILE in state["manifest"]["files"]:
            read_receipts(state["folder"] / RECEIPT_FILE)
        db.commit()
        leagues = {league} if league else {r["league"] for r in state["matches"].values()}
        selected = []
        with Session(engine) as session:
            for current in sorted(leagues):
                rows = [
                    r
                    for r in state["matches"].values()
                    if r["league"] == current and r["status"] == "finished"
                ]
                if not rows:
                    continue
                sources = sorted({r["source"] for r in rows})
                history = read_history(session, league=current, source=",".join(sources))
                selected.extend(select_training_history(history, cutoff).match_id)
        return sorted(selected)
    finally:
        engine.dispose()
        db.close()


def held_inventory(root, section):
    raw = proof(root, section["held_before"])
    if section["held_before_manifest"]["sha256"] not in {
        section.get(key, {}).get("sha256") for key in ("before_manifest", "after_manifest")
    }:
        raise ValueError("prior held inventory is not bound to a materialized replay export")
    manifest = document(root, section["held_before_manifest"])
    folder = (root / section["held_before_manifest"]["path"]).parent
    target = root / section["held_before"]["path"]
    if not target.resolve().is_relative_to(folder.resolve()):
        raise ValueError("prior held inventory has no independent archived source")
    path = target.relative_to(folder).as_posix()
    if manifest["files"].get(path) != section["held_before"]["sha256"]:
        raise ValueError("prior held inventory differs from independent archived source")
    reader = csv.DictReader(io.StringIO(raw.decode()))
    if not {"raw_sha256", "league", "provider_match_id"} <= set(reader.fieldnames or []):
        raise ValueError("prior held inventory schema missing")
    return list(reader)


def outcome(state, mid):
    owners = [
        r
        for r in state["matches"].values()
        if r["match_id"] == mid or r.get("counterpart_id") == mid
    ]
    if len(owners) > 1:
        raise ValueError("non_unique replay fixture relation")
    if not owners:
        return None
    row = {k: v for k, v in owners[0].items() if k != "row_number"}
    result = state["results"].get(row["match_id"])
    return row, {k: v for k, v in result.items() if k != "row_number"} if result else None


def transitions(root, section, outcomes, *, inserted):
    before, after = [export(root, section[key]) for key in ("before_manifest", "after_manifest")]
    if before["folder"].resolve() == after["folder"].resolve():
        raise ValueError("replay state exports must be independent")
    actual_inserted = 0
    for row in outcomes:
        mid, reported = row["fixture_id"], row["outcome"]
        old, new = outcome(before, mid), outcome(after, mid)
        if reported == "held_unbound":
            raise ValueError("held_unbound replay fixture remains unresolved")
        if reported in {"suspected", "rejected"}:
            if new is not None and old != new:
                raise ValueError("risky replay fixture changed stored state")
            continue
        if new is None:
            raise ValueError("replay outcome has no materialized fixture state")
        derived = (
            "unchanged"
            if old == new
            else "updated"
            if old
            else "inserted"
            if new[0]["match_id"] == mid
            else "linked"
        )
        if reported == "cancelled" and new[0]["status"] == "cancelled":
            derived = "cancelled"
        if reported != derived:
            raise ValueError("replay outcome differs from actual state transition: " + mid)
        actual_inserted += derived == "inserted"
    if inserted != actual_inserted:
        raise ValueError("inserted count differs from actual state transitions")
    return dict(
        inserted=actual_inserted,
        before_sha256=section["before_manifest"]["sha256"],
        after_sha256=section["after_manifest"]["sha256"],
    )


def existing_links(root, tables):
    """Validate archived result-link proofs; retain stale snapshots for visible gaps."""
    from zucai.result_source_links import (
        RESULT_LINK_COLUMNS,
        RESULT_LINK_FILE,
        _validate_rows,
        validate_result_links_manifest,
    )

    manifest = json.loads((root / "feed_manifest.json").read_bytes())
    if not validate_result_links_manifest(manifest):
        return []
    digest = manifest["files"][RESULT_LINK_FILE]
    raw = proof(root, dict(path=RESULT_LINK_FILE, sha256=digest))
    values = [json.loads(line) for line in raw.splitlines()]
    if any(not isinstance(row, list) or len(row) != len(RESULT_LINK_COLUMNS) for row in values):
        raise ValueError("result source link schema mismatch")
    rows = [dict(zip(RESULT_LINK_COLUMNS, row, strict=True)) for row in values]
    if len(rows) != manifest["result_source_links"]["count"]:
        raise ValueError("result source link count mismatch")
    matches = {r["match_id"]: r for r in tables["matches"]}
    _validate_rows(rows, matches, {r["id"]: r["name"] for r in tables["teams"]})
    return [
        dict(
            row,
            provenance=dict(
                path=RESULT_LINK_FILE, sha256=digest, pointer=f"{RESULT_LINK_FILE}#/line/{i}"
            ),
        )
        for i, row in enumerate(rows, 1)
    ]
