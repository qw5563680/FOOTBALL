# T53 Unknown Team Prior Disclosure Implementation Plan

> For agentic workers: use the existing task specification, isolated worktree and tests-first workflow. Parent integrates and verifies the entire branch.

**Goal:** Disclose which side uses default strength, retain research probabilities, exclude affected matches from main selections, and stratify review results.

**Architecture:** One read-only disclosure helper consumes frozen prediction metadata. New predictions add explicit side flags without changing model arithmetic. Report/selection/scoreboard consumers reuse that evidence; old frozen files remain untouched.

**Tech Stack:** Existing Python/SQLite, pytest and Ruff; no dependencies or API requests.

**Spec:** `docs/tasks/T53-unknown-team-prior-disclosure.md`, explicitly authorized for implementation and a draft PR.

## Global constraints

- Base `eb335c7`, branch `codex/T53-unknown-team-prior-disclosure`, worktree `/workspace/work/football-t53`.
- No prior formula, model parameter, market-input, BET, training or production changes.
- Preserve frozen predictions and ledger bytes. Old missing metadata is unrecorded, not proof both teams are known.
- Real fixtures come from immutable archive `1b8cc9c697b863d4c92b4e0006f2190d053e1fcb`; explicitly distinguish synthetic both-unknown input.
- Evidence directory `/workspace/work/t53-evidence`; reports only README/SHA256 lists under 200 KB.

## Shared interface

`zucai.prediction_disclosure` provides:

- `prior_disclosure(row) -> dict`: `used` (true/false/null), `home` and `away` (unknown-side true/false/null), `method` (str/null), `evidence` (str). Reads raw `metadata`, or a propagated `prior_disclosure` field on derived rows.
- `uses_unknown_team_prior(row) -> bool`: true when archived evidence establishes default strength; absent legacy metadata alone does not change candidate eligibility.
- `prior_label(row) -> str`: Chinese side/team/method disclosure for affected rows; empty for established known or unrecorded rows. Flag-only legacy cases explicitly say the side was not recorded.
- New model metadata uses `home_unknown_team` and `away_unknown_team`, alongside unchanged `unknown_team_prior` and `prior_method`.
- Old pooled metadata may identify sides only when both original `home_training_leagues` / `away_training_leagues` dictionaries are present and consistent. No model pickle loading in report rendering.

## Work and verification

- [x] Core disclosure: add failing tests for historical away-unknown, home-unknown, known, both-unknown and flag-only legacy; implement helper and metadata-only model change; annotate single-match report without changing probabilities. Owner: core agent.
- [x] SFC: disclose in 14-match and pick-nine views, filter affected rows out of confidence order and model single selection, retain research probabilities and diagnostic reasons; handle existing frozen SFC reports through embedded original batch. Owner: SFC agent.
- [x] Parlay: disclose research rows and exclude default-strength matches from main groups/highest rankings, including either leg; preserve market comparison as separately labelled and old frozen ledger bytes. Audit older rankers in scope. Owner: parlay agent.
- [x] Digest/scoreboard: annotate frozen forecasts and review, show known/default/unrecorded strata, join legacy score rows to exact frozen forecast evidence in memory rather than rewriting stored rows. Owner: digest/scoreboard agent.
- [x] Archive acceptance: crop true historical rows with hashes; run real model metadata side checks and before/after known-team probability/eligibility comparisons; hash all original forecasts and ledgers before/after. Owner: evidence agent.
- [x] Integrate, full pytest/Ruff, independent read-only review, final report and SHA256 list. Owner: parent. Delivery is the requested branch and a `T53:` draft PR, with no merge or production action.

## Review focus

- Unknown away/home/both and incomplete legacy side evidence must be distinguished correctly.
- Missing metadata cannot silently certify a known-team cohort; malformed evidence must not bypass an affirmative prior flag.
- Neither single nor double leg, pick-nine rank nor highest-confidence order may admit an affected team-model row.
- Known-team probabilities and pre-existing candidate eligibility stay unchanged; market probability never decides prior use.
- Old scored records and SFC/parlay reports require read-only disclosure from exact original frozen evidence, with no historical rewriting or retrospective ledger re-selection.

## Execution results

- Final full suite: 2,641 passed; Ruff check and format check passed (290 files).
- Real model baseline/patched comparison: exact probability bytes for all three real inputs; both-unknown is explicitly synthetic input through the real archived model.
- Candidate control: six known matches/three groups unchanged; actual SFC sequences 1 and 7 excluded from confidence/pick-nine eligibility with frozen research probabilities retained.
- Corrected actual production cohort: 4 default, 92 known, 0 unrecorded; 55 experimental score rows excluded and original active keys used.
- All 2,177 input files and restored database SHA256 unchanged. Independent reviewer repeated final-v2 acceptance.
- Earlier mixed-cohort acceptance summaries are superseded; use acceptance-final-v2 and reviewer-final-v2 only.
- Detailed evidence: reports/t53-unknown-team-prior/README.md; full intermediates stay in isolated work storage.
