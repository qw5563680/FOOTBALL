# T44 · 百项比赛指标：登记表、覆盖核对与逐场双方对照表（阶段 F-1）

## 背景
所有者 2026-11-06 交来《足球比赛指标》工作簿：原始 39 项（比赛控制 / 射门结构 / 机会质量 / 传球组织 / 突破与对抗 /
防守与门将 / 定位球与纪律）、扩展 61 项（40–91 解释已踢完的比赛，92–100 描述开球前可知的阵容与赛程）和一张
「采集、计算与核对规则」表。三张表已原样转为 CSV 存入 `docs/indicators/`：

- `match-indicators-core-39.csv`、`match-indicators-extended-61.csv`：编号、分组、指标名称、单位、怎样记／怎么算、口径与常见错误、建议英文字段。
- `collection-and-aggregation-rules.csv`：身份与样本（双方近 10 场、主客拆分）、时段、空值四种含义、可追溯、聚合、建模、来源示例。

所有者的目标是**每场比赛一份双方对照报告**：两队各自近 10 场（含其中主场 / 客场子集）的这些指标，附有效样本数与来源。
仓库现状：`match_stats` 只存 API-Football `fixtures/statistics` 的 16 类固定项加 `expected_goals` / `goals_prevented` /
`Free Kicks`（`sources/apif_stats.py` 的 `STAT_TYPES`），fd CSV 提供 S / ST / C / F / O / Y / R / FKC（`sources/fd_stats.py`），
`match_events` / `match_lineups` / players 原件已归档，`match_injuries`（T38）已入库。半场统计经 T30 实测不可得。
因此 100 项中一部分已有、一部分可从已归档原件派生、一部分供应商根本没有。先把「有 / 没有 / 能算 / 不能算」说清楚，
再按规则表把能算的算出来，不猜、不补、不用别的数冒充。

## 范围
1. **指标登记表进代码**：`src/zucai/indicators/registry.py` 读取 `docs/indicators/*.csv`（随包安装或复制到 `data/indicators/`），
   每项含编号、英文字段、单位、分组、是否原始量 / 对手镜像 / 比率 / 派生量（按规则表「避免多算一遍」归类），以及**分子 / 分母字段**（成功率类）。
   测试校验 100 个编号唯一、英文字段唯一、分子分母引用存在。
2. **覆盖核对（离线，只读已归档原件与 fixtures）**：`zucai indicators coverage` 输出 `reports/indicators/coverage-v1.md`（≤ 100 KB），
   每项一行：来源（apif statistics / events / lineups / players / injuries、fd、jc、matches 表、无）、状态
   （`stored` 已入库 / `derivable` 可由已归档原件或已有表派生 / `provider_has_not_collected` 供应商有但本项目未采集 /
   `provider_lacks` 已探测供应商没有 / `needs_new_source` 需新来源 / `not_applicable_now`），依据（引用 `docs/DATA_SOURCES.md` 的 T30 实测表或 fixtures 文件名）。
   不发任何真实请求；对「供应商有但未采集」只列出端点与大致配额估算，**不在本任务新增采集**。
3. **长表存储**：新表 `match_indicators`（按规则表「建议的一行字段」）：`match_id, team_id, side, period('FT'), indicator, provider, version,
   value(nullable), status('measured'|'na'|'unverified'|'not_applicable'), numerator, denominator, source_record_key, raw_sha256, captured_utc`。
   底层数值用 null 配状态与原因；来源明确测为零才写 0。`export/restore/verify` 纳入该表（technical_data v4 或新 manifest 段，旧归档恢复为空表）。
4. **先算能算的**（全部离线，由已有表派生，不改采集）：
   - 1–39 中 `stored` 项：从 `match_stats` 直接映射（3 控球、4–9 射门结构、12 xG、14–16 传球、32 扑救、33 角球、35 越位、36 犯规、37 任意球、38–39 牌）；
     fd 的 S/ST/C/F/O/Y/R 作第二来源并标 provider，不与 apif 混算。27–31 抢断 / 拦截 / 解围 apif 球队统计没有（players 端点有逐人 tackles，未入库），本任务留 na 并在覆盖报告标 `provider_has_not_collected`。
   - 40–49 由 `match_events`（Goal / Card 时间线）派生：半场进球、全场进球、首球时间、领先 / 打平 / 落后分钟、双方 11 人分钟、人数劣势分钟、
     两黄变红人数、首次罚下时间。分钟计算的补时口径按规则表写进文档，缺事件原件则整组 na，不用比分倒推。
   - 58–59 点球由 events 的 `detail` 派生；13 npxG 只有 apif 同场点球 xG 可减时才算，否则 na（不固定减 0.76）。
   - 92–94 由 `matches` 表派生（上一场开球间隔、过去 14 天完赛数；94 加时负荷无数据则 na）。
   - 95–97 由 `match_injuries`（截点前最新完整捕获）与 `match_lineups` 派生；98 首发更换只在**两场官方首发都在预测截点前捕获**时计算，否则 na 并注明；
     100 换帅后场数由 lineups 的 coach 字段按时间序派生，临时教练单独标识。
   - 其余（1–2 进攻次数、10–11 绝佳机会、17–26 传球细分 / 过人、28–31 防守细分、34 上半场角球、50–57、60–91、99）本任务只登记状态，不实现。
5. **逐场双方对照表**：`zucai indicators report --match <match_id> [--as-of]` 生成 Markdown：两队近 10 场（预测截点前已结束的一线队正式比赛，
   排除友谊赛 / 青年队）与其中主场 / 客场子集；次数类报总数与场均并附 `n/N`，成功率用总分子 ÷ 总分母，只有逐场百分比时标「逐场均值」，
   分母为 0 写不适用；每格附来源与状态。先只做命令行与文件输出，**不改日报结构**（接入日报另开任务）。
6. 文档：`docs/ARCHITECTURE.md` 数据模型加 `match_indicators`；`README.md` 快速开始加两条子命令；`docs/DATA_SOURCES.md` 不新增来源结论。

## 不做
- 不接入新数据供应商，不发真实请求，不扩大 API-Football 采集范围（players 端点入库、伤停以外的新端点另开任务）。
- 不把任何指标放进正式模型或实验模型；不改 T33/T34/T38 实验；不碰赔率。
- 不用总数倒减填空，不用进球加扑救反填射正，不固定扣 0.76 算 npxG，不把全场角球除二当上半场。
- 不改日报 / 记分板；不把 100 项声称为「已采集」。

## 测试（离线）
- 登记表：编号 / 英文字段唯一，分子分母引用闭合，三张 CSV 行数 39 / 61 / 18。
- 派生：用 `data/fixtures` 已有 events / lineups / injuries 样本构造已知答案（例：两黄变红一次、首球 23'，领先 67 分钟）；缺原件时整组 na。
- 聚合：n/N、总分子 ÷ 总分母、逐场均值标注、分母 0 → 不适用；主场子集只有 1 场显示 1/1。
- 覆盖报告：每个编号恰好一行，状态取值合法，`stored` 项必须能在 `match_stats` 或 fd 字段映射里找到。
- export/restore/verify 对新表的完整性与哈希。

## 验收标准
- 100 项每项有登记、有状态、有依据；覆盖报告 ≤ 100 KB 且不含逐场数据。
- 范围 4 列出的派生项在 fixtures 样本上答案正确，缺数据时为 null+状态而不是 0。
- `indicators report` 对任一已有 jc 比赛能输出双方对照表，格里没有「不能算却填了数」的情况。
- 不新增任何真实请求；pytest 全绿、ruff 通过。
