# 数据源说明

所有接口和页面结构都可能变化。标「需验证」的条目在实现时先抓一次真实响应确认，并把裁剪后的样本放进 `data/fixtures/`。

## 1. 中国竞彩网 sporttery.cn（竞彩官方）

- 在售赛程与各玩法赔率（需验证）：
  `GET https://webapi.sporttery.cn/gateway/jc/football/getMatchCalculatorV1.qry?poolCode=hhad,had,crs,ttg,hafu&channel=c`
  返回 JSON，`value.matchInfoList[]` 按日期分组，每场含 `matchId`、`matchNumStr`（周六001）、
  `leagueAbbName`、`homeTeamAbbName`、`awayTeamAbbName`、`matchDate`、`matchTime`，
  以及 `had`、`hhad`（含 `goalLine`）、`crs`、`ttg`、`hafu` 各玩法赔率对象。
- 已完赛结果（需验证）：
  `GET https://webapi.sporttery.cn/gateway/jc/football/getMatchResultV1.qry?matchPage=1&matchBeginDate=YYYY-MM-DD&matchEndDate=YYYY-MM-DD&leagueId=&pageSize=30&pageNo=1&isFix=0&pcOrWap=1`
  返回 `value.matchResult[]`，含 `matchId`、`sectionsNo999`（全场比分如 `2:1`）、`sectionsNo1`（半场）。
- 请求头需要 `Referer: https://www.sporttery.cn/` 和常见浏览器 UA，否则可能 403。
- 限速：同一接口间隔 ≥ 1 秒。

## 2. 500.com

- 竞彩历史赛程与赛果：`https://trade.500.com/jczq/?date=YYYY-MM-DD`（HTML，需验证）。
- 单场欧赔/亚盘历史：`https://odds.500.com/fenxi/ouzhi-{fid}.shtml`、`.../yazhi-{fid}.shtml`
  （HTML 表格，`fid` 为 500.com 的比赛 ID，与竞彩 `matchId` 不同，需要建立映射，需验证）。
- 14 场胜负彩期次：`https://trade.500.com/sfc/`，历史期次 `?expect=期号`（需验证）。
- 页面编码可能是 GBK，解析前按 `response.apparent_encoding` 处理。

## 3. 体彩官网 lottery.gov.cn

- 传统足彩开奖与期次信息，作为 500.com 的备用来源（需验证接口）。

## 4. football-data.co.uk（历史赛果与收盘赔率，T08）

- 免费、无 key、静态 CSV，站方声明"My data is free"，另有法律免责页面；仅供个人分析，不转发原始文件。请求间隔 ≥ 2 秒。
- 主联赛按季文件：`https://www.football-data.co.uk/mmz4281/{SSSS}/{DIV}.csv`，`SSSS` 为两位起止年份（`2526`），
  `DIV` 为 `E0 E1 E2 E3 EC SC0 D1 D2 I1 I2 SP1 SP2 F1 F2 N1 B1 P1 T1 G1`（2026-11-02 核实英格兰页面，其余需验证）。
  列头以 `Div, Date, Time, HomeTeam, AwayTeam, FTHG, FTAG, FTR, HTHG, HTAG, HTR` 开头，`Date` 为 `dd/mm/yyyy`，
  收盘赔率列在普通赔率列名的庄家前缀后加 `C`，如 `B365CH`、`PSCH`、`AvgCH`、`MaxCH`。
- 其他联赛合并文件：`https://www.football-data.co.uk/new/{CCC}.csv`，一个文件含全部赛季（2026-11-02 核实 `JPN.csv` 自 2012 起）。
  列：`Country, League, Season, Date, Time, Home, Away, HG, AG, Res, PSCH, PSCD, PSCA, MaxCH, MaxCD, MaxCA, AvgCH, AvgCD, AvgCA, BFECH, ..., B365CH, ...`。
  `CCC` 候选：`JPN SWE NOR FIN USA BRA CHN DNK AUT SWZ POL MEX ARG IRL ROU RUS`（需验证）。
- `Time` 的时区基准无官方说明，必须用当季文件与库中 jc 场次比对后逐联赛写进 `data/football_data_co_uk/leagues.json`。
- 队名为英文，与 sporttery 中文缩写的对齐只走 `data/team_aliases/football_data_co_uk.json`，规则见 T08 任务单。

## 5. football-data.org v4（欧洲主流联赛全量赛程与赛果，T09）

- 基地址 `https://api.football-data.org/v4/`，请求头 `X-Auth-Token`，key 来自环境变量 `FOOTBALL_DATA_ORG_TOKEN`，不进仓库、日志与 sidecar。
- 免费档 12 项赛事永久免费：`PL ELC PD SA BL1 FL1 DED PPL BSA CL EC WC`（2026-11-02 核实 coverage 页）；约 10 次/分钟，超限 429（需验证响应头 `X-RequestCounter-Reset`）。
  免费档可回溯的最早赛季无官方说明，实现时用真实请求确认。
- 端点：`GET /competitions/{code}/matches?season=YYYY[&status=FINISHED]`、`GET /matches?dateFrom&dateTo&competitions=`。
- 时间 `utcDate` 为 UTC ISO。比分含 `score.duration`，加时赛时常规时间取 `regularTime`，不得用 `fullTime`（需验证字段）。
- `lastUpdated` 是记录修改时间，不是赛果发布时间，不能作为路线 B 的 `published`。
- 免费档限非商业使用，README 须标注来源。

## 6. API-Football v3 / api-sports.io（亚洲与北欧联赛，T10）

- 基地址 `https://v3.football.api-sports.io/`，请求头 `x-apisports-key`，key 来自环境变量 `API_FOOTBALL_KEY`，不进仓库、日志与 sidecar。
- 免费档 100 次/天，UTC 零点重置，响应头 `x-ratelimit-requests-remaining`（需验证）。第三方资料称免费档仅开放部分历史赛季，实现时逐联赛用真实请求确认。
- 端点：`GET /fixtures?league={id}&season={YYYY}`（整季一次请求）、`GET /fixtures?date=`、`GET /leagues`。
  联赛 ID 候选（需验证）：日职 98、韩职 292、澳超 188、瑞超 113、挪超 103、芬超 244、丹超 119、美职足 253、中超 169。
- 时间优先 `fixture.timestamp`（Unix UTC）。常规时间比分只取 `score.fulltime`；`goals` 可能含加时（需验证）。
- 状态 `fixture.status.short`：`FT/AET/PEN` 完赛，`PST/CANC/ABD` 延期或取消，`AWD/WO` 判罚不写比分，`NS/TBD` 未赛。
- 用途以站方当前条款为准，README 须标注来源。

## 队名对齐

不同来源的队名写法不同（如「曼联」「曼彻斯特联」）。`teams.aliases` 存所有见过的别名，
对齐规则在 `zucai/sources/teams.py` 里维护，未知队名先写入 `data/unmatched_teams.csv` 供人工确认，不要静默猜测。

## 合规

只抓公开页面，遵守各站 robots 和限速，抓取结果仅用于个人分析。
