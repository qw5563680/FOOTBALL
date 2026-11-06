# T48 FD statistics linkage implementation plan

> For agentic workers: execute TDD tasks with independent core/archive owners, followed by a fresh whole-branch review.

Goal: restore valid archived FD observations without changing canonical matches or historic model outputs.
Architecture: legacy fd/counterpart resolution stays supported in history and daily modes. An additive fd_stats_links index holds exact alias/identity/raw provenance for additional JC attachments; no replacement of counterpart_id. Optional hash-listed index JSONL is exported/restored separately from legacy technical row shards.
Tech stack: existing Python/SQLAlchemy/SQLite/pytest; no new dependency.
Spec: docs/tasks/T48-fd-stats-linkage.md (the user's explicit implementation and acceptance request).

## Global constraints
No sports network requests/production execution/data pushes/merge. Preserve N=10, timestamps, backfilled/null available, models/parameters/P2/BET, matches/counterparts/frozen predictions/accounts. Old v1/v2/v3 archives remain valid.

## Review focus
- Same-provider ID reused for a different match or duplicate reverse/team/time candidates must reject.
- Proposed aliases and malformed/missing evidence cannot authorize linkage.
- A new index with missing/altered hash-listed bytes fails restoration atomically.
- Index survives both feeds index restoration and Release row restoration.
- Replay of previously rejected raw archives must actually run once and become idempotent.

## Task 1: core linkage and ingestion
Files: models.py, stats_schema.py, stats_storage.py, new fd_stats_links.py, tests/test_fd_stats_links.py.
Interface: FdStatsLink model; FD_LINK_FILE, FD_LINK_COLUMNS and validate_fd_links(sqlite3.Connection); resolve_fd_match(session, game, league, metadata, aliases) -> Match or ValueError. ingest_fd adds optional aliases_path and supports known JC counterpart in history mode.
- [x] Write offline regression tests for absorbed fd counterpart, retained fdorg/empty pointer exact linkage, invalid aliases/reverse/duplicate/time, conflict evidence, idempotency and transaction rollback; observe expected failures.
- [x] Implement additive identity evidence and writer authorization; existing pointer paths remain compatible.
- [x] Run focused tests and commit core work.

## Task 2: archive persistence and guards
Files: new fd_stats_archive.py, cloud_feeds.py, stats_archive.py, tests/test_fd_stats_archive.py.
Consumes Task1 model/constants/validator; new table is outside STATS_COLUMNS/ROW_TABLES.
- [x] Write failing tests for feeds+row shards roundtrip, older archive without index, missing/tampered proof and wrong team/pointer; observe failures.
- [x] Export optional state/fd_stats_links.jsonl with normal feed hashes; restore before validation; check_stats authorizes only validated additional links.
- [x] Run focused archive and legacy stats tests and commit.

## Task 3: bounded offline replay and real acceptance
Files: src/zucai/fd_stats_replay.py, CLI if appropriate, examples/t48_replay.py, README/ARCHITECTURE, reports/2026-11-07-t48-fd-stats-linkage/README.md and SHA256SUMS.
- [x] Define replay plan from verified original file hashes, locations, seasons and capture sidecars; require explicit paths, no downloads or cursor mutation.
- [x] Observe regression tests for replay/idempotency and invalid plan before implementation; preserve clocks and identities.
- [x] Restore fixed original feeds and technical shards/receipts to isolated input. Replay the nine real CSVs through production ingest, inspect all210 historical pairs,21target cells and remaining5.
- [x] Repeat replay (0new), export with local-only Release storage, restore fresh database, compare links/rows/features plus immutable matches/results/odds and frozen/account artifacts.
- [x] Record exact hashes, commands, counts and limits. Run full pytest, ruff check/format, workflow check if changed. Fresh whole-branch review; address important findings.

## Decisions
User already specified implementation, offline verification and draft-PR delivery. Routine schema/interface choices are implementation decisions within that authorization; no extra design approval pause. Superseded alternative: overwriting counterpart (forbidden). Keeping evidence only in CSV audit was rejected because writer/restore need a stable relational index. Separate index avoids changing legacy row shard formats.
