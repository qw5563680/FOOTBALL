"""Content checks for the read-only daily gate's materialized replay exports."""

import csv
import io
import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
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


def restorable_state_file(key):
    """Whether a manifest key is a state file that restore accepts and export rewrites.

    Raw captures and audit files are inputs, not exported state; they are bound by
    their own per-file proofs, never by an export comparison.
    """
    import re

    from zucai.cloud_feeds import (
        FD_LINK_FILE,
        MONTH,
        RESULT_IDENTITY_FILE,
        RESULT_LINK_FILE,
        STATE_FILE,
        STATS_FILES,
    )

    return key in STATS_FILES | {FD_LINK_FILE, RESULT_LINK_FILE, RESULT_IDENTITY_FILE} or bool(
        re.fullmatch(
            rf"state/(?:teams|matches|results|closing_odds)\.jsonl|"
            rf"state/(?:unmatched_teams|rejected_results)\.csv|"
            rf"state/odds/{MONTH}\.jsonl|matches/{MONTH}\.csv|"
            rf"state/sfc/[0-9]{{5}}\.json|{STATE_FILE}",
            key,
        )
    )


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


@contextmanager
def restored_memory(state):
    """Run existing history/availability/quarantine selection in a disposable memory DB.

    The connection is memory-only. Its URL carries the source export directory solely
    so the existing receipt reader resolves this export's cross-day evidence.
    """
    from sqlalchemy import create_engine

    from zucai.cross_day_decisions import RECEIPT_FILE, read_receipts
    from zucai.fd_stats_archive import restore_fd_links
    from zucai.models import Base
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
        yield engine, db
    finally:
        engine.dispose()
        db.close()


def training_selection(state, cutoff, league):
    """Use the existing history selectors and current receipt registry, without a fit."""
    from sqlalchemy.orm import Session

    from zucai.predict.backtest import read_history
    from zucai.predict.pooled import select_training_history

    with restored_memory(state) as (engine, _):
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
    if path != "state/audit/apif_held_unbound.csv":
        raise ValueError("prior held inventory must be the APIF held audit")
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
    if len({r["source"] for r in owners}) != len(owners):
        raise ValueError("non_unique replay fixture relation")
    for other in owners[1:]:
        first = owners[0]

        def utc(value):
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=UTC)

        scores = [state["results"].get(r["match_id"]) for r in (first, other)]
        if (
            any(
                first.get(k) != other.get(k)
                for k in ("league", "home_team_id", "away_team_id", "status")
            )
            or abs(utc(first["kickoff_utc"]) - utc(other["kickoff_utc"])) > timedelta(hours=3)
            or any((r is None) != (scores[0] is None) for r in scores)
            or scores[0] is not None
            and any(scores[0][k] != scores[1][k] for k in ("home_goals", "away_goals"))
        ):
            raise ValueError("contradictory replay fixture owners")
    owners.sort(key=lambda r: r["match_id"] != mid)
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
            "inserted"
            if mid not in before["matches"] and mid in after["matches"]
            else "unchanged"
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


def replay_importer(facts, code, section, root, report, scope, before, after):
    """Reproduce saved invocation semantics with the unchanged offline importer.

    Options are evidence, never guessed from capture time or modern collector defaults.
    Every counter (including supplemental counters) and resulting stored row is checked.
    """
    from sqlalchemy.orm import Session

    from zucai.identity_binding_archive import FORMAL
    from zucai.models import Base
    from zucai.sources.api_football import ApiFootball
    from zucai.sources.apif_ingest import COUNT_KEYS, import_apif_games

    def clock(value):
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.utcoffset() is None:
            raise ValueError("importer replay time must include timezone")
        return instant

    if not section.get("replay_options"):
        raise ValueError(
            "recorded importer replay_options missing; historical options cannot be inferred"
        )
    options = document(root, section["replay_options"])
    invocation = document(root, options["invocation_evidence"])
    expected = dict(
        run_id=report.get("run_id"),
        provider_code=code,
        season=scope["season"],
        source_sha256=scope["raw"]["sha256"],
    )
    if not expected["run_id"] or any(options.get(k) != v for k, v in expected.items()):
        raise ValueError("importer replay_options run/source/scope mismatch")
    for key in (*expected, "retain_results", "assume_known_after_seconds", "imported_utc"):
        if key not in options or invocation.get(key) != options[key]:
            raise ValueError("importer replay_options differ from recorded invocation: " + key)
    if (
        options["before_manifest_sha256"] != section["before_manifest"]["sha256"]
        or options["after_manifest_sha256"] != section["after_manifest"]["sha256"]
        or clock(options["cutoff_utc"]) != clock(facts["gate_cutoff"])
        or options["formal_aliases_sha256"] != sha256(FORMAL.read_bytes()).hexdigest()
        or type(options["retain_results"]) is not bool
    ):
        raise ValueError("importer replay_options export/cutoff/config mismatch")
    delay = options["assume_known_after_seconds"]
    if delay is not None and (type(delay) is not int or not 0 < delay <= 14 * 86400):
        raise ValueError("importer replay delay missing or invalid")
    originals = [
        p
        for r in facts["apif"]
        if r["competition_id"] == code and r["season"] == scope["season"]
        for p in r["provenance"]
        if p["sha256"] == options["source_sha256"]
    ]
    if not originals:
        raise ValueError("importer replay original missing")
    original = originals[0]
    archive = Path(facts["archive_root"])
    raw = proof(archive, original)
    meta = document(
        archive, dict(path=original["path"] + ".meta.json", sha256=original["metadata_sha256"])
    )
    seen, imported = clock(meta["fetched_at_utc"]), clock(options["imported_utc"])
    if not (
        seen <= imported <= clock(after["manifest"]["created_utc"]) <= clock(facts["gate_cutoff"])
        and clock(before["manifest"]["created_utc"]) <= imported
    ):
        raise ValueError("importer replay imported_utc contradicts capture/export/cutoff")
    parser = ApiFootball(leagues={code: facts["configs"][code]})
    try:
        games = parser.parse(raw, league_id=code, season=scope["season"])
        if parser.rejected or len(games) != scope["parsed_matches"]:
            raise ValueError("importer replay parser differs from report")
    finally:
        parser.session.close()

    with restored_memory(before) as (engine, db):
        with Session(engine) as session:
            result = import_apif_games(
                session,
                games,
                seen=seen,
                assume=timedelta(seconds=delay) if delay else None,
                aliases=json.loads(FORMAL.read_bytes()),
                retain_results=options["retain_results"],
                imported_utc=imported,
            )
            session.commit()
        for key in COUNT_KEYS:
            if scope[key] != result[key]:
                raise ValueError("current " + key + " counts differ from actual importer replay")
        with restored_memory(after) as (_, expected_db):
            for name in (
                "teams",
                "matches",
                "results",
                "result_source_links",
                "result_source_identities",
            ):
                columns = [c.name for c in Base.metadata.tables[name].columns]

                def rows(connection, columns=columns, name=name):
                    return [
                        dict(zip(columns, row, strict=True))
                        for row in connection.execute(f"SELECT {','.join(columns)} FROM {name}")
                    ]

                if normalized_rows(name, rows(db)) != normalized_rows(name, rows(expected_db)):
                    raise ValueError("actual importer replay differs from exported " + name)
    return dict(
        counts={k: result[k] for k in COUNT_KEYS},
        replay_options_sha256=section["replay_options"]["sha256"],
        invocation_sha256=options["invocation_evidence"]["sha256"],
        retain_results=options["retain_results"],
        imported_utc=options["imported_utc"],
    )


def normalized_rows(name, rows):
    from zucai.models import Base

    columns = Base.metadata.tables[name].columns

    def value(column, item):
        if item is None:
            return None
        if str(column.type) == "DATETIME":
            # SQLite exports omit UTC offsets; stored database timestamps are UTC.
            instant = datetime.fromisoformat(str(item).replace("Z", "+00:00"))
            if instant.tzinfo is not None:
                instant = instant.astimezone(UTC)
            return instant.replace(tzinfo=None).isoformat()
        if str(column.type) == "JSON" and isinstance(item, str):
            return json.loads(item)
        return item

    return sorted(
        json.dumps(
            {c.name: value(c, row[c.name]) for c in columns}, sort_keys=True, ensure_ascii=False
        )
        for row in rows
    )


def recent_importer(facts, code, section, root, originals, reported):
    """Re-execute daily parsing/importing; declared risk labels cannot suppress rows."""
    from sqlalchemy.orm import Session

    from zucai.identity_binding_archive import FORMAL
    from zucai.models import Base
    from zucai.sources.api_football import ApiFootball
    from zucai.sources.apif_ingest import import_apif_games

    options = document(root, section["replay_options"])
    before, after = [export(root, section[k]) for k in ("before_manifest", "after_manifest")]
    if (
        options.get("before_manifest_sha256") != section["before_manifest"]["sha256"]
        or options.get("after_manifest_sha256") != section["after_manifest"]["sha256"]
        or options.get("formal_aliases_sha256") != sha256(FORMAL.read_bytes()).hexdigest()
        or options.get("provider_code") != code
        or type(options.get("retain_results")) is not bool
    ):
        raise ValueError("recent importer options scope/config/export mismatch")
    delay = options.get("assume_known_after_seconds")
    if delay is not None and (type(delay) is not int or not 0 < delay <= 14 * 86400):
        raise ValueError("recent importer delay invalid")

    def clock(value):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value.utcoffset() is None:
            raise ValueError("recent importer clock needs timezone")
        return value.astimezone(UTC)

    imported = clock(options["imported_utc"])
    if (
        not clock(before["manifest"]["created_utc"])
        <= imported
        <= clock(after["manifest"]["created_utc"])
        <= clock(facts["gate_cutoff"])
    ):
        raise ValueError("recent importer/export clock contradicts cutoff")
    pending, actual = {}, Counter()
    with restored_memory(before) as (engine, db):
        for reference in originals:
            raw = proof(root, reference)
            path = root / reference["path"]
            meta = json.loads(path.with_suffix(path.suffix + ".meta.json").read_bytes())
            seen = clock(meta["fetched_at_utc"])
            if seen > imported:
                raise ValueError("recent importer precedes original capture")
            parser = ApiFootball(leagues={code: facts["configs"][code]})
            try:
                games = [
                    g for g in parser.parse(raw) if g.league == facts["configs"][code]["league"]
                ]
                for r in parser.rejected:
                    pending["apif:" + str(r["provider_match_id"])] = "rejected"
            finally:
                parser.session.close()
            with Session(engine) as session:
                result = import_apif_games(
                    session,
                    games,
                    seen=seen,
                    assume=timedelta(seconds=delay) if delay else None,
                    aliases=json.loads(FORMAL.read_bytes()),
                    retain_results=options["retain_results"],
                    imported_utc=imported,
                )
                session.commit()
            for key, value in result.items():
                if type(value) is int:
                    actual[key] += value
            for key, label in [
                ("held", "held_unbound"),
                ("suspected", "suspected"),
                ("rejected", "rejected"),
            ]:
                for row in result[key]:
                    pending["apif:" + str(row["provider_match_id"])] = label
        claims = {r["fixture_id"]: r["outcome"] for r in reported}
        for mid, label in claims.items():
            if (label in {"held_unbound", "suspected", "rejected"}) != (mid in pending) or (
                mid in pending and pending[mid] != label
            ):
                raise ValueError("recent risk outcome differs from actual importer: " + mid)
        if not set(pending) <= set(claims):
            raise ValueError("recent importer risks missing from report")
        with restored_memory(after) as (_, expected_db):
            for name in (
                "teams",
                "matches",
                "results",
                "result_source_links",
                "result_source_identities",
            ):
                columns = [c.name for c in Base.metadata.tables[name].columns]

                def rows(connection, columns=columns, name=name):
                    return [
                        dict(zip(columns, r, strict=True))
                        for r in connection.execute(f"SELECT {','.join(columns)} FROM {name}")
                    ]

                if normalized_rows(name, rows(db)) != normalized_rows(name, rows(expected_db)):
                    raise ValueError("recent actual importer differs from exported " + name)
    return dict(counts=dict(actual), pending=pending, imported_utc=options["imported_utc"])
