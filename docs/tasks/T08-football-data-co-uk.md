# T08 · 接入 football-data.co.uk 历史赛果（训练数据扩充）

## 目标
`zucai fetch fd` 能把 football-data.co.uk 公开 CSV 里的多季常规时间赛果导入数据库，作为 Dixon-Coles 的训练历史，
使每个联赛模型的训练样本从目前的 61–74 场扩大到多季；同时把文件自带的收盘赔率原样归档，供后续回测对照。
该来源不需要 key、不需要登录，Codex 云端环境白名单需加 `www.football-data.co.uk`。

## 背景与约束
- 来源只提供历史赛果和无时间戳的收盘赔率，不提供在售赔率，不参与任何预测输入。赔率仍只做对照基准（ARCHITECTURE 第二条原则）。
- 两类文件（2026-11-02 已核实 URL 与列名，细节见 `docs/DATA_SOURCES.md` 第 4 节，仍标「需验证」的项目实现时先抓真实文件确认）：
  1. 主联赛按季文件 `https://www.football-data.co.uk/mmz4281/{SSSS}/{DIV}.csv`，`SSSS` 为两位起止年份如 `2526`，
     `DIV` 如 `E0`（英超）、`E1`（英冠）、`SP1`、`I1`、`D1`、`F1`、`N1`、`P1`、`B1`、`T1`、`G1`、`SC0`。
     列：`Div, Date(dd/mm/yyyy), Time(HH:MM), HomeTeam, AwayTeam, FTHG, FTAG, FTR, HTHG, HTAG, HTR, ...` 后接大量统计与赔率列。
  2. 其他联赛合并文件 `https://www.football-data.co.uk/new/{CCC}.csv`，一个文件含该联赛全部赛季，
     `CCC` 如 `JPN`、`SWE`、`NOR`、`FIN`、`USA`、`BRA`、`CHN`、`DNK`、`AUT`、`SWZ`、`POL`、`MEX`、`ARG`、`IRL`、`ROU`、`RUS`（需验证哪些存在）。
     列：`Country, League, Season, Date, Time, Home, Away, HG, AG, Res, PSCH, PSCD, PSCA, MaxCH, MaxCD, MaxCA, AvgCH, AvgCD, AvgCA, BFECH, ..., B365CH, ...`。
- `Time` 列的时区基准没有官方说明。实现时必须用 2026/27 当季文件与库中已有 jc 场次（同联赛、同两队）比对，
  逐个 `DIV/CCC` 确定是英国时间还是当地时间，结论写进 `docs/DATA_SOURCES.md`；无法确定的文件不入库。
- 队名是英文（如 `Man United`、`Nott'm Forest`），与 sporttery 的中文缩写不同。**不得按相似度猜队名**，对齐只能走下文的别名文件。
- 条款：站方声明数据免费，另有法律免责页面；仅供本项目个人分析，不转发原始文件。请求间隔 ≥ 2 秒，只 GET，带常见 UA。

## 范围
1. `src/zucai/sources/football_data.py`：`FootballDataCoUk(Source)`。
   - `fetch_raw(*, div=None, season=None, country=None)` 下载一个 CSV，经 `RawStore` 落盘到 `data/raw/football-data-co-uk/`，sidecar 记 URL、时刻、HTTP 状态、SHA256。
   - `parse(raw)` 返回 `list[FdGame]`，dataclass 字段：`div_code`、`season`、`kickoff_utc`、`home_raw`、`away_raw`、`full: (int,int) | None`、`half`、`closing: dict[bookmaker, (h,d,a)]`。
     缺 `Time` 或缺比分（未赛/延期行）的记录进 `data/fd_rejected_rows.csv` 并计数，不入库。
   - 文件内 `Date+Time` → UTC 的转换按上文核实出的每个 `DIV/CCC` 时区基准做，基准表放 `data/football_data_co_uk/leagues.json`，
     每项含 `div_code`、`league`（sporttery 中文缩写，如 `英超`）、`time_basis`（`Europe/London` 或当地 IANA 时区）、`verified_utc`、`verified_by`（jc 比对场次数）。
     未 `verified` 的联赛 `fetch fd` 直接拒绝。
2. 队名对齐 `data/team_aliases/football_data_co_uk.json`：`{"英超": {"Man United": "曼联", ...}}`，每条带 `status`：
   - `auto_verified`：该英文队名所在的 fd 行，按（联赛，开球相差 ≤ 3 小时）在库中找到**恰好一场** jc 比赛，且对方球队也已对齐或同样可对齐，由 Codex 跑比对脚本自动写入；
   - `proposed`：Codex 根据常识给出但无法用 jc 数据交叉验证，**不生效**，PR 描述里逐条列出供审查方改为 `approved`；
   - `approved`：人工审查通过，生效。
   生效的别名通过 T01 的 `upsert_team` 把英文名追加到对应 Team 的 `aliases`；不生效的英文名新建英文 Team 并照旧写 `data/unmatched_teams.csv`（source 填 `fd`）。
3. 入库规则：
   - `match_id` 形如 `fd:E0:20250815:liverpool:bournemouth`（Div、开球日期、两队 slug），`source='fd'`，`league` 用 `leagues.json` 的中文缩写，`status='finished'`。
   - **去重优先官方**：若同联赛、两队都已对齐、开球相差 ≤ 3 小时的 jc 比赛已存在，不新建 fd 比赛、不写 fd 赛果，只在 `matches.counterpart_id` 为空时把 jc 行的 `counterpart_id` 记为该 fd id，并计入 `linked_existing`。
     若只有一队能对齐而另一队不能、且同联赛同时段存在 jc 比赛，记入 `data/fd_suspected_duplicates.csv`，不入库。
   - `results` 走 T03 路线 B：`fetch fd` **必须**显式带 `--assume-known-after 3h`（延时可调），`available_source='assumed'`；不带开关直接报错退出，避免写入 `captured=下载时刻` 这种对回测无用的行。
     重复下载同一文件时沿用 T03 第 3、6 条：比分未变保留原依据，比分更正记 `captured` 并保留原快照。
   - 收盘赔率存新表 `closing_odds(match_id, bookmaker, pool='had', h, d, a, basis='publisher_closing')`，无时间戳列；
     只归档 `PSC*`（Pinnacle）、`B365C*`、`AvgC*`、`MaxC*` 四组，缺任一项的行不写该组。本任务**不**让 T05/T06 读取这张表。
4. CLI：
   - `zucai fetch fd --div E0 --season 2526 [--season 2425 ...] --assume-known-after 3h`
   - `zucai fetch fd --country JPN --from-season 2019 --to-season 2026 --assume-known-after 3h`
   - 结束时打印：下载文件数、入库场次、`linked_existing`、拒绝行数、未对齐英文队名数、`assumed` 行数。
5. 让 T05/T06 能用上这批数据（最小改动）：
   - `read_history` 的 `source` 参数接受逗号分隔的多个来源（如 `jc,fd`），训练命令 `--source jc,fd`；
     由于第 3 条已保证 fd 不与 jc 重复，这里不需要再去重，但要在抛错信息里保留「多个联赛」的检查。
   - 模型元数据把单值 `source` 改为列表 `sources`，旧模型文件读取时视为单元素列表；
   - T06 的「模型来源必须等于比赛来源」放宽为「比赛来源 ∈ 模型 `sources`」，`forecast.json` 的 `models[].source` 同步改为 `sources`，报告渲染跟随。
6. fixtures：一个主联赛文件、一个合并文件，各裁剪 ≤ 30 行放 `data/fixtures/t08/`，保留原始列头与一行含空 `Time` 的异常行。
7. 测试：解析与时区转换、别名三种状态的生效规则、去重优先官方（含「只对齐一队」进可疑清单）、必须带开关、重复导入不改已有依据、`closing_odds` 归档、`--source jc,fd` 训练与 T06 来源集合判断。

## 不做
- 不接其他 API（football-data.org、API-Football 另立 T09/T10）。
- 不让 T05 回测或 T06 报告使用 `closing_odds`，不改市场基准逻辑。
- 不做跨源比分冲突仲裁：jc 已有赛果的比赛 fd 一律跳过。
- 不做半场之外的任何统计列（射门、角球、红黄牌）。
- 不自动定时抓取；加入 `cloud_feeds` 归档另行决定。

## 验收标准
- 真实运行：至少 `E0、SP1、I1、D1、F1` 各最近 5 个完整赛季加当季，以及库中已有 jc 数据的其他联赛（如 `JPN`、`SWE`、`FIN`）。
  PR 描述写明每个联赛的入库场次、`linked_existing`、拒绝行、未对齐队名数，以及 `leagues.json` 里每个联赛的时区结论和比对场次数。
- 对库中已有 jc 数据的时间段，fd 行与 jc 比赛的链接率 ≥ 95%，未链接的行逐条列在 PR 里。
- `proposed` 状态的别名全部列在 PR 描述里等待审查；没有任何别名是按相似度自动生成的。
- `zucai train --league 英超 --source jc,fd` 可在真实库上完成回测，PR 贴出训练场数、`assumed_training_share` 和对数损失，与仅 `--source jc` 的结果并列。
- `docs/DATA_SOURCES.md` 第 4 节、`docs/ARCHITECTURE.md` 数据模型（`closing_odds`、`sources`）、README 快速开始同步更新。
