# T55：按赛前发布时间选版及 T52 附项

本次为固定原件的离线重放；未推送正式数据，未触发生产运行，未使用 API key。
代码基线 main `c1ace6f`；输入固定在
[`b492f4d7c501fd8cdbd2694d760f2f13892f796c`](https://github.com/qw5563680/FOOTBALL/tree/b492f4d7c501fd8cdbd2694d760f2f13892f796c)。
历史截止为 `2026-11-12T10:44:20.936794+00:00`，读取原记分板截止时间；实际重放时间记录在证据摘要。

完整证据保存在数据分支
[`data/t55-scoreboard-publication-acceptance-20261112 / acceptance/2026-11-12-t55`](https://github.com/qw5563680/FOOTBALL/tree/data/t55-scoreboard-publication-acceptance-20261112/acceptance/2026-11-12-t55)。
逐场结果、全部文件前后哈希、198 个正式/附件发布证明、RED/GREEN 日志均在该目录。
代码分支 reports 只保存本说明与 SHA256SUMS。复跑入口：
[`verify-t55-archive.py`](../../scripts/verify-t55-archive.py)、
[`verify-t55-t52-archive.py`](../../scripts/verify-t55-t52-archive.py)。

## 发布机制

新批次先带 `publication_required=true` 冻结，成功 push 后 fetch 并核对远程原件，才采样当前 UTC
记录 `published_utc` 与含路径、SHA256、commit/ref 的 receipt，再发布确认清单。
生成时间、本地归档时间、尚未推送的提交不能替代该确认；确认清单未发布时保持未知。
历史批次只读完整 Git 历史，采用第一次加入不可变文件的 committer 时间并验证精确 blob SHA256。
这是任务允许的历史回退，不声称历史 commit 时间就是 push 完成时间。
缺历史、浅克隆、坏 receipt、改写过的原件或推导失败均按未知排除。

正式复盘、实验同批配对、A–F 账户与日报采用相同门槛：发布时间严格早于开球且不晚于读取截点。
旧评分保留原字节；纸面账户只从统计视图剔除不合格冻结注单，F 同时剔除相应组及票据。
其余注单的原资金、注额、创建时间、结算和前瞻/回放标记原样保留；不从零重放，不生成或追加校正票据。
正常的新批次开票、现金约束和结算仍沿用原账本规则。
模型、参数、P2、BET 未改变。

## 主项验收

| 标准 | 实跑证据 |
| --- | --- |
| 1. 11-07 四场切回赛前已发布版本 | 4/4 从 `42380973719-1-2026-11-07` 切为 `42358846882-1-2026-11-07`；四场正式概率逐字节相同，命中仍 3/4 |
| 2. 生成早但发布晚、发布时间未知 | 构造记录均未评分，各计 `published_after_kickoff=1`、`publication_unknown=1`；端到端测试另验证旧评分/账户被排除而原评分/账本字节不变 |
| 3. 已在开球前发布的原选版不变 | 其余 92 条已评分记录完整规范 JSON 逐字节一致，无概率舍入或容差；共同 SHA256 为 `1bcd3fbcb20a96accde4de2b7cba863b589b3498f39ec327d77c66921a612bef` |
| 4. 原档案不变及检查 | 全部 2,369 个文件前后 SHA256 一致；恢复库 SHA256 亦不变。11-12 初版全量 pytest 2,753 项通过；本次审查修复检查见下文 |

被排除批次生成于 `2026-11-07T14:28:57.080535+00:00`，四场开球均为 `14:30:00Z`。
首次包含正式文件的提交为
[`efd14cb1bce11dae9bda7b0a35498f23fb2ab153`](https://github.com/qw5563680/FOOTBALL/commit/efd14cb1bce11dae9bda7b0a35498f23fb2ab153)，
committer 时间 `14:34:18Z`；完整候选路径、前版时间及对应 SHA256 见 `acceptance-final/four-comparison.json`。

| 比赛 | 常规时间结果 | 原命中 | 重选后命中 | 正式概率 |
| --- | --- | --- | --- | --- |
| `jc:2042592` 莱红牛—奥格斯堡 | 5:2 | 是 | 是 | 完全相同 |
| `jc:2042593` 帕德博恩—法兰克福 | 2:2 | 否 | 否 | 完全相同 |
| `jc:2042594` 多特蒙德—埃沃斯堡 | 2:1 | 是 | 是 | 完全相同 |
| `jc:2042595` 美因茨—拜仁 | 1:2 | 是 | 是 | 完全相同 |

实际重放选中 123 场，已评分 96 场；正式发布时间排除 4 条，未知 0 条，赛果未完整 27 场。
xG 同场同批共同样本实跑从 **31 变 35**，新样本分别为英超 7、西甲 9、意甲 10、德甲 9。
原有 A–E 账本 1,959 行均通过发布时间门槛，实际原账本没有需要剔除的票；
旧不合格账本筛选路径已按下文审查反例在真实档案副本复核，原件未被更改。

正式模型总体 n=96、LogLoss=0.9525824916462907、Brier=0.5467700162497802 完全不变。
改选也切换了四场的冻结 SP 版本，市场对照因此有预期的小幅变化：德甲市场 n=9，
LogLoss **0.6004019215 → 0.6002602730**、Brier **0.3100198273 → 0.3096142388**。
赔率只属于独立市场基准，没有进入模型；完整值见审查修复证据中的 `root-market-comparison.json`。

## PR #115 审查修复（2026-11-13）

本次只修复两条必须修：账户校正改为原冻结记录子集；日报只比较已评分键，在必要重算时保留
完整预测披露与零样本联赛。浅克隆/整体 Git 失败的行为和历史证据持久化建议未纳入此次范围。
新证据位于
[`acceptance/2026-11-13-t55-review`](https://github.com/qw5563680/FOOTBALL/tree/data/t55-scoreboard-publication-acceptance-20261112/acceptance/2026-11-13-t55-review)。
[`verify-t55-review-archive.py`](../../scripts/verify-t55-review-archive.py) 在完整本地 Git 历史上创建独立
`b492f4d` worktree，运行真实记分板和日报，保存证据后移除临时副本；不更改原数据分支或输入档案。

- 在真实档案副本中，仅把 `41509202754-1-2026-11-03` 设为发布时间未知：统计剔除原有 **33 张**注单，
  A–E 原账本仍为 **1,959 行、逐字节不变**，没有新增 194 条重放记录。其余原行与前瞻/回放标记完全相同。
  1.05/A 从 38 注变 36 注；回放票从 2 变 0 仅因为剔除的原票中有这两张，不给剩余票重新贴回放标记。
- F 对相应批次执行同样子集筛选；不合格组与所属两张票一并退出统计，其余组、票、batch 标记原样保留，
  原 F 账本 **123 行、逐字节不变**。新批次仍能按原账本现金约束正常开票，重复运行不重复新增。
- 无注单不合格时，真实记分板的 123 个有效键中有 96 个已评分、27 个待结；日报完整沿用原 probability，
  包括欧国联/欧战两个零样本联赛及默认实力分层 **4/92/0**，不因待结键重算。
  两条新增端到端测试经过真实归档校验与发布门槛，分别覆盖待结记录和实际评分排除后的分层/空联赛保留。
  测试仅观察实际渲染器接收到的 summary，不打桩发布或选版逻辑；原有展示专用夹具仍单独测试渲染。

修前、修后摘要和逐文件 SHA256 对照分别在 `baseline-reproduction/` 与 `acceptance-final/`；
原输入全部 **2,369 个文件**保持不变。独立复核另外运行完整数据副本，得到相同的33张剔除及原账字节不变结果，见 `independent/real-injection-result.json`。

## T52 附项验收

后到竞彩目标与原 APIF 比赛为同一明确 counterpart，且联赛、球队 ID、主客方向、开球时间均一致时，
期次未重新入库也保持 matched。正向、反向 counterpart 测试通过；未关联、主客颠倒及错误目标仍拒绝。
固定 `state/sfc/26163.json` 实际 14 行，11-13 提醒有 6 场未展示，已不出现映射计数；实际日报包含 26163 栏。
此重放同样核对全部 2,369 个文件 SHA256 不变。解压副本没有 Git 历史，因此这条日报验收不声称 SFC 发布已确认。

## 复跑与检查

```bash
PYTHONPATH=src python scripts/verify-t55-archive.py \
  --archive /path/to/fixed-archive --git-repository /path/to/full-git-repo \
  --output /path/to/new-t55-output
PYTHONPATH=src python scripts/verify-t55-t52-archive.py \
  --archive /path/to/fixed-archive --output /path/to/new-t52-output \
  --archive-commit b492f4d7c501fd8cdbd2694d760f2f13892f796c \
  --as-of 2026-11-12T11:00:00+00:00
PYTHONPATH=src python scripts/verify-t55-review-archive.py \
  --archive /path/to/fixed-archive --git-repository /path/to/full-git-repo \
  --output /path/to/new-t55-review-output
PYTHONPATH=src python -m pytest -o addopts='' -q
ruff check src tests
ruff format --check src tests
```

所有测试和验收均使用离线 fixture 或已归档原件。此次修改的63项账户/发布/日报集成测试通过；
全量 `pytest -q`：**2756 passed，0 failed（139.90s）**。
Ruff、格式和差异检查全部通过；完整日志与JUnit XML在修订证据目录，既有NumPy弃用提示保留。
独立复核另有59项相关测试通过。

`SHA256SUMS` 的 `repository/` 指当前代码，`evidence/` 指本次修订证据目录；
`prior-repository/` 指初版代码 `2710059`，`prior-evidence/` 指11-12初版证据目录，旧证据不覆盖。
