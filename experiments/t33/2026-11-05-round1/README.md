# T33 round 1 — historical diagnostic experiments

Non-purchase research. BET disabled. No production adoption.

- `initial/`: immutable predictions from code a747b08 and feeds bcf1430, including fd nine leagues and the initial incomplete xG corpus. Preserve this failed/incomplete-corpus diagnostic.
- `xg-after-backfill/`: final xG four-league experiment using newly archived feeds da17cbc, the SAME frozen protocol and a747b08 code. Final xG tables use this directory only.
- `shared-input-hashes.json`: teams/matches/results/odds inputs are byte-identical across the two snapshots. fd inputs did not change.
- Original `SHA256SUMS` retains the command's original checklist. `BUNDLE_SHA256SUMS` additionally covers audit receipts, logs and subgroup comparisons.
- `group_comparisons.json` was added using reporting-only code baaee26; includes generator version/hash and original prediction hashes, without fitting or selection.
- `strict` excludes backfilled statistics; `backfill_replay` assumes historical kickoff+3h availability and is NOT proof of contemporaneous knowledge or prospective validation.

Human-readable complete tables: FOOTBALL code branch `codex/T33-stats-features-experiment`, `reports/2026-11-05-t33-experiment/README.md`.
