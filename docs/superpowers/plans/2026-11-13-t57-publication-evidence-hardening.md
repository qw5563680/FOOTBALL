# T57 Publication Evidence Hardening Implementation Plan

> Execute inline with superpowers:executing-plans; use test-driven-development and final independent review.

**Goal:** Fail visibly when required Git publication history is unavailable; persist verified historical evidence for safe reuse.
**Architecture:** Share explicit Git infrastructure checks; keep per-file rejection semantics. Read-only resolvers collect new evidence for the scoreboard/digest output transaction. Separate forecast/SFC history state follows feed export/restore.
**Tech Stack:** Existing Python, local Git, JSON and pytest; no new dependencies.
**Spec:** docs/tasks/T57-publication-evidence-hardening.md

## Global Constraints
No sports requests, credentials, production writes, model/parameter/BET or frozen-record changes. Existing post-push receipts win. Branch and draft PR only.

## Review Focus
- All receipts present must work without Git; incomplete/invalid cache must not silently zero outputs.
- Per-file hash mismatch stays unknown with healthy Git and cannot replace the first saved entry.
- SFC errors after forecast resolution must not leave partially saved cache/output.
- Export from a stale working database must preserve newer archive history entries.
- Restore of an old snapshot must remove stale destination history; hash failures must reject restore.

### Task 1: Failure semantics and persistent evidence
- [x] Add real local-Git regressions for absent repository, shallow history, missing ref/remote and commands without partial output; see RED.
- [x] Add `publication_history.py` for error/check/cache primitives; refactor forecast/SFC lookup to use them.
- [x] Collect first historical successes without overwriting existing entries; validate raw bytes on every read. Preserve read-only lookup API.
- [x] Publish collected state in existing scoreboard/digest rollback transaction; regression checks output/cache rollback and idempotency.

### Task 2: Feed state and documentation
- [x] Whitelist history state for feeds, merge append-only entries on export, remove stale state on restore. Test hash-checked roundtrip.
- [x] First unlisted history state does not rewrite feed_manifest; next regular export enrolls it. If already enrolled, update its hash atomically when adding entries. This preserves acceptance 4's first-run input bytes.
- [x] Document format, validation, failure reasons and enrollment; no workflow changes.

### Task 3: Real snapshot acceptance and delivery
- [x] Fixed b492f4 archive, same clock: compare baseline/new outputs and all input hashes, fault both commands before/after cache, tamper cache, per-batch unknown, restore and idempotency.
- [x] Run full pytest, ruff check and format; final independent review. Put only README/SHA256 under reports; detailed evidence in separate data branch.
- [x] Commit/push T57 and create draft PR, no merge.
