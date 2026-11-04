# 逐场详细比赛数据接入与模型改进 · 现状核查与分阶段方案（2026-11-04）

作者：Claude（审查者）。依据：main `c9e3d97`、数据分支 `f5cbbea`、Actions 原件 artifact `apis-source-41612517821-1`。
本文只记录核查到的事实与计划；**没有任何一项表示预测已经更准**。

## 1. 现状清单：项目现在到底用了什么

| # | 问题 | 事实 |
| --- | --- | --- |
| 1 | 数据源能提供什么 | **sporttery / 500.com（jc）**：赛程、竞彩赔率快照、官方赛果、14 场胜负彩。**football-data.co.uk（fd）**：9 个主联赛季文件（E0 E1 SP1 I1 D1 F1 F2 N1 P1）含全场/半场比分、收盘赔率，以及「where available」的射门 HS/AS、射正 HST/AST、角球 HC/AC、犯规 HF/AF、黄牌 HY/AY、红牌 HR/AR、越位 HO/AO、裁判、观众（站方 notes.txt，2026-11-04 读取）；6 个国别合并文件（JPN SWE NOR FIN USA BRA）只有比分与赔率。**football-data.org（fdorg）**：赛程、比分，欧冠。**API-Football（apif，Pro 档）**：赛程、比分之外，`/leagues` 元数据对 7 个桥接联赛 2021–2026 季标 `fixtures.events / lineups / statistics_fixtures / statistics_players = true`、`injuries = true`（葡超 2021–2024、2026 为 false）、`predictions = true`（见第 3 节证据）。赔率端点不用。 |
| 2 | 项目实际请求了什么 | jc：赛程 / 赔率 / 赛果页面；fd：季 CSV；fdorg：`/competitions/{code}/matches`；apif：**只有** `/fixtures?league&season`、`/fixtures?date`、`/leagues?id=`、`/status`。T10 / T22 任务单明文「不访问赔率、事件、阵容、统计端点」。 |
| 3 | 原件里已经保存了什么 | 所有来源的原始响应（含 fd 整张 CSV、apif 整季 fixtures JSON）只写 `data/raw/`，随 Actions artifact 保留 **90 天**，不进数据分支。因此 fd CSV 里已下载过的射门 / 角球 / 牌数列**没有被解析也没有长期保存**；apif 原件里只有赛程比分字段。 |
| 4 | 哪些字段进了数据库 | `matches`（身份、开球、状态、counterpart）、`results`（90 分钟比分、半场比分、`available_utc`）、`odds`（jc 快照）、`closing_odds`（fd PS/B365/Avg/Max 收盘）、`predictions`、`sfc_*`、`teams`。**没有任何技术统计、事件、阵容、伤停表。** |
| 5 | 哪些字段真正进了当前模型 | 国内 15 个 Dixon-Coles 与欧战联合模型（`predict/dixon_coles.py`、`pooled_fit.py`）的输入只有：`home / away`（规范球队名）、`home_goals / away_goals`（90 分钟）、`kickoff_utc`（时间衰减权重、半衰期）、`league`（范围与先验）、`available_utc`（截点过滤）。另有训练用平局校准（赛果）与未知球队先验（同池分位数）。**赔率、半场比分、休息天数、任何统计都不进模型。** |
| 6 | 哪些只用于报告 / 对照 / 未使用 | 市场概率（jc had / hhad 去水、fd 收盘）只做评估对照列与 T19 虚拟账户下注；半场比分只入库；`zucai features build`（T04 / T13）能算近 5 / 10 场进失球、积分、休息天数、交锋、市场列共 43+7 列，但**没有任何模型读取它**，只有 CLI 输出；fd 统计列从未解析。 |

## 2. 覆盖范围（按预测赛事）

- 预测对象：15 个国内联赛（英超、西甲、意甲、德甲、法甲、荷甲、葡超、巴甲、英冠、法乙、瑞超、挪超、芬超、日职、美职；配置另含丹超、韩职、澳超、中超的 apif 采集）+ 欧冠 / 欧罗巴 / 欧协联。
- 历史窗口起点：最近 3 个完整赛季 + 当季（2023/24–2026/27，7 月换季）。库内这一窗口共 **22,075 场**（英超 1224、西甲 1253、意甲 1241、德甲 991、法甲 1004、荷甲 1021、葡超 1008、巴甲 1355、英冠 1835、法乙 1080、瑞超 856、挪超 864、芬超 612、丹超 325、日职 1224、韩职 460、澳超 318、中超 480、美职 1772、欧冠 660、欧罗巴 922、欧协联 1570），当季已入库 3,065 场。
- 其中**已带 apif fixture ID** 的只有 4,415 场（apif 来源行与 jc 关联行）；fd 来源的 9 个大联赛和 fdorg 来源的欧冠**没有 apif ID**，取技术数据前必须先建「技术数据关联索引」（第 6 节）。
- 展示窗口「近 10 场 / 近 20 场」是报告口径；训练用全部可得历史，不因展示窗口裁剪。
- 覆盖不足的真实情况要如实列：apif 对国别合并文件联赛（日职、瑞超、挪超、芬超、美职、巴甲）历史季是否有统计尚未实测；fd 统计只覆盖 9 个主联赛且「where available」；欧冠 2021/22、2022/23 库内无比赛。

## 3. 已有真实证据与仍未知的部分

**已实测（无新请求，读取 run 41612517821 的原件 artifact，SHA256 清单见 `state/audit/apif_league_probes.csv`）**：7 个桥接联赛 2021–2026 各季 `coverage.fixtures.events / lineups / statistics_fixtures / statistics_players` 全部 `true`；`injuries` 除葡超部分赛季外 `true`；`odds` 仅当季 `true`（不用）。这些只是**元数据线索**，不是字段有值的证明。

**仍未知（需 T30 真实探测）**：
- 11 个采集联赛（3、848、98、292、188、113、103、244、119、253、169）与欧冠（2）的 coverage 元数据；
- 各端点的真实响应：`fixtures/statistics` 是否带 `expected_goals`（第三方样本见过字符串 `"1.89"`，供应商未在可访问页面确认）、`goals_prevented`、是否支持半场参数；`fixtures/events`、`fixtures/lineups`、`fixtures/players`、`injuries` 的参数、空值形态、加时赛累计口径、历史季是否为空；
- 官方文档页本轮对自动读取返回 403，端点名以实测响应 `get` 字段为准，任务单里的端点名都标「需验证」；
- 每分钟上限：官方定价页只列日额度（Pro 7,500/日），分钟上限未见于可访问页面，须以响应头 `x-ratelimit-limit` 为准。


### 3a. T30 第一轮真实探测结果（2026-11-04，run 41779592141 / 41783408987，共 375 次请求）

- 分钟上限实测 `x-ratelimit-limit: 300`，日上限 7500（响应头）。
- `fixtures/statistics`：16 类固定统计 + `expected_goals` / `goals_prevented`（字符串）；**xG 自 2024 季起出现**，2022 季 19 个联赛全部没有；半场参数被忽略，半场统计不可得。
- `fixtures/events`、`fixtures/lineups`：54/54 个联赛季样本有数据。
- `fixtures/players`：欧罗巴、欧协联样本全部为空，芬超 2022/2024、韩职 2024 为空。
- `injuries`：日职、韩职、中超、葡超、欧协联为空；其余联赛每季数百到数千条。
- 欧战样本全部落在 7 月资格赛（统计为空），联赛阶段待修复抽样后定向探测。
- 详见 `docs/reviews/2026-11-04-t30-review.md`。

## 4. 字段字典（草案，供 T31 定稿）

规则：缺失写 `null` 并记原因，不填 0；无 xG 不得用射门或进球冒充；半场数据不按比例推算；比率缺分母不编造；加时赛统计不可靠分离时标 `period_basis=unknown` 并禁止进 90 分钟模型；同名字段跨供应商不拼接；更正保留版本。

| 字段（规范名） | 来源字段 | 单位 / 方向 / 时段 | 供应商 | 可得时间 | 优先级 |
| --- | --- | --- | --- | --- | --- |
| ft_goals / ht_goals | fd FTHG/FTAG/HTHG/HTAG；apif score.fulltime/halftime | 球 / 本队与对手 / 全场、半场 | fd、apif | 已有 `results.available_utc` | 已入库 |
| shots_total / shots_on / shots_off / shots_blocked | fd HS/AS、HST/AST；apif Total Shots、Shots on Goal、Shots off Goal、Blocked Shots（需验证） | 次 / 双向 / 全场 | fd 免费、apif | 回填=采集时刻（非当时） | 优先 |
| shots_inside / shots_outside | apif Shots insidebox / outsidebox（需验证） | 次 / 双向 / 全场 | apif | 同上 | 优先 |
| possession_pct | apif Ball Possession（字符串 "55%"，需验证） | % / 双向 / 全场 | apif | 同上 | 优先 |
| corners（全场；半场单独核实） | fd HC/AC；apif Corner Kicks | 次 | fd、apif | 同上 | 优先 |
| fouls / offsides / yellow / red | fd HF/AF、HO/AO、HY/AY、HR/AR；apif Fouls、Offsides、Yellow Cards、Red Cards | 次 | fd、apif | 同上 | 优先 |
| gk_saves | apif Goalkeeper Saves | 次 | apif | 同上 | 优先 |
| passes_total / passes_accurate / passes_pct | apif Total passes、Passes accurate、Passes %（分子分母齐全才算比率） | 次、% | apif | 同上 | 优先 |
| xg / xga（对手 xg） | apif expected_goals（是否存在、覆盖哪些赛事赛季待实测） | 期望球 / 双向 / 全场 | 仅同一供应商口径 | 同上 | 优先（有则接） |
| goals_prevented | apif goals_prevented（待实测） | 期望球 | apif | 同上 | 有来源再接 |
| event_time（进球 / 点球 / 红牌 / 换人） | apif fixtures/events：time.elapsed(+extra)、type、detail、team、player | 分钟 | apif | 同上 | 优先 |
| lineup_start / lineup_bench / minutes | apif fixtures/lineups、fixtures/players（minutes） | 名单、分钟 | apif | **赛后抓取不算赛前已知**；赛前存档才算 | 优先（口径受限） |
| injuries（伤缺 / 存疑 / 停赛） | apif injuries（league+season 或 fixture；历史可得性待实测） | 名单 + 原因 | apif | 采集时刻 | 有来源再接 |
| 绝佳机会、关键传球、对抗、抢断、拦截、解围、危险进攻 | 供应商定义不一 | — | — | — | 有可靠来源再接 |

## 5. 请求预算、运行时长、存储量（估算，正式回填后重算）

- 配额：当前配额文件显示 `plan=Pro, limit_day=7500`（响应头为准，不写死）；`reserve=10`；日常采集约 10–30 次/日。可用于回填的约 **7,400 次/日**。
- 请求数（22,075 场）：仅 statistics ≈ 22,100；+events ≈ 44,200；+lineups ≈ 66,300；+players ≈ 88,400；关联索引另需 9 个 fd 联赛 × 4 季 = 36 次，欧冠 4 次（范围变更，见第 6 节）；探测约 450 次。
- 日额度角度：仅 statistics 3 天；四端点 12 天；加 injuries 与补抓约 +20%。
- **运行时长是真正的瓶颈**：现行自定间隔 6.1 秒 → 每小时 590 次，`apis` job 上限 60 分钟 → 每次 run 只能约 580 次；四端点需约 150 次 run。若把统计端点间隔改为 1 秒（Pro 分钟上限以响应头实测为准）且回填 job 上限 5 小时，则每 run 约 18,000 次，受日额度而非时长限制。**间隔与 job 时长是所有者决定项**（见第 9 节）。
- 当季增量：每天完赛约 50–150 场 × 4 端点 = 200–600 次，6.1 秒间隔 20–60 分钟，可放入现有晚班。
- 存储：原件单场约 statistics 2.5 KB、events 6 KB、lineups 9 KB、players 45 KB → 22k 场约 55 MB / 130 MB / 200 MB / 1 GB。原件压缩后走 Release 附件或 artifact，不进 git 数据分支；规范化表（球队×场统计 ~3 MB、事件 ~30 MB、阵容 ~50 MB）以 JSONL/parquet 进数据分支并进 manifest。

## 6. 身份关联与范围边界

- 技术数据必须附着到库内已有 `match_id`：同联赛、同季、双方 apif 球队 ID 经正式别名解析为与库内一致的规范球队、开球 ±3 小时、一对一；否则进 `state/audit/stats_link_audit.csv`，不新建比赛或球队，不按名字猜。
- fd 来源的 9 个大联赛与 fdorg 欧冠没有 apif fixture ID：需要「技术数据关联索引」`apif_fixture_index`（provider、provider_fixture_id、match_id、链接依据、原件 SHA256）。为 7 个桥接联赛建索引只需 `/fixtures?league&season`（T23 identity 已用过同一端点，不导入比赛）；**欧冠需请求 apif 联赛 2**，这与 T20「不新增 apif 欧冠采集」冲突——本方案把它限定为「仅建关联索引、不导入赛程赛果、不改 fdorg 优先级」，列为所有者决定项；未批准前欧冠技术数据不做，其余工作不受影响。
- 复用 T23 / T29：临时身份不绑定；身份合并与传播后再链接。

## 7. 存储方案（T31 定稿）

新增三张附属表（独立于现有表，不改旧表）：`match_stats`（provider、provider_fixture_id、match_id、team_id、side、period_basis、stat_key、raw_value、value、unit、captured_utc、available_utc(可空)、availability_basis、raw_sha256、parser_version、missing_reason、version）、`match_events`、`match_lineups`（含 role=start/bench、minutes、captured_before_kickoff 布尔与捕获时刻）。重复采集按 (provider_fixture_id, endpoint, raw_sha256) 去重；新响应空值不覆盖已有有效值，更正写新 version；导出 / 恢复 / manifest / 断点续跑与现有 feeds 机制一致；不改冻结预测、赛果可知时间、虚拟账户。

## 8. 赛前特征与防偷看（T33 定稿）

特征按每场自己的截点 `min(as_of, kickoff)` 计算，只用 `available_utc` 严格更早的记录；回填的历史统计 `availability_basis=backfilled`，只能作带限制的历史回放，不称实时验证；比分的 `available_utc` 不复用给统计；赛后拿到的阵容不当赛前已知；预计首发 / 正式首发、伤缺 / 存疑分开；对手视角按底层比赛去重。真正的前瞻验证从接入后冻结开始累积。

## 9. 需要所有者决定的事项

1. 统计端点请求间隔是否从 6.1 秒放宽（以响应头分钟上限为准），以及回填 job 时长上限（建议新 job，≤ 5 小时）。
2. 是否批准「apif 欧冠（联赛 2）仅建关联索引」的范围变更。
3. 原件长期归档位置：Release 附件（推荐）还是继续只靠 90 天 artifact。
4. 四个端点的接入顺序：建议 statistics → events → lineups → players；injuries 视探测结果。

## 10. 阶段任务单与依赖

T30（A：探测）→ T31（B：解析 / 存储 / 关联 / 离线测试）→ T32（C：回填与增量）→ T33（D：赛前特征与模型实验）→ T34（E：冻结前瞻比较与采用决定）。
T31 中 fd 统计列解析不依赖 apif 探测结果，可与 T30 并行。
