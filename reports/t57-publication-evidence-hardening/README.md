# T57 发布证据加固验收

本任务只改变发布证据读取、保存和失败边界。模型、参数、BET、冻结预测、原评分行、两本纸面账和既有回执不改写。

## 固定输入与复跑

- main 基线：`7dfaf41`（已含 T55/T56）。
- 固定数据提交：`b492f4d7c501fd8cdbd2694d760f2f13892f796c`，2369 个原文件。
- 仅在本地完整 Git 副本上运行；两版本使用相同原件、原记分板截止时间、执行时钟与 run ID。
- 执行时钟固定为 `2026-11-13T00:00:00+00:00`，只用于离线可重复比较，不回写任何采集时间。

```bash
PYTHONPATH=src python scripts/verify-t57-publication-history.py \
  --archive /path/to/local-full-history-b492-copy \
  --baseline-code /path/to/main-7dfaf41-code \
  --output /path/to/new-evidence-directory
PYTHONPATH=src pytest -q
ruff check src tests
ruff format --check src tests
```

[逐命令退出码、日志、基线/新输出、哈希及完整测试记录](https://github.com/qw5563680/FOOTBALL/tree/data/t57-publication-evidence-acceptance-20261113/acceptance/2026-11-13-t57)。
同目录 `implementation-version.json` 绑定代码提交；本目录 SHA256SUMS 的 `evidence/` 前缀指该目录，`repository/` 指代码文件。

## 实现边界

- 未覆盖历史记录遇到非仓库、浅克隆、缺 published_ref/远端配置或 Git 命令故障，抛明确错误；两个 CLI 非零退出。
- 已校验回执或落盘证据可完全绕开 Git。SFC 的确认版本同时覆盖其指向的原件，避免原件误触发历史查询。
- `forecast_history.json` / `sfc_history.json` 按路径只增不改；每次核对当前原件 SHA256、方法、commit/ref 和时间。
  坏槽位保持原样，Git 正常时只令对应文件未知；Git 不可用且有未覆盖记录时失败。
- 证据与记分板/日报在既有失败回滚事务中一起保存；已入 feeds 清单的状态追加时，同时更新清单哈希，
  报告使用更新后的哈希，重复运行不会因自己的写入产生额外版本。
- 首次落盘不改 feed_manifest；下一次普通 export 才纳入 files 清单。restore 只恢复已入清单并校验通过的状态。
  这是为同时满足任务“首次运行其余输入不变”和“feeds 导出恢复可验证”；未入清单不冒充已归档状态。

## 独立复核

独立代码复核发现的“有效 SFC 回执仍依赖 Git”和“已入清单追加后报告记录旧哈希”均已用失败回归复现后修复。
另实测 SFC 故障发生在预测证据已排队之后，归档字节仍完全不变。实现没有调用体育接口或生产运行。

## 五条验收结果（最终代码实跑）

| 验收 | 结果与证据 |
| --- | --- |
| 1. 没有落盘证据的三类故障 | 移走 `.git`、浅克隆、缺 published_ref × 两命令，共 6 次非零退出；错误点名原因，全部原文件字节不变。 |
| 2. 保存证据后再故障 | 保存 198 条预测/附件证据、2 条 SFC 历史证据，其余 SFC 由已校验回执覆盖；三类故障 × 两命令共 6 次成功，输出与正常 Git 对照逐字节一致。坏 candidate_sha256 × 同样 6 次均失败且不写文件。 |
| 3. 个别未知保持旧规则 | 仅将 Nov3 `41509202754-1-2026-11-03` 批次设为未知，main 与新代码输出完全一致：publication_unknown=6，样本 96→94；账本原文不变。 |
| 4. 正常路径与幂等 | 正常样本 n=96，全部报告与 main 同次运行逐字节一致。首轮只新增两份 state/publication 文件和正常报告输出；重复时证据文件不变。原 151 条评分字节完整保留，按既有 T55 追加 4 条，与 main 完全一致；A–E/F 旧账本字节不变。 |
| 5. 全量检查与恢复 | pytest 2810 项通过（0 失败/错误/跳过，151.47 秒）；Ruff 与格式检查通过（308 文件）。两份状态进入 feeds 哈希清单并恢复，无 Git 下解析一致；篡改状态被 restore 拒绝，目标库不变。 |

总计 30 次 CLI 检查，其中 12 次预期失败、18 次成功。原数据副本的 2369 文件保持原样。
重复生成时既有 `newly_scored` 从 4 变 0 会产生正常的日报版本变化；已逐字节核对 main 的相同重复运行，
没有把这种既有变化当作证据不幂等。此处“幂等”明确包括证据文件不变、相同输入稳定后输出不变。
