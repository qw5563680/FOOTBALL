# T68 scoped history and canonical identity approval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Complete the four owner-requested follow-ups without publishing data or enabling collection.
**Architecture:** Keep the legacy reconciliation path unchanged; explicitly select scoped approved new routes for batch identity projection and unique JC links. Create canonical identities only from reviewed owner decisions anchored to authenticated numeric JC evidence; retain all duplicate/conflict checks. Missing season is a blocked gate result, never inferred.
**Tech Stack:** Python, SQLAlchemy/SQLite, existing archive readers, pytest.
**Spec:** docs/tasks/T68-new-competition-team-identity.md section 7; docs/reviews/2026-11-14-t68-review.md final section; docs/reviews/2026-11-15-t68-owner-approval.md; owner's four-item follow-up.

## Global Constraints
- Existing 18 league aliases, production data, frozen predictions, scores, ledgers and collection switches remain unchanged.
- No sports HTTP requests or production keys; all executions use archived originals or offline fixtures.
- No automatic creation approvals, fuzzy identity matching, score-based identity choice or backdated timestamps.
- Evidence: reports/t68-scoped-history-and-identity-approval/README.md; detailed records on a dedicated evidence branch. Draft T68 PR, no merge.

## Review Focus
- Two provisional opponents require simultaneous identity projection; processing order must not change results.
- Partial scope/time approvals must not move other competitions' history.
- Already-claimed, reversed or multiple JC candidates must stay blocked and preserve old links.
- Repeated creation and creation after another writer changed identity inventory must not duplicate teams.
- Missing season must remain blocked without breaking a multi-competition register or inventing a current denominator.

### Task 1: Gate missing season
Files: src/zucai/identity_daily_gate.py; tests/test_t68_missing_season.py.
- [x] Real archive entry regression: remove season from a registered route; expect blocked reason and no exception; keep another valid route readable.
- [x] Run RED; implement explicit preflight blocked result; run GREEN.
- [x] Run all 87 actual verified import routes and list every missing-season route.

### Task 2: Scoped historical consolidation
Files: src/zucai/sources/apif_scoped_reconcile.py; tests/test_t68_scoped_reconcile.py; README.md.
- [x] Reproduce two provisional opponents with a canonical JC match; test exact identity, repeat, foreign league, conflicting claims and rollback.
- [x] Implement explicit offline selected-route entry; validate formal approvals before batch projection; reuse strict counterpart checks. Keep legacy entry unchanged.
- [x] Replay actual Saudi/World Cup archive in isolated copy; report changed IDs, remaining gaps, unchanged protected rows and export/restore.

### Task 3: Canonical identity approval entry
Files: src/zucai/identity_canonical.py; src/zucai/identity_binding_archive.py; tests/test_t68_canonical_approval.py; README.md; docs/ARCHITECTURE.md.
- [x] Reject proposed/unreviewed decisions, stale numeric evidence, name/alias collisions and category conflicts; repeat approved decision creates zero duplicates.
- [x] Validate against archive originals and live DB identity inventory; transactionally create only explicitly approved numeric identities; persist identity and approval provenance through existing teams export.
- [x] Test subsequent archive reader recognizes created identity, roundtrip and immutable match/result preservation. Real 257 IDs remain proposals, no invented owner approvals.

### Task 4: Explain denominators and deliver
Files: reports/t68-scoped-history-and-identity-approval/README.md; docs/DATA_SOURCES.md.
- [x] Re-run pinned 8b900f4 with/without FD auxiliary originals and list each additional verified relation, raw file/hash and command.
- [x] Full pytest, Ruff check/format checks; inspect diff and publish draft PR with limitations and evidence links.
