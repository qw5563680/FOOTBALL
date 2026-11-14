# T63 versioned follow-up evidence

All files in this directory concern the PR136 scope revision. The earlier
`/workspace/work/t63-evidence` remains unchanged. These are offline fixture and
implementation checks, not new real-data accuracy experiments or Claude approval.

## Complete synthetic archives

`source_version_artifacts/` preserves complete outputs from this follow-up's
full-suite fixture execution (`/tmp/pytest-of-agent/pytest-377`). These inputs use
`data/fixtures/t16/train_history.csv` and `data/fixtures/t17/upcoming.json`, with
explicit synthetic league mutations in the tests.

- `success/`: main old-definition forecasts plus complete expanded-source
  candidate models, parameters, training JSON and prediction documents; two runs
  retain their separate immutable files.
- `candidate_failed/`: the old primary model remains available while the failed
  candidate freezes reasons and exclusions without claiming a model artifact.
- `expanded_primary/`: an original experimental league uses expanded sources in
  its main forecast and remains ineligible for paper accounts.

`collect_fixture_artifacts.py` copied only these small synthetic archives.
`verification.json` records successful `verify_forecasts` checks before and
after copying; `SHA256SUMS` covers copied files. No formal archive was copied or
mutated. This is not a real HTTP replay or evidence of improved predictions.

## First full-suite failures and test adaptation

`full-suite.red.{log,xml}`: 3,579 tests; 3 failures, 0 errors, 0 skipped;
162.220 seconds. All three failures were old expectations inconsistent with the
owner's revised scope. Product source stayed frozen during their adaptation.

1. `tests/test_model_evaluation.py::test_readiness_cannot_relabel_old_or_incompatible_model`
   expected `p2_ready is True` for an English-league fixture with only `fd` and no
   fixed old-definition selection protocol. A single-source fixture cannot be
   treated as the approved `fd,jc` definition. The generic historical-gate fixture
   now uses non-T63 winter-season league `苏超`; its original date, half-life and
   no-retuning assertions remain unchanged.
2. `tests/test_model_evaluation.py::test_readiness_allows_other_corpus_but_rejects_replay_and_expired_seasons`
   likewise expected the incomplete English fixture to be ready and then to
   expire automatically at the next annual gate boundary. The generic fixture
   now uses `苏超`; all its original corpus, replay and expired-season assertions
   remain. T63's seven fixed old definitions are separately tested in
   `test_t63_model_definitions.py`, including no automatic expiry, full definition
   matching and changed-method/CLI-override rejection.
3. `tests/test_train_output_t16.py::test_train_model_and_output_record_training_and_evaluation_database_hashes`
   asserted `evaluation is None`, an expectation introduced by T63 phase 1.
   PR136 restores the complete old English parameter entry, including its old
   evaluation database hash. The test now checks a retained 64-character hash,
   its distinction from this training database, and equality of all recorded
   training/evaluation provenance fields. An additional assertion requires this
   deliberately `fd`-only fitted model to remain unready, preventing it from
   borrowing the approved `fd,jc` definition.

The root requested independent review of these fixture changes. The follow-up
full-suite log/XML use `full-suite.{log,xml}`; the first run is never overwritten.

## Targeted red/green trail

- `cloud-before-wiring.log` -> `cloud-integration.log`: complete source-candidate
  attachment added to the actual cloud orchestration.
- `candidate-boundary-before.log` -> `pipeline-integration.log`: preserve SFC
  target scope; a post-fit training-report write failure must remove model-path
  and model-hash claims; late generation retains the original cutoff and excludes
  its probabilities.
- `candidate-scope-before.log` -> `candidate-scope-after.log` and
  `archive-after-scope.log`: partial configuration may freeze candidates for its
  configured old-league subset; every configured old league must have an outcome.
- `paper-parent-marker-*`, `paper-candidate-parent-*`, `paper-sfc-label-*`:
  immutable new-format context, candidate exclusion and accurate frozen labels.
- `paper-reference-identity-{red,green,regression}.*`: all three existing technical
  experiments and the market reference retain their own identity instead of
  inheriting the main Dixon definition; probabilities and independent voting
  behavior remain equal to the old fixture output.
- The final manual-calibration patch's 6-red/171-green output was not saved to a
  standalone log. Session tool records are red chunk `8ca859`, green chunk
  `a7552e`, and Ruff chunk `496b9c`. Do not mistake the earlier
  `model-definitions-pytest.txt` (208 tests before that patch) for its final check.
  The final combined internal review and full-suite include the manual override
  tests, including equal-value overrides and ordinary annual stability.

`quality-checks.json` records actual Ruff, format and diff-check tool results.
The final completion note supplies exact full-suite counts from its XML.
