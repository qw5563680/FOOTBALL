# T52 dead bridge mapping implementation plan

**Goal:** Restore strictly verified national fixtures when an archived bridge names a missing match, and describe missing predictions accurately.

**Spec:** `docs/tasks/T52-dead-bridge-national-mapping.md` (owner authorized implementation and draft PR).

**Architecture:** Reuse existing national team IDs and exact kickoff checks. A bridge to an existing match remains authoritative and conflict checked. Preserve the original bridge and explicit fallback reason in new mapping evidence; never rewrite frozen files.

**Constraints:** No model/parameter/BET changes, fuzzy matching, new sports requests, or production publication. Use existing Python/SQLite interfaces and real archived fixtures.

## Work and verification

- [x] Add failing core tests for absent-target fallback, existing wrong-target conflict, invalid provider IDs, kickoff mismatch, and repeat refresh; minimally change `sources/sfc.py`; rerun tests.
- [x] Add failing digest tests distinguishing empty days from unmatched/unpredicted fixtures, including partial coverage and frozen historical context; minimally change digest rendering; rerun tests.
- [x] Pin a real data archive commit and crop 26163 fixtures with provenance. Reproduce old 8/14 mapping and replay patched 14/14; require six exact APIF IDs in the November 13 prediction batch.
- [x] Compare the eight unaffected match identities and actual model probabilities byte for byte. Inject a bridge to an existing wrong match and require a conflict. Hash frozen input files before/after replay.
- [x] Explain why sale eligibility and bridge catalog availability can diverge using archived collection evidence. Record all five acceptance results in `reports/t52-dead-bridge/README.md` with hashes.
- [x] Run full pytest, ruff check and format check; obtain independent code review. Delivery is a commit on the requested branch and a `T52:` draft PR, with no merge or production action.

## Review focus

Existing wrong target, mismatching source team IDs, reversed teams or kickoff, ambiguous national candidates, and frozen report compatibility must remain fail closed.

## Execution record

- Base: `41395e5` (main); no competing T52 branch/PR found.
- Worktree: `/workspace/work/football-t52`; evidence logs: `/workspace/work/t52-evidence`.
- Core, digest and real-fixture acceptance are delegated with disjoint file ownership; final integration and checks remain with the parent.

- Final checks: 2,582 tests passed; ruff check and format check passed (284 files).
- Actual archive replay: base 8/14 → patched 14/14; six November 13 predictions; original eight probability bytes and all 2,177 archive file hashes unchanged.
- Independent read-only review and separate full-archive replay passed; both identified regressions were fixed before final validation.
- Detailed results: `reports/t52-dead-bridge/README.md`; large outputs remain in the isolated evidence directory and local handoff bundle.
