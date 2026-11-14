# T63 internal follow-up closure: European retained results

Date: 2026-11-14 UTC. Reviewer: internal delegated Codex review, not Claude
approval. Repository: `/workspace/work/football-t63`.

This is a new closure record. The original pending reproduction remains intact
in `2026-11-14-internal-followup-test-adaptation-eu-retention.md`, and the earlier
511-test review is not reused as evidence for this later fix.

## Conclusion

The independently reproduced European retained-result blocker is closed by the
narrow change in `predict/backtest.py` and `predict/pooled.py`. No further
must-fix was found in this narrow review. This conclusion does not claim formal
Claude approval or completion of root's separate real fixed-snapshot numerical
comparison.

The reader first retains its existing legacy materialization. Only a related,
currently valid ResultSourceLink proof component enables the timed selection
path. An active new proof may reconnect a validated native counterpart edge
whose old representative was removed; it cannot activate at the exact proof
cutoff or enable an unrelated timeless edge. The legacy source priority and
score/availability policy remain in force. Missing or stale proof still reaches
the overlap guard.

## Independent synthetic checks

Nine separately constructed temporary-database scenarios passed using the real
APIF importer or reconciliation entry point. They use the existing synthetic
T10 capture and canonical bindings, not production data.

1. Existing FD followed by retained APIF, equal score: the FD representative is
   retained both exactly at import time and one second later.
2. Existing JC followed by retained APIF, equal score: the JC representative is
   retained at the same two boundaries.
3. Existing FD followed by retained APIF, conflicting score: the incumbent FD
   score remains selected.
4. Existing JC followed by retained APIF, conflicting score: the incumbent JC
   score remains selected.
5. APIF first, FD arriving later, followed by the real `reconcile_source_results`:
   APIF is selected before FD availability; overlap remains rejected after both
   are available but before proof, and at the exact proof timestamp; FD wins one
   second after proof.
6. The same arrival/reconciliation boundaries with JC instead of FD.
7. A native JC-to-FD relationship plus only a new APIF-to-FD timed proof: the
   already removed FD row remains a valid anchor and JC is selected after proof.
8. Deleting the new proof in a temporary database: the loader returns no proof
   and the overlapping European rows are rejected.
9. Moving the paid kickoff after proof in a temporary database: the stale proof
   is excluded and the overlapping European rows are rejected.

The incumbent-first scenarios additionally checked that the incumbent score,
availability timestamp/source, and native counterpart field were unchanged, and
that the retained APIF counterpart remained unset. The three-source scenario
checked that the existing native anchor remained unchanged. All 9 checks passed
in one process, exit 0.

## Targeted test verification

The following independent command completed with exit 0 and 67 passing tests:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  /workspace/work/t48-venv/bin/python -m pytest -q \
  tests/test_t63_european_retention.py \
  tests/test_t63_legacy_versions.py \
  tests/test_t63_apif_retention.py \
  tests/test_t63_source_latency.py \
  tests/test_pooled_pipeline.py
```

Only the known penaltyblog/NumPy deprecation warning was reported. The separate
three adapted historical test cases in `test_model_evaluation.py` and
`test_train_output_t16.py` also passed independently, exit 0. Their previously
recorded review conclusion is unchanged: moving generic readiness fixtures to
non-T63 winter-season 苏超 preserves the original gate assertions; the restored
evaluation-hash expectation retains provenance equality and adds a negative
FD-only readiness assertion. These changes do not weaken the new/old version
eligibility protections.

Reviewed source SHA-256 values:

```text
ed107e4d18f092ce651cd11fa59752ca1c718c26f43cd9fcc0547fd06e7fa49f  src/zucai/predict/backtest.py
ac0f26f2bd5ab066d621e1600b6572b5590d58d05a52cfe15a268ad089a859ca  src/zucai/predict/pooled.py
2e5cd693196f751de91b5a912d30f5274960f988ae82901491b4ad470c12195d  tests/test_t63_european_retention.py
```

## Limits and separate follow-up

No product source, production database, original archive, frozen prediction, or
ledger was changed by this review. No sports HTTP or real Log Loss/return
experiment was run. Temporary synthetic databases and this new evidence file
are the only review writes. Root separately owns the refreshed 39-fit / 105-
probability baseline comparison.

The newly authorized SFC scope adjustment (retain diagnostic probabilities for
the eight experimental primary versions while keeping the seven candidate
sidecars isolated) is a separate review and does not change this EU closure.
