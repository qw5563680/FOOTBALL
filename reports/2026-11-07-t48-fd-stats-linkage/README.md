# T48 · 历史射正统计附着修复与固定原件离线验收

**结论：离线链路修复并验收通过。210场历史比赛的420个两侧射正值全部核对成功；21场目标有16场四格10/10，另外5场继续资料不足。没有运行生产或生成新的模型概率。**

## 代码与固定材料

- 业务代码提交：`50bac669f63712ce9f27de0c1ddbae8cf0a3b027`；基于 main `d10f37fea3f5f4e41c340c4a724d29804649d958`。最终PR head可能只追加本报告与同版验收脚本。
- 独立修复分支 `codex/T48-fd-stats-linkage`，草稿 [PR #99](https://github.com/qw5563680/FOOTBALL/pull/99)。[T47/#98](https://github.com/qw5563680/FOOTBALL/pull/98)仍是原排查报告。
- 原运行 `42256764906-1`，原代码 `e124e2dfc9293a7f619baf46d26e18c8a359636e`；实际输入 `afd2e1b0c6e8e396b1d931055da906959b9997f1`，观察冻结 `1a30f7dda5b6f81b931f10c2a4dcaa58978caf2a`。
- 截点 `2026-11-06T17:13:13.835049Z`；N=10。恢复库SHA256 `9bf57b6fc67b384943777674a3bf4b0c46237315ef21021582e33328dd67a441`，原21/21冻结cells逐项复现一致。
- 原manifest的102技术分片和66伤停回执全部校验。9份原件均取T47的S1–S9指定SHA及真实sidecar，原包SHA256 `4fc5ec8c23e0168f3996185e163d63e0ccf0a6c9611f69e03e9ac2ab58d906d5`，来源为既有[stats-raw-202611 Release](https://github.com/qw5563680/FOOTBALL/releases/tag/stats-raw-202611)。本轮没有体育源HTTP请求。

## 改动

1. 历史回填可沿已有明确jc→fd counterpart附着，修复184场路径。
2. 另外26场用正式联赛别名、规范主客ID、原件身份/真实时间、同联赛三小时窗口及双向唯一证据建立独立`fd_stats_links`；不覆盖fdorg或空counterpart，不合并旧Team。
3. writer/归档共用严格关联与原件观察验证；证据通过可选hash-listed `state/fd_stats_links.jsonl`先于技术行恢复。旧technical_data v1/v2/v3的列、分片和snapshot口径不改。
4. 新`stats replay-fd`按显式原件/sidecar哈希清单预解析全部CSV，再逐文件事务回放；不依赖ok调度游标，不下载数据。重复检查在事务内复用审计快照，保留新捕获回执逻辑。

## 真实结果（同一最终代码）

| 检查 | 实测结果 |
|---|---|
| 210历史场 | 210/210成功；逐场原CSV行、provider fixture ID、两侧HST/AST、raw_value、SHA及captured均一致 |
| 21目标 | 16场四格10/10；5场继续不足；原历史样本ID和sample_n未变 |
| 9整份原件 | 新增6624条技术统计行；FD由217008增至223632；API-Football117652不变；总match_stats=341284。记录行不是独立比赛数 |
| 新增统计关联 | 整份9文件52条，其中本次210目标历史范围26条；额外记录同属9文件中其他已存在的合格比赛，未新建球队/比赛 |
| 第二次9文件重放 | 新增0；DB字节、全表canonical、审计/状态文件均不变；重复报告的27条拒绝不是新增数据 |
| 全量旧snapshot保护 | 本地publish_rows逐行校验全部旧分片；原技术行不丢、不改ID |
| 导出恢复 | 本地Release替身→publish_rows→export_feeds→restore_feeds→restore_rows；全部技术行、52关联、行快照、21目标features一致 |
| 原有数据保护 | teams/matches/results/odds/closing_odds/predictions六表均不变；3551个源资料、冻结预测和账本文件无新增/删除/改写 |
| 执行版一致性 | 147个src文件前后哈希一致；执行脚本与提交example同哈希 |
| 数据校验 | 外键无错误，SQLite完整性ok |

9文件中的27条拒绝全部保留审计；不为它们创建身份或填值。失败35场的诊断和后来修正依据也保留在交付包：全局英文原队名身份并存不等于本场provider fixture冲突；独立定向复核确认35场正式canonical与实际主客完全一致。修复只收窄文字门槛，未改旧球队或真正的主客/同源ID/时间冲突检查。

## 仍不足的5场

四格顺序为主射正/主被射正/客射正/客被射正，分母始终10。

| 目标 | 四格n/N | 仍缺什么 |
|---|---|---|
| jc:2042564 埃弗顿—考文垂 | 10/10、10/10、8/10、8/10 | 考文垂原同联赛窗口只有8场；本轮不修赛果身份或增加比赛 |
| jc:2042565 德比郡—博尔顿 | 全部9/10 | 原取得英冠CSV不含fdorg:561921、fdorg:561928；不补采、不补零 |
| jc:2042593 帕德博恩—法兰克福 | 8/10、8/10、10/10、10/10 | 帕德博恩同联赛窗口仅8场 |
| jc:2042594 多特蒙德—埃沃斯堡 | 10/10、10/10、8/10、8/10 | 埃沃斯堡同联赛窗口仅8场 |
| jc:2042596 阿森纳—赫尔城 | 10/10、10/10、9/10、9/10 | 赫尔城同联赛窗口仅9场 |

## 检查与独立复核边界

- 全量：`OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 pytest -q -o addopts='' --junitxml=...`：**2533 passed，0失败、0错误、0跳过**。6341条已知第三方penaltyblog/NumPy弃用警告，未隐藏或排除测试。
- `ruff check src tests examples`、`ruff format --check src tests`（277文件）、`git diff --check`通过。未改工作流，不伪称新跑actionlint。
- 新测试先复现失败再修复。独立分支检查提出R1正式主客矛盾、R2空/错误witness、R3后续CSV预检三项，均已有RED→GREEN回归；保留原评审和修复说明，不把它写成用户独立复核窗口已批准。
- 全量测试绿色不代表模型盈利；本轮仅计算固定截点特征，不重训、不出新概率。模型/参数/P2/BET不变，N=10不变，历史采集时钟不改；新行仍backfilled/null available。

## 复现与完整证据

可执行脚本：`examples/t48_verify_replay.py`。独立副本需先按固定Git提交恢复feeds，按manifest准备全部分片/回执，按S1–S9哈希准备真实原件和sidecar。`selected-raw-manifest.json`逐份显式列路径、div/season、CSV/sidecar SHA、captured、长期raw_location。搬工作区时只调整路径，不改固定内容和时钟。

实际执行命令（仅临时副本）：

```sh
/workspace/work/t48-venv/bin/python /workspace/work/t48-acceptance/scripts/verify_repair.py --inputs /workspace/work/t48-acceptance --handover-evidence '/workspace/work/football_handover_20261107/详细排查证据' --code /workspace/work/football-t48-fix --output /workspace/work/t48-acceptance/final-verified --evidence /workspace/work/t48-acceptance/final-verified/evidence --protected /workspace/FOOTBALL/scoreboard --protected /workspace/FOOTBALL/sfc
```

脚本socket层禁网；本地Release替身只读旧assets并写临时目录。已生成完整证据包 `FOOTBALL_T48_离线验收_2026-11-07.zip`（逐场CSV/JSON、完整日志、哈希清单及原评审），不含数据库、模型、正式key或重复的大归档。当前执行环境的附件输出目录不可写，现有GitHub连接也没有Release附件上传能力，因此**未能交付ZIP下载链接**；完整文件暂留工作区，未声称已上传。PR正文直接附210场和21场逐项核对表，复核不只依赖汇总数；全部检查可按本节固定材料与已提交脚本重跑。`SHA256SUMS`列出已生成包内的完整证据哈希；原材料仍引用既有固定Git/Release。

本轮未触发生产、未推data分支、未合并。保持草稿，供独立复核；尚无本修复的真实生产运行验收。
