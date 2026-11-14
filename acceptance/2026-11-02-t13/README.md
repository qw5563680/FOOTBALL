# T13 full acceptance evidence

These files belong on `data/football-feeds`, outside the code PR. No database, model or new source response is included.
The input archive is pinned to `ddeb76ff75fb23da736745c354db946c24b87107`; its manifest and all eight files are copied under `t13-input/archive/` for standalone verification.

- `t13-acceptance/features.parquet`, `.metadata.json`, `.manifest.json`: original CLI export, `feature_version=t04-v2`.
- `t13-acceptance/acceptance-summary.json`: source provenance, exact coverage, row checks and stable-column comparisons.
- `t13-acceptance/verify_acceptance.py`: independent export/database verification, with `--repo-root`, `--input-dir`, `--artifact-dir` overrides.
- `t13-input/build_v1.py`: original builder from code baseline `d4acd27`.
- `t13-input/restore_and_compare.py`: restore pinned input with existing `restore_feeds`, run v1 and compare with frozen T04.
- `t13-input/restored-v1.parquet`, `.attrs.json`: comparison baseline on the same restored input.
- `t13-input/input-provenance.json`: original/restored input difference disclosure; database paths record the execution environment.
- `build_v2.py`: exact implementation used, including final documentation-only module docstring.
- `pytest.log`: complete 261-test result; NumPy 2.5 emits existing penaltyblog deprecation warnings.
- `SHA256SUMS`: hashes of all archived files except the checksum list itself.

From the code checkout with dev dependencies installed, copy `t13-input/` to `work/t13-input/` and `t13-acceptance/` to `work/t13-acceptance/`.
Run `python work/t13-input/restore_and_compare.py` to restore a fresh database (requires no preexisting `work/t13-input/restored/zucai.db`).
Then run `python work/t13-acceptance/verify_acceptance.py` to verify the preserved export, original T04 files and reconstructed DB.
The verifier also supports explicit directory overrides if the artifacts remain in this data worktree.

To regenerate v2 without overwriting preserved files:

```bash
python -m zucai.cli features build --db work/t13-input/restored/zucai.db --as-of 2026-11-01T16:25:56.611980Z --out work/t13-rerun/features.parquet
python work/t13-acceptance/verify_acceptance.py --artifact-dir work/t13-rerun
```

A regeneration changes generation timestamps and artifact hashes. Other 41 model values, existing HAD probabilities and evidence, rest IDs/intervals and HHAD selections must match.
All source result availability timestamps remain captured; no training or availability replay is performed.
