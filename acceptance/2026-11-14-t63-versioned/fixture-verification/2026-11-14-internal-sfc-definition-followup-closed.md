# T63 internal SFC scope and definition follow-up: closed

Date: 2026-11-14 UTC. Internal delegated Codex review, not Claude approval.
Repository: `/workspace/work/football-t63`.

The separately saved pending record,
`2026-11-14-internal-sfc-definition-followup-pending.md`, is preserved unchanged.
The European retained-result closure is independent and remains valid.

## Conclusion

No further must-fix was found in this narrow SFC review. The implementation now
matches root's authorized scope: the eight expanded experimental primary
versions retain SFC diagnostic probabilities, the seven expanded candidate
sidecars cannot replace the seven fixed legacy primary versions, and A–F paper
eligibility remains unchanged. Non-T63 diagnostic routes and genuine old
marker-free freezes remain compatible.

The finding in the pending record is closed. For a new seven-league freeze,
`_sfc_definition_allowed` now requires the legacy source variant, true eligibility,
and compatibility with the complete fixed legacy method definition. It validates
the definition/hash first. A legitimate but different FD/JC method definition no
longer qualifies merely because its source variant is `legacy`.

The parent marker still requires complete frozen definition fields. Every
prediction and model must independently validate and have exactly matching
definition fields; explicit batch-level candidate fields remain rejected by the
SFC primary-document boundary. No shared A–F qualification function was relaxed.

## Independent verification

The same fourteen-match temporary-database reproduction from the pending record
now produces zero admitted probabilities and no combination. This uses the real
`_validate_batch` and `build_sfc_report`, not a patched validator:

```json
{
  "league": "英超",
  "source_variant": "legacy",
  "fixed_legacy_definition": false,
  "paper_eligible": false,
  "selection_basis": "explicit_manual_half_life",
  "rows_admitted_to_sfc": 0,
  "combo_created": false
}
```

Thirty-three independent checks through the actual validator and report builder
were rerun against the final source and passed, exit 0:

- Genuine old marker-free freezes remain usable.
- All eight expanded primary leagues retain their exact probability triplets,
  diagnostic combination, experimental label, and false paper eligibility. The
  A–F qualification predicates still reject them.
- Fixed old primary rows and non-T63 欧国联/苏超 rows retain the same probability
  triplets and diagnostic combination as the original fixture.
- Each of the seven expanded candidates is rejected both with and without the
  parent definition marker.
- Each explicit candidate parent form (expanded variant, false eligibility, or
  both) is rejected, including otherwise valid eight-primary rows.
- Invalid parent marker types, each missing frozen-definition field on a row or
  model, and fully stripped new records are rejected.
- A standalone hash-valid row definition differing from its model is rejected;
  expanded rows cannot forge the old-ready label and eligibility.

The following additional targeted suite passed, exit 0:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  /workspace/work/t48-venv/bin/python -m pytest -q \
  tests/test_t63_paper_versions.py \
  tests/test_sfc_report.py \
  tests/test_nations_league.py \
  tests/test_scoreboard_paper.py \
  tests/test_parlay_paper.py \
  --junitxml=/workspace/work/t63-version-evidence/internal-sfc-scope-regression.xml
```

The parsed JUnit evidence reports **133 tests, 0 failures, 0 errors, 0 skipped**.
It includes the implementation's fixed-definition positive cases and same-value
manual override, changed-value override, changed prior, and lost-eligibility
negative regressions.

Reviewed SHA-256 values:

```text
95f5bb090d248e7c7feac01e8a58e31012704cc09e99b2a9360c5d609d6a5b35  src/zucai/report/sfc.py
8a9e56fe56ed7f2dff10e5d3461a2b86c40ac6a393ad366493eda0f4231a7794  src/zucai/scoreboard/paper.py
117334d8c49a351d53afd622d474e5781238a4170f2864cf8875e7e53ffabb70  src/zucai/parlay/paper.py
6a51443f9425fb9d16022e8ead176cd606b41454fbd532b68fad399cfe606d8a  tests/test_t63_paper_versions.py
```

## Limits

This is an internal review of the stated boundaries, not formal Claude approval
or a claim that root's final whole-repository suite or fixed-snapshot numerical
comparison has completed. The implementer was notified that the independently
reproduced blocker is closed before starting that final suite.

No product source, production database, original archive, frozen prediction, or
ledger was edited by the reviewer. Only temporary synthetic databases and new
review evidence were written. No sports HTTP or real Log Loss/return experiment
was performed. Full T67 approval policy remains outside this review's scope.
