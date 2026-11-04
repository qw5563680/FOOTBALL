# T30 · 逐场技术数据端点、权限、字段与覆盖率实测（阶段 A，小扩展）

## 背景
见 `docs/research/2026-11-04-match-stats-program.md`。当前 `operation=probe` 只支持 `/fixtures` 首页与 `/leagues?id=`；
技术统计、事件、阵容、球员、伤停端点从未请求过。7 个桥接联赛的 `coverage` 元数据为 true 只是线索，字段是否有值必须读真实比赛响应。
正式 key 只在 Actions Secrets；本任务只扩展探测代码与离线测试，真实探测由审查者在 Actions 触发。

## 前置依赖
T22（配额守卫）、T23（桥接联赛 ID）。不依赖 T29 之外的任何未合并分支。

## 范围
1. **`probe_kind=stats`**（`src/zucai/cloud_probe.py` 新模块或 `cloud_stats_probe.py`；`cloud_feeds` 与 workflow 的 `probe_kind` 选项加 `stats`）：
   - 输入：`leagues`（默认 7 个桥接联赛 + 3,848 + 九个采集联赛 + 2 欧冠**仅元数据**）、`seasons`（默认 `2022,2024,2026`）、`per_scope`（每联赛季抽样场数，默认 2）。
   - 步骤：(a) `/leagues?id=` 读 `coverage`（12 个未实测联赛）；(b) 每个联赛季 `/fixtures?league&season` 一页，从中按状态抽样：`FT` 常规完赛、`AET/PEN`（若有）、`PST/CANC`（若有），优先与库内 `match_id` 能对应的比赛；(c) 对抽到的 fixture 分别请求 `fixtures/statistics`、`fixtures/events`、`fixtures/lineups`、`fixtures/players`（端点名以响应 `get` 字段核验，文档不可访问时以实测为准）；(d) 每个联赛季请求一次 `injuries?league&season`；(e) 若 `fixtures/statistics` 支持半场参数（需验证），对 1 场加测。
   - 每个响应：原件写 `data/raw/api-football/`（复用 `RawStore` 与 `safe_json_for_archive`），sidecar 记配额头；`HTTP 200` 但 `errors` 非空按失败记录，不算成功。
   - 审计 `state/audit/apif_stats_probes.csv`：run_id、observed_utc、league、season、fixture_id、endpoint、http、errors、results、字段清单（statistics 的 type 列表）、`expected_goals` 是否出现且非空、空值字段数、`period_basis`（FT/AET 标记）、raw_sha256、请求耗时、outcome、reason。
   - Actions 摘要：按赛事 × 赛季 × 端点列抽样数 / 有效数 / 空值数 / 请求数 / 剩余配额。
2. **预算**：全部请求过 `DailyQuota`（reserve 保持 10）；默认抽样总请求数 ≤ 500，超出时拒绝启动并提示缩小范围；间隔沿用 6.1 秒（本任务不改）。
3. **文档**：`docs/DATA_SOURCES.md` 第 6 节新增「技术数据端点实测」小节模板（由审查者回填结果）；README `probe` 用法补 `stats`。

## 不做
- 不入库、不建表、不解析进模型；不请求赔率 / 预测端点；不改请求间隔与 job 时长；不新增 apif 欧冠赛程导入（欧冠只读元数据）。
- 不在开发环境用正式 key；测试只用 `data/fixtures/`。

## 失败处理
权限错误（`errors.plan/access`）、限流 429、空响应、结构不符分别记 reason 并继续下一项；配额触底立即停止并写部分结果；任何原件归档失败视为该项失败。

## 测试（离线 fixtures：合成的 statistics / events / lineups / players / injuries 响应各一份，含空值与 errors 两种变体）
抽样选择与状态覆盖；端点响应解析为审计行；`errors` 非空判失败；`expected_goals` 缺失 / 字符串数值两种形态；配额预算超限拒绝；原件与 sidecar 写入；CSV 追加去重；`probe_kind=stats` 的 workflow 条件（actionlint）。

## 真实验收（审查者触发）
`operation=probe probe_kind=stats`（默认范围）：run 成功，artifact 含原件；审计表覆盖 ≥ 18 个联赛季 × 5 端点；摘要列出各端点有效率；请求总数 ≤ 500，剩余配额 ≥ 6,900。审查者把结果回填 DATA_SOURCES 并据此定稿 T31 字段字典。

## 完成标准
`pytest -q` 全绿，`ruff check src tests examples`、`ruff format --check src tests`、actionlint 通过；PR 描述按模板；真实探测由审查者完成后 PR 才合入。
