T61 workflow / JC catalogue handoff

Implemented owned paths:
- .github/workflows/football-feeds.yml
- src/zucai/cloud_feeds.py
- src/zucai/cloud_jc_catalogue.py
- tests/test_t61_workflow.py
- tests/test_t61_jc_catalogue.py
- tests/test_t61_catalogue_archive.py
- data/fixtures/t61/jc*.json and jc-README.md
- Root-approved minimal evaluator compatibility edits in tests/test_stats_workflow.py and tests/test_cloud_predict_workflow.py

Fresh targeted regression run: 348 passed (regressions-green.log); command used PYTHONPATH=src /workspace/work/t48-venv/bin/python -m pytest -q and covered existing cloud feeds, prediction workflow, stats workflow/probe, daily chain, candidate workflow, relink workflow, and all three owned T61 test modules.
Owned ruff check and ruff format --check: passed. PyYAML parsed workflow: 9 jobs. No actionlint executable available.
Full pytest started, then stopped at ~24% without observed failures at root request because root's canonical full-suite run was already active. full-suite.log is explicitly an incomplete/terminated duplicate, not passing full-suite evidence.

TDD evidence: archive-red.log/archive-green.log; jc-red.log/jc-green.log; workflow-red.log/workflow-green.log; pointers-backfill-red.log/combined-green.log; runtime-cleanup-red.log and final regressions-green.log. The initial combined and first regression logs preserve intermediate evaluator failures; final regressions-green.log is authoritative for the owned final code.

JC window: previous 12 complete Asia/Shanghai calendar months. Current run date 2026-11-14 produces 2025-11-01 through 2026-10-31. Queries use existing official result matchDate, which differs from master table sale-number day/position counts; report labels both bases. No score parsing, header requests, imports, or direct database writes. Requests counts logical fetch_raw page attempts; the existing transport can retry internally. Collector guard requires GitHub Actions workflow_dispatch and RUNNER_ENVIRONMENT=self-hosted; workflow labels are [self-hosted, football-local]. Existing page transport retains its one-second limiter.

Catalogue archives use a strict safe-path whitelist and SHA256 manifest. Raw backfill originals/checkpoint accepted. Runtime aliases are deliberately not exported, but restoring older snapshots removes all later regular files beneath owned state/catalogue, including these unmanifested aliases. Symlinks anywhere in owned subtree are rejected before DB/archive mutation.

No commits, pushes, Actions dispatches, or real HTTP performed.
