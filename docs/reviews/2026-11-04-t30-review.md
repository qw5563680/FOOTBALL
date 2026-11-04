# T30 审查记录（阶段一）· PR #46 · 2026-11-04（北京）

审查者：Claude。实现者：Codex（分支 `codex/T30-stats-probe`，头提交 `bc7e3a8`，base main `84ea51f`）。
任务单：`docs/tasks/T30-match-stats-probe.md`。涉及数据来源，按审查流程第 5 条归档。

## 范围与本地验证

- diff 19 个文件 +1934/−9：`cloud_stats_probe.py`（计划、预算、逐端点观察、摘要）、`stats_probe.py`（抽样、响应解析、审计）、
  `cloud_feeds` 与工作流的 `probe_kind=stats` / `per_scope`、客户端 sidecar 增存分钟配额头、README / DATA_SOURCES 模板、61 项离线测试与 11 KB 合成 fixtures。
- 独立 worktree：`pytest -q` 1436 passed（87 s）；`ruff check`、`ruff format --check` 通过；CI（含 actionlint）绿。
- 对照任务单：范围 1–3 覆盖；「不做」未越界（不入库、不建表、不改间隔、欧冠只读元数据、不请求赔率）。
- 任务单默认范围预算 560 > 上限 500 的冲突：实现者如实拒绝启动、不偷偷缩样；审查者用显式参数分两轮完成验收，不改代码。

## 真实探测（审查者触发，正式 key 只在 Actions）

| run | 参数 | 请求 | 结果 |
| --- | --- | ---: | --- |
| [41779592141](https://github.com/qw5563680/FOOTBALL/actions/runs/41779592141) → 数据分支 `53288de` | 默认 19 个联赛 × 2022/2024/2026，`per_scope=1` | 344 | 54 个联赛季 × 5 端点全部有响应；剩余配额 7163；artifact `apis-source-41779592141-1`（715 个原件，SHA256 `b517cd58…`） |
| [41783408987](https://github.com/qw5563680/FOOTBALL/actions/runs/41783408987) → `9e85252` | `leagues=3,848 seasons=2024 per_scope=3` | 31 | 补到 PEN、AET 状态样本；剩余配额 7122 |

逐端点结论（抽样有效率不是全季覆盖率）：

| 端点 | 实测 | 字段 / 口径 |
| --- | --- | --- |
| `/leagues?id=` | 19/19 ok | 12 个新测联赛 coverage：events / lineups / statistics_fixtures 全 true；statistics_players 芬超 2022/2024 false；injuries 日职、韩职、中超、欧协联 false，澳超 2022 false |
| `/fixtures?league&season` | 54/54 ok | 每季一页返回整季（175–526 场） |
| `fixtures/statistics` | 50/54 ok，4 空 | 固定 16 类：Shots on/off Goal、Total/Blocked Shots、Shots inside/outsidebox、Fouls、Corner Kicks、Offsides、Ball Possession（字符串 "55%"）、Yellow/Red Cards、Goalkeeper Saves、Total passes、Passes accurate、Passes %（字符串）；另 `expected_goals`、`goals_prevented` 为**字符串**（"2.43"）。**xG 只在 2024 季起出现**，2022 季 19 个联赛全部无该字段；2024/2026 季 15 个国内联赛有值（丹超 2024、澳超 2024、韩职 2026 的样本为 null） |
| `fixtures/statistics?half=true` | 1 次，results 0、errors 空 | 参数被忽略，**半场统计不可得** |
| `fixtures/events` | 54/54 ok | `time.elapsed/extra`、`type`（Goal/Card/subst/Var）、`detail`（Normal Goal/Penalty/Own Goal、Yellow/Red Card、Substitution n）、team、player、assist |
| `fixtures/lineups` | 54/54 ok | `formation`、`coach`、`startXI` / `substitutes`（player.id/name/number/pos/grid）；赛后抓取，不证明赛前已知 |
| `fixtures/players` | 45/54 ok，9 空 | 每队球员 `games.minutes/rating/position/substitute` 及 shots/goals/passes/tackles/duels/dribbles/fouls/cards/penalty；**欧罗巴、欧协联全部为空**，芬超 2022/2024、韩职 2024 为空 |
| `injuries?league&season` | 35/54 ok，19 空 | `player.type`（Missing Fixture / Questionable）、`player.reason`（伤病类型、Yellow Cards、Red Card）、`fixture.id/date`、team；每季一页 200–6000 条；日职、韩职、中超、葡超、欧协联三季均为空 |

其他实测事实：响应头 `x-ratelimit-limit: 300`（每分钟上限，Pro），`x-ratelimit-requests-limit: 7500`；两轮共 375 次请求，耗时 35 分钟 + 4 分钟（6.1 秒间隔）。

**抽样偏差（本轮最重要的局限）**：抽样取每组状态里 ID 最小的比赛，欧罗巴 / 欧协联三季抽到的全是 7 月资格赛（如 2024 季 `1208482` Botev Plovdiv–Maribor，第一轮资格赛），这些场次统计全空（只有牌数），两轮共 9 个欧战样本无一来自联赛阶段。**欧战联赛阶段是否有统计与 xG 尚未证明**；国内联赛样本不受此影响。

## 发现

| 级别 | 内容 | 状态 |
| --- | --- | --- |
| 必须修 | 抽样无法触达欧战联赛阶段：`known_fixture_ids` 把 apif 来源行（资格赛）也算「已知」，排序后 ID 最小者优先。请增加显式 `--fixtures <id,id,…>`（≤ 20 个，仍过预算与 ALLOWED 校验）或把 jc 关联（`counterpart` 为 apif 的 jc 行）排在 apif 来源行之前。修后审查者补一轮欧冠外两项欧战联赛阶段的定向探测（约 20 次请求） | 待修 |
| 建议修 | 任务单默认范围与 500 上限冲突：把默认 `per_scope` 改为 1（预算 344）或把默认赛季改为两季，避免「默认即拒绝」 | 待回复 |
| 仅说明 | `half=true` 被服务端忽略（results 0、无 errors），半场统计不可得，T31 字段字典据此删去半场列 | — |
| 仅说明 | xG 自 2024 季起才有，2022/2023 季不能用射门冒充；T32 回填 xG 窗口按 2024 季起算 | — |

## 阶段二待办

修复后定向探测欧罗巴 / 欧协联联赛阶段（2024、2026 季各 2 场）；结果回填 DATA_SOURCES 第 6 节模板；据此定稿 T31 字段字典与 T32 预算。

---

# T30 审查记录（阶段二）· PR #46 头提交 `3c6f92e` · 2026-11-04

## 增量复审

- 必须修 1 采用方案 (b)：同一状态组内先取 jc 行 counterpart 关联的 apif 比赛，再取其他已知 ID，再取未知 ID，同档按 ID 稳定排序；
  `known_fixture_ids` 保留 jc 身份供排序。方案 (a) 因 workflow_dispatch 输入已达 10 项上限（actionlint 报错）未采用，理由成立。
- 建议修采纳：`per_scope` 默认改 1（默认预算 344），README / DATA_SOURCES 同步。
- 本地：`pytest -q` 1442 passed；ruff 通过；CI 绿。

## 定向探测（审查者触发）

run [41785683100](https://github.com/qw5563680/FOOTBALL/actions/runs/41785683100) → 数据分支 `7ca04b3`，`leagues=3,848 seasons=2026 per_scope=2`，23 次请求，剩余配额 7099，artifact `apis-source-41785683100-1`（SHA256 `db30d08f…`）。

| 样本 | 阶段 | statistics | xG | events | lineups | players |
| --- | --- | --- | --- | --- | --- | --- |
| 欧罗巴 `1636207`（jc:2041500 AC米兰–本菲卡） | 联赛阶段 FT | ok，17 类全有值 | 0.96 / 0.64 | ok | ok | ok（23 人，含分钟、评分、射门、传球） |
| 欧罗巴 `1554450` | 资格赛 PEN | ok 但 16 空值 | null | ok | ok | 空 |
| 欧协联 `1636379`（jc:2042130） | 联赛阶段 FT | ok | 有值 | ok | ok | ok |
| 欧协联 `1554416` | 资格赛 PEN | 空 | — | ok | ok | 空 |

结论：**欧罗巴 / 欧协联联赛阶段有完整统计、xG 与球员数据；资格赛阶段统计和球员数据缺失**（事件与阵容仍有）。
`half=true` 在联赛阶段比赛上返回与全场**逐项相同**的数值（AC Milan 射门 17、控球 60%、xG 0.96 两次一致），确认参数被忽略，半场统计不可得。
欧冠（apif 联赛 2）本任务只读元数据，联赛阶段未探测，须待所有者批准范围变更。

## 结论

必须修已关闭；三轮探测共 398 次请求，覆盖 54 个联赛季 × 5 端点加欧战联赛阶段 2 场。**可合并**。
