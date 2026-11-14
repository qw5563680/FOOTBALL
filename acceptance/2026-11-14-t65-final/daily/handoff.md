# T65 daily isolation handoff

Implemented in `/workspace/work/football-t65`, uncommitted. No commit, push, merge,
sports HTTP request, formal credential use, production write, or real backfill.

Changed files owned by this subtask:

- `data/league_map.json`: exactly 62 approved routes change `daily: true` to `false`;
  `backfill: true` and all identity evidence remain unchanged.
- `src/zucai/league_registry.py`: `daily_provider_config(provider, *, path=REGISTRY_PATH)`
  returns the approved daily configs and preserves APIF's existing special Nations date route.
  Existing `provider_config`, `provider_ids`, identity/index and backfill semantics are unchanged.
- `src/zucai/sources/apif_command.py`: default date imports use the common daily config;
  explicit historical seasons retain the original approved config.
- `src/zucai/cloud_aliases.py`: daily APIF candidates parse using the same daily config.
  The existing fail-closed rule for rejected rows inside the selected scope is unchanged.
- `tests/test_t65_daily_scope.py`: six focused cases over synthetic T14-derived payloads.
- `tests/test_t61_registry.py`: the prior assumption that every approved import route
  is daily is replaced by the newly approved split between daily and historical routes.
- `tests/test_t61_provider_integration.py`: the test injects the real
  `daily_provider_config` through a temporary registry path; it still checks that
  disabled daily imports are filtered and explicit `leagues_path` dictionaries are preserved.
- `tests/test_t65_reviewed_routes.py`: the reviewed-route contract now requires no
  new daily routes and all 62 historical routes, while retaining access/candidate checks.

`cloud_apis.collect_apis` already calls `fetch_apif(date=...)` and `write_candidates`.
It has no other fixture parser: its direct `ApiFootball` instance only refreshes status.
No change to cloud status, FD/FDORG, probe, identity or statistics consumers was necessary.

## Verification

- Red: new focused suite produced four expected failures and two passes (`red.log`).
  Before the fix, the daily list had 62 extra routes, mixed raw inserted 63 instead of 1,
  valid new rows expanded candidates from 2 to 4, and an AWD row suppressed old candidates to 0.
- Green: all six focused cases passed (`green.log`).
- Domain regression: 151 tests passed, exit 0 (`domain-tests.log`). Files:
  `test_t65_daily_scope.py`, `test_t61_registry.py`, `test_t65_registry_mappings.py`,
  `test_apif_command.py`, `test_cloud_aliases.py`, `test_cloud_apis.py`,
  `test_cloud_cups.py`, `test_apif_reconcile.py`.
- Targeted ruff check and format check passed for the five Python files.
- `git diff --check` passed.
- The root agent owns the combined full test suite, repository-wide ruff, docs and reports.

Follow-up after independent review: two previously omitted legacy test files exposed
an obsolete monkeypatch target and the old daily/backfill policy expectation.
Both were reproduced (`review-red.log`, two failures), then updated without changing
production code or weakening their explicit-config/access/candidate boundaries.
The expanded ten-file suite passed **172 tests in 17.46 seconds**, exit 0
(`extended-domain-tests.log`); ruff and format checks for both files passed
(`review-ruff.log`). `review-followup.json` records this final verification and
`code-files.json` fixes the eight owned files' SHA256 values. The domain code is frozen.

Commands used the agreed interpreter: `PYTHONPATH=src /workspace/work/t48-venv/bin/python`.
Pytest included `-q` in addition to the repository's quiet default, so the domain log
shows 72 + 72 + 7 passing dots rather than a separate numeric summary.

## Independent acceptance

`verify_daily_isolation.py` extracts only baseline source and small config files from
`b83d2cc7d975e41f418f59aad3341a7d1574f95a` into a temporary directory, runs separate
old/current processes, blocks every HTTP request, and removes the temporary source afterward.
It does not copy a large database or archived sports corpus.

The mixed raw contains one original J1 row, 62 synthetic FT rows (one per newly approved
route) and one new league 79 AWD row. It is explicitly synthetic, derived from
`data/fixtures/t14/apif.json`, and is not an API access or season-availability claim.
Input provenance and exact raw SHA256 are in `inputs/provenance.json`.

`summary.json` and `acceptance.log` record:

- 11 ordinary daily APIF routes and 73 historical routes, including all 62 additions.
- The five retained candidates 40/62/71/850/916 stay disabled.
- Exactly 62 daily flag edits; unchanged old 46 provider objects, old 21 mapping rows,
  and all 90 official catalogue objects.
- For identical mixed raw bytes: all 14 database tables' canonical snapshots, complete
  import result, audit CSV bytes and candidate JSON bytes equal old main.
- With no official seed, the original J1 fixture inserts once, rejected/suspected = 0.
- With the synthetic official seed, the original unbound J1 fixture remains the same
  pre-existing suspected duplicate (1); it still produces exactly two J1 candidates.
  The new AWD does not add a rejection or suppress either candidate.
- Clean versus mixed raw has identical database and import audit bytes. Candidate
  decisions match in the pytest case while each input keeps its genuine raw SHA256.
  Do not claim candidate provenance bytes are equal between different raw inputs.

Suggested documentation wording:

> T65 新核准的 62 个 API 赛事仅开放历史回填（`backfill=true`、`daily=false`）。
> 日期导入与候选别名生成共用每日范围，保持原 11 条普通 APIF 路由和既有欧国联特殊日期行为。
> 每日采集由 T68 逐赛事开启；本次未执行真实回填，也没有核验新赛事的赛季读取权限。

> 同一份含新增赛事 FT/AWD 行的离线合成日期原件，在原 main 与当前代码下得到相同的
> 数据库逻辑内容、导入结果、审计 CSV 和候选 JSON。不同原件之间保留各自真实 SHA256；
> 业务结果的等价比较不抹除原件证据差异。

The earlier per-route review evidence is retained as historical review provenance;
the owner-merged PR 136 / revised task IV.4 supersedes its earlier daily/backfill policy sentence.
