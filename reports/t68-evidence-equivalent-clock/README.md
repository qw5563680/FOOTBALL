# T68 第八节第 10 项：近 30 天证据重放观察时间

分支 `deepseek/T68-evidence-equivalent-clock`，基线 `8c948e0f`；按所有者本轮补丁要求实现，非草稿 PR，正常运行 CI。

## 问题与修复

审查者在数据分支 `3d9740f` 的沙职副本上定位：294 行仅 `result_checked_utc` 与检查端重放不同。
本轮以此定位为依据；没有重新下载或重放整份生产归档，不把 294 行写成开发环境的实跑数量。

检查端重放使用各原件 sidecar 的 `fetched_at_utc` 作为 `seen`。
生成端 equivalent 模式错误地使用执行截点，dates 模式错误地沿用整季原件的抓取时刻。
`src/zucai/identity_gate_evidence_gen.py` 两处改为读取本份原件的元数据：

- equivalent：所选整季原件自己的抓取时间；
- dates：循环内每份日期原件自己的抓取时间。

`imported_utc` 继续使用执行截点。原件内容、真实采集时刻、导入器行为、检查端规则均不改。
README、ARCHITECTURE、DATA_SOURCES 同步，无数据库、命令行接口或工作流变更。

## 回归测试

在 `tests/test_t68_evidence_generation.py` 新增两条经 `cloud_feeds identity-evidence` 完整入口的测试：

- `test_cloud_equivalent_replay_uses_original_capture_before_cutoff`；
- `test_cloud_dates_replay_uses_each_original_capture_before_cutoff`。

两条复用既有离线 fixtures。合成上下文另加一场没有竞彩对应行的 APIF 比赛，
保证真实导入器会保存比赛，避免只有“链接到已有竞彩”而不写时间字段、无法检出问题。
执行截点为 `2026-11-14T16:30:00Z`（北京时间 11 月 15 日 00:30）；
整季原件捕获为 `16:20:00Z`，日期原件分别在 `16:05:00Z` 至 `16:14:00Z`，均不同于截点。
这些时刻仅是明确标注的合成测试条件，不是新取得的真实数据。

两条测试断言：最近 30 天证据不在缺项中、模式正确、检查端实际导入器新增 1 场，
且实际导入时间仍是原截点。没有替换检查端或导入器，没有只断言文件存在。

修复前，只添加两条测试、保留主干业务代码：
`2 failed, 11 deselected in 16.21s`，两条都报
`recent actual importer differs from exported matches`。

修复后：`2 passed, 11 deselected in 15.18s`。

复跑命令：
`PYTHONPATH=src OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 pytest -q -o addopts= tests/test_t68_evidence_generation.py -k original_capture_before_cutoff`

完整证据生成测试文件：`13 passed in 61.26s`。
最终全量：`4703 passed, 10329 warnings in 585.63s (0:09:45)`，退出码 0。
命令：`PYTHONPATH=src OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 pytest -q -o addopts= -o tmp_path_retention_policy=failed --basetemp=work/t68-evidence-clock/pytest`。
`ruff check src tests examples` 通过；`ruff format --check src tests` 为 `428 files already formatted`。
`git diff --check` 通过；执行时的源码/测试哈希逐项相同，保护文件与基线逐字一致。
既有 penaltyblog / NumPy 弃用警告保留，未隐藏。

全量使用 pytest 内置 `tmp_path_retention_policy=failed` 控制临时目录，单线程 BLAS 控制资源；
不更改测试收集、断言或跳过条件。日志、源码哈希与 UTC/北京时间保留在工作区 `work/t68-evidence-clock/`。

## 交回边界

只做离线测试，不手动触发生产工作流，不使用正式 key，不请求体育接口，不推生产数据。
没有修改 `identity_gate_evidence.py` 或 `identity_daily_gate.py`，检查规则与基线逐字一致。
无需重跑既有模型、覆盖旧预测或改写账本。保持 PR 未合并，交审查者复审。
