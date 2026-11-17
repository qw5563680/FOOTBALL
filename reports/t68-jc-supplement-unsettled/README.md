# T68 第八节第 12 项：未结算补充页补丁

任务依据：所有者本轮 A/B/C 修复要求；基线 `fa3394ed`（#180 已合并）。
分支 `deepseek/T68-jc-supplement-unsettled`，提交与非草稿 PR 正常运行 CI，不手动触发生产工作流。

## 根因与真实样本

运行 [44135483201](https://github.com/qw5563680/FOOTBALL/actions/runs/44135483201) 的官方日页含两条尚未结算的比赛。
固定数据归档 [3d9740f 原页](https://github.com/qw5563680/FOOTBALL/blob/3d9740f/state/catalogue/supplement/jc/2026-11/2026-11-17-p1.json)，
`jc:2042830`、`jc:2042829` 的 `matchResultStatus="1"`、比分为空；
`SportteryResults._row` 正确返回 `None`，读取器却直接访问 `game.play_date`。
采集和历史跳过逻辑只确认分页完整，尚未确认整天是否已结算。

原页 SHA256：`faf28eedb55b21ec01a1a842c4954570ce8c0dfb339bdfc42db39cb21246ebd2`；
真实捕获 UTC `2026-11-17T17:06:30.803269Z`，北京时间 2026-11-18 01:06:30。
`data/fixtures/t68-supplement/unsettled-2026-11-17.json` 保留原前三行（两条未结算、一条已结算），
行值不改；字典和总数按裁剪调整，不冒充完整原件。来源见相邻 `unsettled-provenance.json`。
成功重取用的状态/比分变体、集成测试用的临时赛事路线均为明确标注的合成上下文；
不声称两场比赛已经取得真实最终比分，不把测试副本当作生产验收。

## 修改

- A：`identity_archive_evidence.py` 遇空赛果记录问题并跳过该证据；`identity_binding_archive.py` 保留未核实行、令 `complete=False`，不能作为完整见证。
  近 30 天检查对被查赛事的未结算行明确报“该日官方页面仍有未结算比赛”，证据不完整；其他赛事的未结算行不误阻断。
- B：`cloud_jc_catalogue.py` 在整天分页校验后检查每条结算结果；未结算日不发布新页面，摘要“还差”包括日期、原因和比赛 ID。
- C：已有未结算日不再跳过。新日全部结算后，先将旧页及 sidecar 原样留到
  `state/catalogue/supplement/superseded/<run_id>/jc/<年月>/`，再替换当前页；失败时保留旧页。
  留底同路径已有不同内容时拒绝覆盖，要求新运行编号。
- `cloud_feeds.py` 仅扩充留底文件路径白名单，保证导出、恢复都保留它；身份证据读取与开启检查扫描不读取留底。
- README、ARCHITECTURE、DATA_SOURCES 同步，无数据库结构、模型或工作流变更。
- 既有 `day-*` 合成分页样本补齐结算字段，原分页/请求上限断言保持；真实未结算样本另外保存。

## 实跑证据

测试文件 `tests/test_t68_supplement_unsettled.py` 新增 9 个用例：

| 项目 | 入口与断言 |
| --- | --- |
| 原异常复现 | 在修复后的测试上下文临时恢复旧读取器，`cloud_feeds identity-evidence` 实际报 `AttributeError: 'NoneType' object has no attribute 'play_date'`；随后恢复修复文件 |
| A：两个读取路径 | `cloud_feeds identity-evidence` 分别从补充页和普查页读入真实未结算行，不崩溃，不把它们列为完整已核实证据 |
| A：被查赛事阻断 | 完整 `cloud_feeds identity-evidence`，使用测试专用赛事配置接入真实国际赛行，持久化 `generation_errors.recent_replay` 明确指出 11-17 未结算，`recent_replay` 留为缺项 |
| A：范围 | 同一页中非被查赛事的未结算行不阻断；被查赛事不能得到完整近 30 天分母 |
| B：新日未结算 | `cloud_feeds probe --probe-kind jc-supplement`，只请求一次，不发布该日，摘要“还差”明确原因 |
| C：旧日仍未结算 | 同一采集入口确实重取，旧页及原 sidecar 逐字不变 |
| C：旧日变为已结算 | 同一采集入口重取并替换；旧页与 sidecar 逐字留底；`restore_feeds` 后留底仍一致；再次执行采集入口请求数为 0 |
| C：留底隔离 | `cloud_feeds identity-evidence` 能恢复并读取带留底的归档，但留底不参与当前身份证据 |

针对性命令：
`PYTHONPATH=src OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 pytest -q -o addopts= -o tmp_path_retention_policy=failed tests/test_t68_supplement_unsettled.py tests/test_t68_jc_supplement.py`

结果：`17 passed in 26.34s`。

最终全量结果：`4701 passed, 10329 warnings in 584.21s (0:09:44)`，退出码 0。
命令：`PYTHONPATH=src OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 pytest -q -o addopts= -o tmp_path_retention_policy=failed --basetemp=work/t68-unsettled/pytest-release`。
最终新增测试单独复跑 `9 passed in 22.27s`，包含读取器进入关联前的未核实状态断言。
`ruff check src tests examples`：`All checks passed!`；`ruff format --check src tests`：`428 files already formatted`。
`git diff --check` 通过；最终执行记录中源码、测试和样本哈希逐项相同。
既有 penaltyblog / NumPy 弃用警告未隐藏。第一次全量为补强断言主动中止，日志留存；以上为随后完整重跑结果。

测试沿用会话级 `responses` 禁止真实 HTTP。全量运行使用 pytest 内置
`tmp_path_retention_policy=failed` 和单线程 BLAS，控制临时目录及资源占用，不改变收集、断言或跳过条件。
完整离线日志及开始时刻（UTC、北京时间）、源码哈希保留在工作区 `work/t68-unsettled/`。
此前扩展回归的四个失败来自旧合成页缺结算状态/无效比分；样本补齐后针对性全部通过，失败日志保留。

## 交回边界

本轮不取新体育数据、不使用正式 key、不手动触发生产工作流，不修改正式归档、比分、球队绑定、预测、参数或账本。
无需重跑模型；正常 CI 及审查者复审完成后，由所有者决定合并与生产验收。
真实未结算日何时变为全部结算，本轮没有联网确认，不补比分、不推测结算时间。
