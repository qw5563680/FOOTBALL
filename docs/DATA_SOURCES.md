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

## 队名对齐

不同来源的队名写法不同（如「曼联」「曼彻斯特联」）。`teams.aliases` 存所有见过的别名，
对齐规则在 `zucai/sources/teams.py` 里维护，未知队名先写入 `data/unmatched_teams.csv` 供人工确认，不要静默猜测。

## 合规

只抓公开页面，遵守各站 robots 和限速，抓取结果仅用于个人分析。
