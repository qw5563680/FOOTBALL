# 数据源说明

所有接口和页面结构都可能变化。标「需验证」的条目在实现时先抓一次真实响应确认，并把裁剪后的样本放进 `data/fixtures/`。

## 1. 中国竞彩网 sporttery.cn（竞彩官方）

- 在售赛程与各玩法赔率（2026-11-01 已核实，中国竞彩网当前计算器的接口）：
  `GET https://webapi.sporttery.cn/gateway/uniform/football/getMatchCalculatorV1.qry?poolCode=had,hhad,crs,ttg,hafu&channel=c`
  返回 JSON，`value.matchInfoList[]` 按日期分组，每场含 `matchId`、`matchNumStr`（周六001）、
  `leagueAbbName`、`homeTeamAbbName`、`awayTeamAbbName`、`matchDate`、`matchTime`，
  以及 `had`、`hhad`（含 `goalLine`）、`crs`、`ttg`、`hafu` 各玩法赔率对象。
  `--date` 按组内 `businessDate`（竞彩编号日）筛选。开球使用北京时间
  `matchDate + matchTime`，跨午夜时两日期可以不同。按 `poolList` 的具体玩法、固定奖、
  在售及单关/过关字段核对，采集时已经开赛的比赛不记作赛前。
  `ttg.s7` 是 7 球及以上。比分的 `s1sh/s1sd/s1sa` 是胜其他/平其他/负其他，
  入库选项分别为 `5+`、`draw_other`、`away_other`。字段尾部的 f 是变化标记，不是报价。
  原始快照每次保留；相同市场十分钟内原价重采不重复入库，变价时整组同时记录，
  以便还原同一时刻的完整赔率。报价时间使用实际采集时间，源站更新时间随原件保存。
- 已完赛结果（2026-11-01 核实，旧路径返回 HTTP 403）：
  `GET https://webapi.sporttery.cn/gateway/uniform/football/getUniformMatchResultV1.qry?matchBeginDate=YYYY-MM-DD&matchEndDate=YYYY-MM-DD&leagueId=&pageSize=30&pageNo=1&isFix=0&matchPage=1&pcOrWap=1`。
  按北京时间开球日期逐日调用，用返回的 pageNo、pages、total 核对全部数据。
  完赛只取已开奖记录的 sectionsNo999（常规时间）和 sectionsNo1（半场）。
  Refund、取消、延期、无效做状态更新；待开奖不产生比分。表级 lastUpdateTime 不能当作某场比分的发布时间。
  结果列表通常没有开球时分。按同场 sportteryMatchId 请求
  `https://webapi.sporttery.cn/gateway/uniform/football/getMatchHeadV1.qry?source=web&sportteryMatchId=ID`，
  核对主客队体彩ID与日期，读取 matchDateTime。详情里的另一个 matchId 属于供应商，不用作竞彩主键。
- 请求头需要 `Referer: https://www.sporttery.cn/` 和常见浏览器 UA，否则可能 403。
- 限速：同一接口间隔 ≥ 1 秒。

## 2. 500.com

- 竞彩历史比分备用页（2026-11-01 核实）：`https://trade.500.com/jczq/?playid=271&g=2&date=YYYY-MM-DD`。
  `--source c500` 解码 GBK，读取比分玩法已结算的精确选项。页面只给“其他”时保留未知；
  当日赛果按实际开球日筛选，需同时检查前一竞彩编号日的跨午夜场次。该页没有精确半场比分。
  备用场次默认用 c500:fixtureid。若页面另外给出的体彩ID、主客队、编号及开球时刻与已有官方场次同时相符，
  可补入该官方场次；较低等级来源不能覆盖已有官方赛果。
- 单场欧赔/亚盘历史：`https://odds.500.com/fenxi/ouzhi-{fid}.shtml`、`.../yazhi-{fid}.shtml`
  （HTML 表格，`fid` 为 500.com 的比赛 ID，与竞彩 `matchId` 不同，需要建立映射，需验证）。
- 14 场胜负彩期次（2026-11-01实际核实）：`https://trade.500.com/sfc/`，指定期次 `?expect=26155`。
  静态GB2312/GB18030页面无需运行JS。读 `input#expect`、`input#buyendtime` 和
  `table#vsTable tr[data-cid]` 的1–14顺序；主客读 `a.team-l/team-r`，联赛读 `td-evt`，
  时间读 `td-endtime`，fixture ID读 `/fenxi/shuju-ID.shtml`。不能把 `data-pjgl` 平均概率或
  `data-bjpl` 报价当球队模型。请求指定期号必须核对返回期号，缺行/重复/身份字段不全整期拒绝。
  当前26156页 `buyendtime=2026-11-02 22:00:00`，26155历史页为空；空值存null，不用开球时间代替。
  页面写“官方截止”仍仅是第三方转述，未验证官方销售资格或网点出票时刻。
  开球仅有月日时分：有完整截止时按其前7天至后30天唯一年份解释，跨年可处理；无截止时以期号
  YY对应2000+YY作年份，并披露 `issue_year_assumed`；无完整截止且涉及1月/12月或跨度超过30天则拒绝。
  实际robots读取未禁止 `/sfc/`；同接口请求间隔至少1秒。原字节和读取时刻保留，不抓登录或购票路径。
- T07比赛ID对照（2026-11-01实际核实）：仅GET公开根路径 `https://trade.500.com/jczq/`，
  不发送robots禁止的 `/jczq/?` 历史查询。行字段 `data-fixtureid/data-id` 连接500比赛ID与体彩ID，
  `data-homeid/data-awayid` 必须与SFC球队链接 `/team/ID/` 方向一致；开球时刻也须一致。
  再检查已有官方Match的联赛和开球。当前页不覆盖的历史场次只能使用已有counterpart或精确身份匹配。
  每条实际使用的ID对照携带URL、读取UTC和原件SHA256；不根据相似简称猜球队，不在查找阶段修改别名。
  同一期再次采集时，当前页已移除的场次可复用旧snapshot中的对照；保留旧读取时刻并重新校验，
  新旧主客球队ID或体彩ID冲突时拒绝。当前页自身获取/解析失败仍返回失败，不伪装为成功。
  先有SFC简称记录、后有ID对照但竞彩记录尚未入库时，保留原c500身份并追加counterpart，仍标unmatched；
  不以相同队名/开球去覆盖已明确指向另一体彩ID的对照。归档对照与新开球冲突仍须核实后再导入。
  已核实联赛对照仅 `瑞典超 -> 瑞超`（fixture1362837/jc2042440）；原始标签与官方标签分别保留。
  实抓根页面广告含非GB字节，解码允许替换无关内容，但所消费的身份字段出现替换字符就拒绝。
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
## T04特征提取的数据可知边界（2026-11-02）

本任务不新增联网采集；只读T03/T06已归档的公开赛程、赛果及had快照。
验收库有866场比赛、833个赛果、3450条赔率，赛果的captured时刻集中于
2026-11-01 13:03:07–13:26:51 UTC（北京时间21:03:07–21:26:51）。这些是实际采集时刻，
不能改成历史开球后固定时间来填满回测特征。结果表没有独立发布时间的行，不能声称当时已经可用。

提取器要求历史开球和赛果available_utc都严格早于目标的min(as_of,kickoff)，
没有可知时间的赛果排除；NULL旧来源标记仅在已有时间时兼容为captured。
assumed默认可用但逐窗披露，strict模式排除。球队近况/交锋限同来源、同联赛，
跨赛季不自动重置；休息间隔可能漏掉库里没有的杯赛或未完赛比赛，不能当完整赛程证明。
市场列只能来自同一完整had快照，line固定0；hhad不混入。数据仅供既有个人分析用途，
代码依赖的许可不授予额外的数据再分发权。验收文件仅保存在既有私有项目仓库。

完整计数、缺失比例及限制见[实际验收](../reports/2026-11-02-t04-acceptance/README.md)。

## 队名对齐

不同来源的队名写法不同（如「曼联」「曼彻斯特联」）。`teams.aliases` 存所有见过的别名，
对齐规则在 `zucai/sources/teams.py` 里维护，未知队名先写入 `data/unmatched_teams.csv` 供人工确认，不要静默猜测。

## 合规

只抓公开页面，遵守各站 robots 和限速，抓取结果仅用于个人分析。
