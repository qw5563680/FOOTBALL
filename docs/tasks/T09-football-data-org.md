# T09 · 接入 football-data.org v4（欧洲主流联赛全量赛程与赛果）

## 目标
`zucai fetch fdorg` 能按赛事拉取 football-data.org 免费档 12 项赛事的赛程与常规时间赛果，补上两类缺口：
1. 历史赛季的训练样本（与 T08 互补，以 T08 为主、本源为校验与补缺）；
2. **当季所有联赛场次**，而不只是竞彩开售的那部分。Dixon-Coles 的球队强度需要整个联赛的比赛，T03 只覆盖竞彩在售场次。
赔率仍只做对照基准；本源免费档不含赔率，也不需要。

## 背景与约束（2026-11-02 核实，细节见 `docs/DATA_SOURCES.md` 第 5 节）
- 基地址 `https://api.football-data.org/v4/`，请求头 `X-Auth-Token: <key>`，key 只从环境变量 `FOOTBALL_DATA_ORG_TOKEN` 读取。
  key 不进仓库、不进日志、不进 `RawStore` sidecar（sidecar 只记 URL 与状态码，URL 不含 key）。
- 免费档"永久免费"的 12 项赛事：英超 `PL`、英冠 `ELC`、西甲 `PD`、意甲 `SA`、德甲 `BL1`、法甲 `FL1`、荷甲 `DED`、葡超 `PPL`、
  巴甲 `BSA`、欧冠 `CL`、欧洲杯 `EC`、世界杯 `WC`。限速约 10 次/分钟，超限返回 429，带 `X-RequestCounter-Reset` 秒数（需验证）。
- 免费档能回溯多少赛季没有官方说明。实现第一步用真实请求确认 `?season=YYYY` 最早可用年份，写进 DATA_SOURCES。
- 时间字段 `utcDate` 为 UTC ISO，可直接用；比分对象含 `score.duration`（`REGULAR`/`EXTRA_TIME`/`PENALTY_SHOOTOUT`）、
  `fullTime`、`halfTime`，加时赛另有 `regularTime`、`extraTime`、`penalties`（需验证）。**常规时间比分**在 `duration != REGULAR` 时必须取 `regularTime`，
  不得把 `fullTime` 当 90 分钟比分；拿不到 `regularTime` 的加时赛行进拒绝清单。
- `status` 取值 `SCHEDULED/TIMED/IN_PLAY/PAUSED/FINISHED/POSTPONED/SUSPENDED/CANCELLED/AWARDED`（需验证）。
  只有 `FINISHED` 写赛果；`POSTPONED/CANCELLED` 对应已有本源比赛置 `cancelled`；`AWARDED`（判罚结果）不写比分、记拒绝清单。
- `lastUpdated` 是记录修改时间，不是赛果发布时间，**不得**当作路线 B 的 `published`。
- 条款：免费档限非商业使用，需在 README 标注数据来源；只 GET。

## 范围
1. `src/zucai/sources/football_data_org.py`：`FootballDataOrg(Source)`。
   - `fetch_raw(*, competition, season=None, date_from=None, date_to=None, status=None)` 对应
     `GET /competitions/{code}/matches?season=YYYY[&status=FINISHED]` 与 `GET /matches?dateFrom&dateTo&competitions=PL,PD`，原始 JSON 落盘 `data/raw/football-data-org/`。
   - 限速器按响应头动态退避：收到 429 按 `X-RequestCounter-Reset` 等待后重试一次，仍失败则整次命令失败，已落盘文件保留。
   - `parse(raw)` 返回 `list[ApiGame]`（T09/T10 共用的 dataclass，放 `src/zucai/sources/api_common.py`）：
     `provider`、`provider_match_id`、`competition_code`、`league`（中文缩写）、`kickoff_utc`、`status`、`home_raw`、`away_raw`、
     `home_provider_id`、`away_provider_id`、`full`、`half`、`regular_time_basis`（`regular`/`derived`/`missing`）。
2. 赛事表 `data/football_data_org/competitions.json`：`code → league（sporttery 中文缩写）`，每项带 `earliest_season`、`verified_utc`。
   欧冠、欧洲杯、世界杯暂不映射（竞彩标签待核，且非联赛），本任务只处理 9 个联赛。
3. 队名对齐复用 T08 的别名机制，文件 `data/team_aliases/football_data_org.json`，键用**本源球队数字 ID**而不是英文名：
   `{"英超": {"57": {"name": "Arsenal", "canonical": "阿森纳", "status": "auto_verified"}}}`。三种状态与 T08 相同：
   `auto_verified`（同联赛、开球相差 ≤ 3 小时、唯一 jc 比赛且对方也可对齐）、`proposed`（不生效，PR 列出）、`approved`。
   T08 已核实的英文名→中文对照可以作为本源 `proposed` 的来源，但仍要走本源自己的交叉验证。
4. 入库规则：
   - `match_id` 形如 `fdorg:{provider_match_id}`，`source='fdorg'`。
   - 去重优先官方：能对上 jc 比赛（同联赛、两队已对齐、开球相差 ≤ 3 小时）的行不新建、不写赛果，只补 jc 行的 `counterpart_id`（为空时），计 `linked_existing`。
     与 T08 的 `fd:` 比赛也要去重：同联赛、两队对齐、开球相差 ≤ 3 小时的 `fd:` 已存在则跳过，计 `linked_fd`。只对齐一队的进 `data/fdorg_suspected_duplicates.csv`。
   - 赛果可知时间按 T03 路线 B：`--season` 回填必须显式 `--assume-known-after 3h`，否则报错；
     `--days N` 日常增量默认 `captured`（采集时刻）。比分更正、未变保留等沿用 T03 第 3、6 条。
   - 预定场次（`SCHEDULED/TIMED`）写入 `matches`，`status='scheduled'`，供后续 T06 预测整轮联赛，不写 `results`。
5. CLI：
   - `zucai fetch fdorg --competition PL --season 2024 [--season 2025] --assume-known-after 3h`
   - `zucai fetch fdorg --competition PL,PD,SA --days 7`（含未来 7 天赛程与过去 7 天赛果，按 `dateFrom/dateTo` 一次请求）
   - 结束时打印：请求数、剩余配额（若响应头提供）、入库场次、`linked_existing`、`linked_fd`、拒绝行、未对齐队名数、`assumed` 行数。
6. fixtures：一个 `competitions/PL/matches?season=` 响应（裁剪 ≤ 20 场，保留一场加时赛与一场 `POSTPONED`）和一个 `/matches?dateFrom` 响应，放 `data/fixtures/t09/`，**先把真实响应里的任何 token 残留检查干净**。
7. 测试：常规时间取 `regularTime`、状态映射、429 退避、key 缺失时的明确报错、别名三状态、双重去重（jc 与 fd）、必须带开关、预定场次入库不写结果。

## 不做
- 不接欧冠、欧洲杯、世界杯。
- 不抓阵容、射手、积分榜。
- 不把本源数据用于 T06 市场基准。
- 不自动定时抓取；加入 `cloud_feeds` 另行决定（届时 key 放 GitHub Actions Secrets）。

## 验收标准
- 真实运行：9 个联赛各拉当季全量赛程，以及免费档允许的最早赛季到上季的赛果。PR 描述写明每联赛入库场次、`linked_existing`、`linked_fd`、拒绝行、未对齐队名数、最早可用赛季。
- 当季与 jc 重叠时段的链接率 ≥ 95%，未链接行逐条列出。
- 每联赛至少一场加时赛（杯赛不在范围，可用降级附加赛或说明无样本）验证 `regularTime` 逻辑；若免费档无加时样本，PR 明示。
- `zucai train --league 英超 --source jc,fd,fdorg` 完成回测，训练场数、`assumed_training_share`、对数损失与 `jc,fd` 并列。
- `docs/DATA_SOURCES.md` 第 5 节、`docs/ARCHITECTURE.md`、README 快速开始同步更新；README 标注数据来源 football-data.org。
