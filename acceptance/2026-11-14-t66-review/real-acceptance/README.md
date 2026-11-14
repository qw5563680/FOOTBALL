# T66 real failed-run offline acceptance

`acceptance.py` replays the 15 nonempty HTTP-200 CSVs from GitHub Actions
`43543648051`, artifact `apis-source-43543648051-1`, SHA256
`ab410c4abdacfffc914d8833419a31c1d2ebbb22c4836ba79cbf61ae84da37e5`.
The other 15 CSV files are zero-byte redirect responses and are recorded but
excluded. All source and sidecar hashes are captured in each run's `inputs.json`.
Keep `source-manifest.json` beside the script: it locks the original 30 CSV
names, byte counts, raw/sidecar SHA256 values and capture metadata, so another
set of files cannot accidentally receive this artifact's label.

The source archive is the existing readonly checkout of data commit `e5848e5`.
Replay always calls the actual `restore_feeds`, `fetch_fd`, `export_feeds` and
`restore_feeds` functions. League-season CSVs use season `2627`; country files use
`from_season=to_season=2026`; known-after remains the explicit existing 3-hour
assumption. No original capture timestamp is changed. HTTP and socket entry
points fail if invoked; normal runs record zero attempts.

`2026-11-14T06:30:00+00:00` is a **synthetic regression clock**, later than every
saved input capture. This evidence is not a claim about historical prediction,
publication, live collection, or production deployment.

Run commands from any directory, substituting the four absolute input paths.
The output directory must be new. Do not point `--out` into the original archive.

```bash
PYTHON=/path/to/project-venv/bin/python
RUNNER=/path/to/real-acceptance/acceptance.py
CODE=/path/to/checked-source
ARCHIVE=/path/to/e5848e5
RAW=/path/to/artifact/raw/football-data-co-uk/2026-11-14
OUT=/path/to/new-acceptance-output

"$PYTHON" "$RUNNER" replay --code "$CODE" --code-label source-label \
  --archive "$ARCHIVE" --raw "$RAW" --out "$OUT" --keep-work

"$PYTHON" "$RUNNER" train --code "$CODE" --code-label source-label --out "$OUT"
```

For a baseline, add `--baseline-only` and omit `--keep-work`: the first replay evidence persists,
but its owned regenerable database and copied runtime state are cleaned up.
This skips repeated import, export/restore and model fitting for the baseline.
The generated export directory is always removed after successful round-trip
verification. No original archive tree is copied. Training runs only against
the retained output database and saves models under `OUT/training`.
`--only 欧战` selects one model; `--resume` reuses already written training
reports. Use `--resume` only for the same source, inputs and regression cutoff.

Training calls the unmodified `zucai.predict.command.train` entry point with the
same configuration and arguments as `cloud_predict`: all 19 configured models,
their existing explicit sources, fixed as-of, the last 56 days for smoke
backtesting, and the original `data/model/params.json`. The runner verifies the
database SHA256 after every fit. It never writes a forecast or production model.

The final run also verifies the normal merge of main/T65 into this T66 work.
The 15-file import/repeat/initial export-restore uses `code-fixed`; its source
hashes remain in `fixed/inputs.json`. `code-merged` separately captures the
frozen merge result, with its parents recorded in `code-merged-origin.json`.
After replay, run these dependent checks before training the merged code:

```bash
"$PYTHON" "$RUNNER" checkpoint --code /path/to/code-fixed \
  --code-label review-fixed-final --out "$OUT"
"$PYTHON" "$RUNNER" validate-final --code /path/to/code-merged \
  --code-label merged-source-label --out "$OUT"
"$PYTHON" "$RUNNER" train --code /path/to/code-merged \
  --code-label merged-source-label --out "$OUT"
```

The checkpoint records all 19 full-history ID hashes, eligible training IDs,
source counts and training scopes, plus the actual 15-league FD configuration.
The merged source must match those values, pass its own complete DB validation,
and export/restore every table without changing the hashes.
`final-source-validation.json` records the separate import/final source hashes;
only the merged snapshot runs the final 19 fits.

Evidence files include all database-table counts and hashes before, after,
after repeated import, and after export/restore; per-file import outputs; old
match and result changes; the 52 existing proof targets' pointer comparison;
APIF/FD candidate and actual link lists; model corpus/eligible training IDs
before and after; exact added/removed training IDs; frozen artifact hash lists;
and full model training reports. The existing 37,617 result rows must remain
identical, the 52 proof rows must remain identical, repeated import must add
zero rows and leave database tables unchanged, and export/restore must preserve
every table hash.

`code-f3bc574` was extracted with `git archive f3bc574 src data`; it is the
immutable source/configuration snapshot for the failing baseline. The working
branch's later changes cannot change that run's imported modules.
The first baseline run had already entered the unnecessary repeat phase when
that check was dropped: it was interrupted, the incomplete transaction rolled
back, and `finish-baseline` verified that every table still equaled the completed
first-import snapshot before retaining the evidence and cleaning its work DB.
The intentional interrupt remains visible in `baseline.log`; it is not the
failure used to establish the regression.

Fixtures in the code branch are exact cropped raw data, not generated responses:
`fd-fin-oulu-kups.csv` uses original FIN physical line 2719, and
`fd-swe-sirius-halmstad.csv` uses SWE physical line 3601. Both preserve the
original header/BOM/CRLF and their sidecars' URL/status/capture time, with only
the cropped content SHA256 updated. `fd-apif-replay-seed.json` contains verbatim
archived table rows and original column order; its APIF rows are database
evidence, not a claimed raw API response. Complete provenance is in
`data/fixtures/t66/fd-apif-real-provenance.json`.
