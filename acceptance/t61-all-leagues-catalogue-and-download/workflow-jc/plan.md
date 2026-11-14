T61 workflow/JC implementation plan

1. Add failing archive tests for exact safe catalogue paths, hashes, symlink rejection, and restoring an older snapshot without retaining later catalogue evidence.
2. Add failing offline JC tests for the previous 12 complete Beijing months, strict pagination/date/identity validation, raw/sidecar preservation, exact 90-row master-name cross-check, and read-only collection.
3. Add failing CLI/workflow routing tests; preserve ten inputs and all old operation routes. Isolate hosted catalogue and self-hosted jc-catalogue, route catalogue-aware backfill arguments.
4. Implement the smallest production changes for these tests, then run relevant old archive, probe, workflow, and source regressions. Root runs combined final suite and documentation integration.

Owned files: cloud_feeds.py, cloud_jc_catalogue.py, football-feeds.yml, tests/test_t61_{workflow,jc_catalogue,catalogue_archive}.py, data/fixtures/t61/jc*. Other agents own all other source changes.
JC path: state/catalogue/<run_id>/jc/<YYYY-MM>/<YYYY-MM-DD>-p<N>.json and .json.meta.json; root reports jc.json and summary.json. Only league identity/counts are parsed; no score parsing, header requests, or DB writes.
