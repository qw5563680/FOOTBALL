# T63 internal follow-up: test adaptation and European retained-result boundary

Reviewer: internal delegated Codex review, not Claude approval.
Repository: `/workspace/work/football-t63`.
Scope: the two follow-ups requested after the independent 511-test review.
The earlier review and its test record are unchanged. No product source, original
archive, production database, frozen prediction, or ledger was edited. No real
Log Loss/return comparison or sports HTTP request was performed.

## 1. Three old test expectations

Reviewed the diff in `tests/test_model_evaluation.py` and
`tests/test_train_output_t16.py` against the explanation in
`IMPLEMENTATION-NOTES.md`.

- The two generic historical-readiness tests move their synthetic league and
  parameter dictionary key from 英超 to the non-T63 winter-season league 苏超.
  They retain the positive control and every previous temporal, incompatible
  half-life, different-corpus, replay, and expired-season assertion. This keeps
  testing the unchanged generic gate rather than treating a single-FD fixture
  without the full approved method definition as the seven-league legacy policy.
- The training-provenance test again expects the restored baseline English
  entry's evaluation database hash. It still checks that this hash differs from
  the actual training database and agrees across the output/model provenance.
  It adds a negative readiness assertion for the deliberately FD-only model.
  The new shape assertion is string length 64; the existing equality assertions
  bind it to the actual checked-in baseline value (it is not a standalone hex
  validator).
- The T63-specific tests separately cover the fixed full definition, changed
  methods and manual overrides, expanded-source ineligibility, and no automatic
  expiry of the unchanged old definition. These were in the independent 511-test
  run before this narrow follow-up.

Conclusion: no protection was weakened by these three expectation changes.
The implementer reports the two entire affected files pass; this follow-up did
not redundantly rerun the previous 511-test suite.

## 2. Confirmed must-fix: retain import blocks the legacy European reader

The independent reproduction uses `data/fixtures/t10/synthetic_ingest.json`
through the existing `captured_game`, `SAMPLE`, and `SEEN` helpers in
`tests/test_t63_apif_retention.py`. Only the synthetic game's league/code and
alias league key are changed to 英超 / 39. Two separate in-memory databases cover
an existing FD result and an existing JC result.

For each case:

1. Insert the fixture's canonical teams and one finished incumbent match with
   the same IDs, kickoff and 2:1 score. Its assumed availability is kickoff + 3h.
2. Default `read_history(session, '欧战', 'apif,fd,fdorg,jc',
   training_leagues=['英超'])` succeeds and returns only the incumbent.
3. Call the real `import_apif_games` with `retain_results=True` and
   `seen=imported_utc=SEEN`, using the fixture's numeric bindings.
4. The import inserts the APIF result and a valid timestamped ResultSourceLink;
   neither match has a native counterpart_id.
5. The same default European reader now raises a cross-source-overlap error.
   As a diagnostic only, the explicit cutoff policy can select the incumbent.

Observed stdout (exit 0; the expected reader failures were caught):

```text
source fd before ['fd:retained'] inserted 1 counterparts [None, None] verified_links [('apif:91001', 'fd:retained', '2026-10-12T10:00:00.000000Z')]
legacy_after_error 同联赛三小时内存在跨来源重复或待核对的身份重叠：apif:91001 / fd:retained
cutoff_policy_after ['fd:retained']
source jc before ['jc:retained'] inserted 1 counterparts [None, None] verified_links [('apif:91001', 'jc:retained', '2026-10-12T10:00:00.000000Z')]
legacy_after_error 同联赛三小时内存在跨来源重复或待核对的身份重叠：apif:91001 / jc:retained
cutoff_policy_after ['jc:retained']
```

Cause: the retained APIF branch in `sources/apif_ingest.py` records the new
ResultSourceLink without rewriting native counterpart fields. The legacy branch
of `predict/backtest.py` calls `legacy_linked_representatives`; that helper only
reads native counterpart_id and therefore ignores this valid new relation.

This is a delivery blocker: the fixed old archive's numerical-invariance check
cannot exercise the first subsequent retained APIF import, and the intended daily
flow can consequently prevent the European model from reading its corpus.

Minimum safe direction communicated to root and implementer: consume currently
valid explicit ResultSourceLink evidence within the legacy materialized view,
preserving its existing source priority and old semantics when no new links are
present. Do not switch the whole European model to the expanded cutoff policy,
infer fuzzy links, overwrite incumbent score/availability/counterpart fields, or
disable the overlap guard. Cover paid-first / incumbent-first arrival, conflicting
scores retaining the existing higher-priority row, and missing/stale/ambiguous
proof still being rejected. Native legacy behavior and the independent baseline
comparison must remain unchanged.

Status at this record: independently reproduced and reported to root and
`/root/t63_implementation`; product fix and independent closure are pending.
