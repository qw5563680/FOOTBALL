# T63 internal SFC definition follow-up: pending finding

Internal delegated Codex review, not Claude approval. This record is separate
from the closed European retained-result review.

The authorized adjustment is to retain SFC diagnostic probabilities for the
eight expanded experimental primary versions, keep the seven expanded candidate
sidecars isolated, preserve the seven fixed legacy primary definitions, and
leave A–F paper eligibility unchanged.

Thirty-three independent checks through the real `_validate_batch` and
`build_sfc_report` entry point passed after the first SFC scope patch. These use
temporary databases built from the offline T07 fourteen-match fixture. They cover
all eight primary leagues, unchanged probabilities/combination for old and
non-T63 rows, the true old marker-free format, all seven expanded candidates with
and without a parent definition marker, explicit candidate parent fields, bad
parent marker types, every missing frozen-definition field on a row or model,
fully stripped new records, a valid row definition inconsistent with its model,
and a forged expanded ready status. A–F qualification predicates remain false
for the eight expanded primary rows.

## Must-fix found after those checks

`src/zucai/report/sfc.py`, `_sfc_definition_allowed`, lines 84–92 at this review,
only checks that a seven-league row has `source_variant='legacy'`. That marker
describes the FD/JC source set, not the full fixed legacy method definition.

Independent reproduction through the real validator and report builder:

1. Construct a valid fourteen-match English league freeze with the fixed FD/JC
   definition, complete row/model metadata, and `model_definition_schema=1`.
2. Change the definition selection protocol to `explicit_manual_half_life`,
   `manual_override`, `unknown`; recalculate its canonical SHA-256 ID. Apply the
   complete same definition and a distinct fitted version to every row/model.
3. Mark the model truthfully as `paper_eligible=False`, `p2_ready=False`, label
   `实验`. The full definition still passes validation and has source variant
   `legacy`, but its ID differs from the fixed approved legacy definition.
4. Build the SFC report. All fourteen probabilities and the combination are
   produced, so an unapproved method version can replace the seven-league old
   primary definition.

Observed output, exit 0:

```json
{
  "league": "英超",
  "source_variant": "legacy",
  "fixed_legacy_definition": false,
  "paper_eligible": false,
  "selection_basis": "explicit_manual_half_life",
  "rows_admitted_to_sfc": 14,
  "combo_created": true
}
```

The minimum correction communicated to root, implementer, and the owning helper
is to retain the fixed legacy definition and eligibility requirement for the
seven old leagues. Only the eight primary experimental versions and unchanged
non-T63 diagnostic routes should receive the SFC diagnostic exception. No shared
A–F eligibility function needs to change.

Status: independently reproduced and reported; correction and independent
closure pending. No source or production/archive data was changed by this
review, and no sports HTTP or real loss/return experiment was performed.
