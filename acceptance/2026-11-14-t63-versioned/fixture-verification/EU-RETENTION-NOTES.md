# Additional European-pool import compatibility repair

The previous complete suite is preserved as `full-suite.pre-eu.{log,xml}`:
3,579 tests passed. It did not cover retaining a new APIF score next to an
already-present European domestic FD/JC result.

The real offline T10 importer retained the paid score and a valid, timestamped
`ResultSourceLink`, while correctly leaving the old `counterpart_id` unchanged.
The restored European legacy reader only understood the latter field and
therefore rejected the new pair as an unlinked cross-source duplicate. This
was independently reproduced for both FD and JC before the repair.

The repair changes only `predict/backtest.py` and `predict/pooled.py`:

- Preserve the original European materialized counterpart selection first.
- Only complete, currently valid timed-link components touching that corpus
  enable the compatibility selector. Other leagues' components do not enter it.
- New links become effective strictly after `verified_utc`; a future or
  equal-cutoff proof never supplies a current relationship.
- The original validated counterpart edges can connect a new effective proof
  to the old representative, including an FD anchor already replaced by JC.
  They do not turn new relationships into timeless legacy links.
- Preserve the old European identity/assumed availability policy and source
  priority. Do not apply the expanded domestic identity exclusions to its old
  corpus. Keep incumbent scores and audit the independently retained score.
- A missing or invalidated link still cannot authorize deduplication. No
  database field, old result clock, collector, numerical parameter or solver
  was changed by this repair.

`tests/test_t63_european_retention.py` contains 8 synthetic cases. The first
execution had 7 failures and 1 pass (`european-retention.red.{log,xml}`); the
repair passed all 8 (`european-retention.green.{log,xml}`). The cases cover FD/JC
incumbents with equal or conflicting scores, exact-fit equality to the old
corpus, retained score audit, APIF-first arrivals and strict proof cutoffs,
native JC-to-FD anchoring, and invalidated proof snapshots.

`european-retention.regression.{log,xml}` records adjacent T63, pooled, legacy
and model-evaluation tests. The independent reviewer separately exercised the
actual daily reconciliation path in 9 scenarios, including missing proofs.
The root is producing a fresh 39-fit/105-prediction baseline comparison after
this repair; the previous real-data comparison is retained as an intermediate
record and is not relabeled as final.

Final full-suite output uses `full-suite.{log,xml}`. All earlier failing and
passing logs remain available. The synthetic archive examples in
`source_version_artifacts/` were generated before this repair; their cloud
candidate freezing code did not change, and they are explicitly fixture-only.
