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
旧评分保留原字节；不合格旧纸面账本保留原字节前缀，在内存重放有效视图；后续有效新票据只追加一次，不改历史票据。
校正视图可能重算资金、B 账户凯利注额和结算；与旧 ID 冲突的值仅用于当前统计，不能当作原时点冻结决定。
后续新 ID 按校正视图的现金约束保存，重读时再次应用同一校正规则；未删除原始记录。
模型、参数、P2、BET 未改变。

## 主项验收

| 标准 | 实跑证据 |
| --- | --- |
| 1. 11-07 四场切回赛前已发布版本 | 4/4 从 `42380973719-1-2026-11-07` 切为 `42358846882-1-2026-11-07`；四场正式概率逐字节相同，命中仍 3/4 |
| 2. 生成早但发布晚、发布时间未知 | 构造记录均未评分，各计 `published_after_kickoff=1`、`publication_unknown=1`；端到端测试另验证旧评分/账户被排除而原评分/账本字节不变 |
| 3. 已在开球前发布的原选版不变 | 其余 92 条已评分记录完整规范 JSON 逐字节一致，无概率舍入或容差；共同 SHA256 为 `1bcd3fbcb20a96accde4de2b7cba863b589b3498f39ec327d77c66921a612bef` |
| 4. 原档案不变及检查 | 全部 2,369 个文件前后 SHA256 一致；恢复库 SHA256 亦不变。全量 pytest 2,753 项通过；ruff、格式与差异检查通过 |

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
旧不合格账本重选路径另由明确合成边界测试验证，未把合成结果冒充真实旧票。

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
PYTHONPATH=src python -m pytest -o addopts='' -q
ruff check src tests
ruff format --check src tests
```

所有测试和验收均使用离线 fixture 或已归档原件。18 项新发布时间回归测试通过，
独立审阅代理最终复跑的相关集成测试 100 项通过；全量最终检查由 PR 中的实际输出及同目录证据日志记录。
