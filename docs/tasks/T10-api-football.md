# T10 · 接入 API-Football v3（亚洲与北欧联赛赛果）

## 目标
`zucai fetch apif` 能用 API-Football（api-sports.io）免费档补齐 football-data.co.uk 与 football-data.org 都不覆盖、但竞彩常年在售的联赛：
日职、韩职、澳超、瑞超、挪超、芬超、丹超、美职足、中超等，同样包含**整个联赛的全部场次**而非只有竞彩开售部分。
免费档每天 100 次请求，本任务的核心是在配额内把事情做完，而不是做得快。

## 背景与约束（2026-11-02 核实，细节见 `docs/DATA_SOURCES.md` 第 6 节）
- 基地址 `https://v3.football.api-sports.io/`，请求头 `x-apisports-key: <key>`，key 只从环境变量 `API_FOOTBALL_KEY` 读取，不进仓库、日志、sidecar。
  如经 RapidAPI 订阅则头名不同；本任务只支持 api-sports.io 直连。
- 免费档：100 次/天，UTC 零点重置，响应头 `x-ratelimit-requests-remaining` 给当日剩余（需验证）。
  第三方资料称免费档只开放部分历史赛季、可能拿不到当季；实现第一步用真实请求逐联赛确认可用赛季范围，写进 DATA_SOURCES。
  若当季不可用，本源定位为历史训练数据来源，README 如实写明。
- 核心端点 `GET /fixtures?league={id}&season={YYYY}`（一个请求返回整季，最省配额）、`GET /fixtures?date=YYYY-MM-DD`、`GET /leagues`。
  联赛 ID 候选（需验证）：日职 98、韩职 292、澳超 188、瑞超 113、挪超 103、芬超 244、丹超 119、美职足 253、中超 169、巴甲 71、英超 39、西甲 140、意甲 135、德甲 78、法甲 61。
- 时间：`fixture.timestamp` 为 Unix UTC，`fixture.date` 带时区偏移，优先用 `timestamp`。
- 比分：`score.fulltime` 为 90 分钟比分，`goals` 可能含加时，`score.extratime`、`score.penalty` 单列（需验证）。**常规时间比分只取 `score.fulltime`**。
- 状态 `fixture.status.short`：`FT` 完赛；`AET`/`PEN` 加时或点球完赛，仍以 `score.fulltime` 为常规时间；
  `PST` 延期、`CANC` 取消、`ABD` 腰斩、`AWD` 判罚、`WO` 弃权；`NS`/`TBD` 未赛。`AWD`/`WO` 不写比分进拒绝清单。
- 条款：免费档允许的用途以站方当前条款为准，README 标注数据来源。

## 范围
1. `src/zucai/sources/api_football.py`：`ApiFootball(Source)`。
   - `fetch_raw(*, league_id, season=None, date=None)`，原始 JSON 落盘 `data/raw/api-football/`。
   - **配额守卫**：每次请求前读取本地 `data/api_football_quota.json`（UTC 日期、已用次数、最近一次响应头剩余值）；
     剩余 ≤ `--reserve`（默认 10）时拒绝继续并明确报错，已完成部分保留。响应头与本地计数不一致时以响应头为准。
   - 分页：`paging.total > 1` 时逐页请求并计入配额；整季请求通常单页（需验证）。
   - `parse(raw)` 返回 `list[ApiGame]`，复用 T09 的 `api_common.py` dataclass，`provider='apif'`。
2. 赛事表 `data/api_football/leagues.json`：`league_id → league（中文缩写）`，每项带 `seasons_available`（真实请求确认）、`verified_utc`。
3. 队名对齐复用 T08/T09 机制，文件 `data/team_aliases/api_football.json`，键用本源球队数字 ID，三种状态同前。
   这些联赛此前只有 jc 一个来源，交叉验证基础较薄：PR 必须写明每联赛 `auto_verified` 与 `proposed` 的数量，`proposed` 逐条列出。
4. 入库规则与 T09 相同：`match_id` 形如 `apif:{fixture_id}`，`source='apif'`；去重优先官方 jc，其次 `fd:`/`fdorg:` 已有行；只对齐一队进 `data/apif_suspected_duplicates.csv`；
   赛季回填必须显式 `--assume-known-after 3h`，日常 `--date` 增量记 `captured`；预定场次写 `scheduled` 不写结果。
5. CLI：
   - `zucai fetch apif --league 98 --season 2024 [--season 2025] --assume-known-after 3h [--reserve 10]`
   - `zucai fetch apif --date 2026-11-01`（当天全部联赛一次请求，再按 `leagues.json` 过滤）
   - 结束时打印：本次请求数、当日剩余配额、入库场次、`linked_*`、拒绝行、未对齐队名数、`assumed` 行数。
6. fixtures：一个整季 `fixtures?league&season` 响应裁剪 ≤ 20 场（含 `AET`、`PST`、`AWD` 各一）、一个 `fixtures?date` 响应、一个 `paging.total=2` 的构造样本，放 `data/fixtures/t10/`，**检查 token 残留**。
7. 测试：常规时间取 `score.fulltime`、状态映射、配额守卫（含响应头与本地计数不一致）、分页、key 缺失报错、别名三状态、三重去重、必须带开关。

## 不做
- 不接 RapidAPI 通道。
- 不抓赔率端点（`/odds`），即便免费档开放。
- 不抓阵容、事件、统计。
- 不自动定时抓取；加入 `cloud_feeds` 另行决定（key 放 GitHub Actions Secrets，并把每日配额预算写进 workflow 说明）。

## 验收标准
- 真实运行：至少日职、韩职、澳超、瑞超、挪超、芬超六个联赛，各拉免费档允许的全部赛季。PR 描述写明每联赛可用赛季、入库场次、`linked_*`、拒绝行、未对齐队名数，以及总请求数（必须 ≤ 100 次/天，跨天运行写明天数）。
- 与 jc 重叠时段链接率 ≥ 90%（这些联赛 jc 样本少，阈值低于 T08/T09），未链接行逐条列出。
- `zucai train --league 日职 --source jc,apif` 完成回测并与仅 `jc` 并列。
- `docs/DATA_SOURCES.md` 第 6 节、`docs/ARCHITECTURE.md`、README 快速开始同步更新；README 标注数据来源 API-Football。
