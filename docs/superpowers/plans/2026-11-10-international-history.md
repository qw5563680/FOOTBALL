# T51 Implementation Plan

Goal: Store three verified historical reference matches while retaining sixteen existing results unchanged.
Architecture: Explicit reviewed batch + read-only pinned inputs → validated immutable directory; standalone reader, no database/model integration.
Tech Stack: Python standard library, pytest, existing nations registry.
Spec: docs/superpowers/specs/2026-11-10-international-history-design.md

## Global Constraints

No production writes, network calls or credentials in the importer/tests. No odds/model/ledger changes. Do not infer 90-minute results from final scores or goal-minute heuristics. Historical availability remains unknown. Only the reviewed 19-match batch is in scope; a new batch requires review.

## Task 1 — Verified supplement and readback

Interfaces: build_supplement(plan_path, source_path, archive_path, registry_path, output_path) → counts; read_supplement(output_path) → supplemental records.

- [x] Write offline fixtures and failing behavior tests for three additions, preservation, replay and rejection of unsafe inputs.
- [x] Implement minimum validation, whole-batch staging, immutable output, checksummed readback and standalone command.
- [x] Run targeted tests, then complete pytest suite; lint and format. Commit implementation and documentation together after acceptance.

## Task 2 — Actual fixed-batch acceptance and draft delivery

Interfaces: Task 1 outputs are read back and copied into an isolated acceptance archive.

- [x] Freeze exact production/source inputs and official evidence; run first/second import; compare original file hashes.
- [x] Zip/export and restore; validate equality; save actual counts, time, commands, logs, input/output hashes.
- [x] Document usage, sources, scope and limits; reports contains README and SHA256 only.
- [x] Review whole change in a fresh context; fix important findings with regression checks.
- [ ] Commit to independent task branch and create draft PR; no merge or production run.

## Review Focus

False identity matches (reversed teams, duplicate dates, alternate IDs), incomplete month archive, accidental use of 120-minute score as 90, misinterpreted summer time, forged historical availability, input/output path aliasing or symlinks, replay changing old files, malformed or tampered archive accepted by the reader. Confirm no importer is wired into scheduled production or model training.
