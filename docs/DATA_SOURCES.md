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

- 无 key、静态 CSV；用途仍受站方条款约束，不能把免费读取视为任意自动访问或再分发授权。
  2026-11-02 读取的 robots 禁止 AI 客户端自动访问；用户随后在本聊天明确确认已获站方许可，才继续本次下载。
  许可状态为 user-confirmed，尚未独立核阅书面依据。只 GET，请求开始间隔至少2秒，不转发完整原始文件。
  [来源说明](https://www.football-data.co.uk/data.php) · [字段说明](https://www.football-data.co.uk/notes.txt)。
- 主联赛按季文件：`https://www.football-data.co.uk/mmz4281/{SSSS}/{DIV}.csv`，`SSSS` 为两位起止年份（`2526`），
  `DIV` 为 `E0 E1 E2 E3 EC SC0 D1 D2 I1 I2 SP1 SP2 F1 F2 N1 B1 P1 T1 G1`（2026-11-02 核实英格兰页面，其余需验证）。
  列头以 `Div, Date, Time, HomeTeam, AwayTeam, FTHG, FTAG, FTR, HTHG, HTAG, HTR` 开头，`Date` 为 `dd/mm/yyyy`，
  收盘赔率列在普通赔率列名的庄家前缀后加 `C`，如 `B365CH`、`PSCH`、`AvgCH`、`MaxCH`。
- 其他联赛合并文件：`https://www.football-data.co.uk/new/{CCC}.csv`，一个文件含全部赛季（2026-11-02 核实 `JPN.csv` 自 2012 起）。
  列：`Country, League, Season, Date, Time, Home, Away, HG, AG, Res, PSCH, PSCD, PSCA, MaxCH, MaxCD, MaxCA, AvgCH, AvgCD, AvgCA, BFECH, ..., B365CH, ...`。
  `CCC` 候选：`JPN SWE NOR FIN USA BRA CHN DNK AUT SWZ POL MEX ARG IRL ROU RUS`（需验证）。
- `Time` 的时区基准无官方说明，必须用当季文件与库中 jc 场次比对后逐联赛写进 `data/football_data_co_uk/leagues.json`。
- 队名为英文，与 sporttery 中文缩写的对齐只走 `data/team_aliases/football_data_co_uk.json`，规则见 T08 任务单。

### T08 实际核验（北京时间 2026-11-02）

已下载主文件 `E0 E1 SP1 I1 D1 D2 F1 F2 N1 P1` 的2627样本；除D2外各再下载2122–2526。
已下载合并文件 `JPN SWE NOR FIN USA BRA`，导入按 Season 起始年2021–2026筛选。
其余代码仍未核验。主文件最终URL会重定向到无 www 的同站地址，回放仅接受这两种相同路径。
验收初次下载时只限制文件请求，内部重定向未等待；审查发现后改为每一跳单独限速并留原件。
修复后实际复抓E0/2122，302到200两跳最小单调时钟间隔2.0秒、CSV哈希与原件一致。
这次复核不改变早期下载记录，完整证据见验收中的 `redirect_smoke.json`。
JPN当前标记为 `2026/2027`，保留该原值，不改写为2026。合并文件 Country/League 的边缘空格被清理后核验。

| 代码 | 联赛 | Time 基准 | 精确UTC比对场数 | 说明 |
| --- | --- | --- | ---: | --- |
| E0 | 英超 | Europe/London | 54 | 验证通过 |
| E1 | 英冠 | Europe/London | 30 | 验证通过 |
| SP1 | 西甲 | Europe/London | 69 | 验证通过 |
| I1 | 意甲 | Europe/London | 70 | 验证通过 |
| D1 | 德甲 | Europe/London | 54 | 验证通过 |
| D2 | 德乙 | 未确定 | 0 | 同时段候选歧义，无唯一锚点，禁止导入 |
| F1 | 法甲 | Europe/London | 33 | 验证通过 |
| F2 | 法乙 | Europe/London | 5 | 单场身份仍待核对 |
| N1 | 荷甲 | Europe/London | 27 | 验证通过 |
| P1 | 葡超 | Europe/London | 22 | 样本内与 Europe/Lisbon 等价，不能识别唯一地理时区 |
| JPN | 日职 | Europe/London | 28 | 验证通过 |
| SWE | 瑞超 | Europe/London | 18 | 另1场差15分钟，仅身份链接，不算精确时区证据 |
| NOR | 挪超 | Europe/London | 17 | 验证通过 |
| FIN | 芬超 | Europe/London | 11 | 验证通过 |
| USA | 美职 | Europe/London | 16 | 3场尚未唯一对齐 |
| BRA | 巴甲 | Europe/London | 27 | 另1场落在英国回拨歧义时刻，拒绝猜测fold |

以上为本批逐场实证的时间转换规则，站方没有公布 Time 的基准；用于历史季还含规则跨季稳定假设。
P1 导入逐行再次检查 London/Lisbon 是否仍给出同一UTC，遇到差异拒绝。所有代码拒绝夏令时不存在或二义时刻。
离线核验脚本 `examples/t08_verify_identity.py` 对候选时区分别建立双向唯一的±3小时有向比赛图；
只由身份一致的唯一配对逐步传播别名，不使用名称相似度或比分挑选。比分只用于匹配后的反证。
至少两场UTC完全相同且优于非等价候选时区才能标 verified；冲突别名不部分激活。
249条 auto_verified 别名有SHA与jc ID证据，没有 proposed 条目。完整证据、未知英文名称和未匹配场次见
[T08验收](../reports/2026-11-02-t08-acceptance/README.md)。英文未对齐历史球队仍可建独立身份，不能据此声称跨源全部统一。

`PSC* / B365C* / AvgC* / MaxC*` 仅在完整且有限、大于1时存 `closing_odds`。
本轮字段说明提示2025-07-23之后Pinnacle报价可能失准；保留原件，不用于模型或正式市场基准。
正式回测仍只读有赛前时间戳的普通had快照，缺少配对市场样本时指标为null。
CLI必须显式启用 `--assume-known-after`；重复相同比分保留原可知时间，更正使用新采集时刻，旧快照不能倒写。
未来结果、空比分、空Time、CSV结构或身份冲突均保留拒绝原因，不能转为0:0或午夜开球。

## 5. football-data.org v4（欧洲主流联赛全量赛程与赛果，T09）

- 基地址 `https://api.football-data.org/v4/`，请求头 `X-Auth-Token`；key 仅从运行时环境变量 `FOOTBALL_DATA_ORG_TOKEN` 读取，不进命令、仓库、日志或 sidecar。
- 本源只映射九个国内联赛 `PL ELC PD SA BL1 FL1 DED PPL BSA`；欧冠、欧洲杯、世界杯不入库。免费档限非商业使用，README 标注来源。
- 端点：`GET /competitions/{code}/matches?season=YYYY[&status=FINISHED]`、`GET /matches?dateFrom&dateTo&competitions=`。
- `utcDate` 为 UTC ISO；`dateTo` **排他**。真实接口单次日期跨度最多 **10 天**，超出返回 HTTP 400 `Specified period must not exceed 10 days.`。
  `--days N` 的 `2N+1` 天自动拆成不超过 10 天的连续窗口，端点无重叠/无空档；如 UTC 2026-11-02 的 `--days 7` 为 `[2026-10-26,2026-11-05)`、`[2026-11-05,2026-11-10)`。
  每窗单独归档与提交；`--raw-file` 一次回放一窗，按原 `--days` 规划验证 URL、HTTP、SHA256。新增 sidecar 字段 `window_anchor_date` 保留起算 UTC 日期，采集跨午夜时仍可复原原窗口。
- 正常限速每 6 秒一次，429 按 `X-RequestCounter-Reset` 秒数等待后只重试一次；缺头默认 60 秒，非法重置值直接失败。
  **最大允许等待 900 秒（15 分钟）**，超过直接报错退出，不等待或提前重试，已归档响应保留。
- `FINISHED` 的常规时间取 `fullTime`；`duration=EXTRA_TIME/PENALTY_SHOOTOUT` 必须取 `regularTime`，不得把加时或点球比分当 90 分钟，也不反推。
  缺少有效常规时间比分或半场比分超过常规时间比分的行拒绝；`AWARDED` 拒绝。
  `SCHEDULED/TIMED` 只建赛程；`POSTPONED/CANCELLED` 只处理已有本源赛程；其余进行中状态不产生赛果。
- `lastUpdated` 是修改时间，不是赛果发布时间，不能作为路线 B 的 published。季回填必须显式 `--assume-known-after 3h`；日常增量用真实采集时刻。
- 默认配置 `data/football_data_org/competitions.json` 九联赛均为 `earliest_season=2023`，`verified_utc` 为各联赛实测拒绝边界的 UTC。
  `data/team_aliases/football_data_org.json` 正式保存 172 条 `auto_verified`，每条保留本源数字 ID、规范名和包含两端比赛 ID、UTC、原件 SHA256 的证据。
  导入器默认读取该文件；proposed 不生效，可通过 `--aliases-file` 显式替换。
- 未对齐球队写 `data/fdorg_unmatched_teams.csv`：`league,team_id,english_name,first_seen_utc`，以联赛/ID 去重并保留最早真实观察 UTC。
  早期原件后回放会更新最早时间；`--audit-dir` 可改目录。该清单、拒绝与疑似重复清单均被忽略，不作自动猜名。

### T09 带凭据核验记录（北京时间 2026-11-02）

- 云端先行 PL 实测：起始年 2026、2025、2024、2023 各 HTTP 200 / 380 场；2022 及抽查的 2021–2016 为 403。
  九联赛后续均成功取得 2023–2026，向前至 2022 均 403。本账户最早确认可读的欧洲赛季为 **2023/24**，巴甲为 **2023 自然年**。
  这是本账户在本轮的实测边界，不代表所有套餐，亦未穷举每个更早年份；当前季为全赛程，历史季筛 FINISHED。
- 2026-11-02 **02:59:34.786 北京时间**（2026-11-01 18:59:34.786 UTC）真实 HTTP 429：
  `X-RequestCounter-Reset: 52`、`X-Requests-Available-Minute: 0`，无 `Retry-After`。此前匿名 403 的 `86400` 不能作为该账户 429 的证据，也不会导致等待整天。
- 首轮 36 份成功整季响应共 13,196 行；当季 3,296 行。旧版采集脚本末尾 15 天窗口真实 400，补采 `[2026-10-28,2026-11-06)` 9 天窗口 200；本轮已修复 CLI 与采集脚本自动分窗。
- 英冠真实 `fdorg:536448`（2024 季）返回 `EXTRA_TIME`、`fullTime=1:1`、`regularTime=0:1`、`halfTime=0:0`，解析为常规时间 0:1。
  英冠 `fdorg:557838` 的 `regularTime=0:0` 与 `halfTime=1:1` 冲突，已拒绝；其余八联赛取得的 2023–2026 样本中未观察到加时/点球。
- 三个真实裁剪件位于 `data/fixtures/t09/live_*.json`，来源、UTC、原件与裁剪 SHA256 在同目录 `provenance.json`；`synthetic_*` 继续明确标为合成异常场景。
  本轮提交前再次检查 token 的原始字节与 JSON 解码回显。pytest 只读离线样本，不使用真实 key 联网。
- 逐联赛实际入库、链接率、未链接名单、部分重建基线限制及同截点回测见 [T09 验收](../reports/2026-11-02-t09-acceptance/README.md)。
  来源署名及使用条件以 [站方条款](https://www.football-data.org/client/register) 为准；API `robots.txt` 本轮 HTTP 404。

## 6. API-Football v3 / api-sports.io（亚洲与北欧联赛，T10）

北京时间 2026-11-02（实际采集 UTC 2026-11-01）在本账户完成带凭据验证。
基地址 `https://v3.football.api-sports.io/`，只支持 api-sports.io 直连；头为
`x-apisports-key`，值只读运行环境 `API_FOOTBALL_KEY`。不访问赔率、事件、阵容或统计端点。

### 账户、赛季与日期权限实测

`/status` 返回 Free、active=true、limit_day=100。`/leagues?id=` 能列出多个赛季，
但这个元数据列表**不代表账户读取权限**。九联赛逐个请求 2021–2026，日职另测元数据标为
current 的 2027；2022–2024 全部取得整季，2021/2025/2026（及日职2027）均为
HTTP 200 且 `errors.plan` 明确提示仅允许 2022–2024。年份用赛季起始年，澳超、丹超为跨年赛季。
本轮 27 份整季原件均 `paging.current=1,total=1`，共 7,059 行，不裁成竞彩开售子集。

|ID/规范联赛|元数据 current 起始年|整季实际可用年份|current 整季|
|---|---:|---|---|
|98/日职|2027|2022、2023、2024|拒绝，HTTP 200 + errors.plan|
|292/韩职|2026|2022、2023、2024|拒绝，HTTP 200 + errors.plan|
|188/澳超|2026|2022、2023、2024|拒绝，HTTP 200 + errors.plan|
|113/瑞超|2026|2022、2023、2024|拒绝，HTTP 200 + errors.plan|
|103/挪超|2026|2022、2023、2024|拒绝，HTTP 200 + errors.plan|
|244/芬超|2026|2022、2023、2024|拒绝，HTTP 200 + errors.plan|
|119/丹超|2026|2022、2023、2024|拒绝，HTTP 200 + errors.plan|
|253/美职|2026|2022、2023、2024|拒绝，HTTP 200 + errors.plan|
|169/中超|2026|2022、2023、2024|拒绝，HTTP 200 + errors.plan|

`/fixtures?date=2024-05-18` 返回 HTTP 200 + `errors.plan`，提示允许 UTC 日期
2026-10-31 至 2026-11-02。随后三天分别取得 1,568、1,187、229 行，全为单页；
日端点覆盖所有赛事，再按配置联赛过滤。**整季当季不可用，但最近三天增量可用**，两者分开披露。
窗口随账户和运行日期改变，不能把本轮日期当作永久权限规则；未来实际 API 错误仍中止，不能当空成功。
配置及逐请求 SHA256、核验 UTC 见 `data/api_football/leagues.json`。

### 配额和安全归档

本轮共 69 次 APIF GET，均在同一 UTC 日；本地日计数69，最后一个有效每日头为62。
有些权限拒绝无配额头，且每日头观察到向上跳变；本地请求数不等同于服务端已收费数。
实际成功响应带 `x-ratelimit-requests-limit: 100`、`x-ratelimit-requests-remaining`；
`x-ratelimit-limit: 10`、`x-ratelimit-remaining` 是分钟额度，不能用来当每日剩余。
UTC 零点重置是本地计数规则，本轮没有跨午夜实测服务端重置。

每次请求前在 `data/api_football_quota.json` 持久预占，包括权限错误及网络失败；
线程锁和进程文件锁覆盖整个 GET。每日头是剩余额度的权威值；缺头则延用保守本地扣减，
本地实际发送数另有100的硬上限。剩余 ≤ `--reserve`（默认10）停止并报错，损坏配额文件也报错。
文件只适用于使用同一持久目录的采集进程；其他机器使用同一 key 的消耗靠下一次响应头反映，不能实时共享。
不隐式重试或跟随重定向，默认间隔6.1秒。429 中止；每个后续 GET 都重新过守卫。

原始 JSON 和 sidecar 仅写 `data/raw/api-football/`，请求头不落盘。复用 T09
`safe_json_for_archive`，检查原始字节、完整 UTF-8 JSON 的键和值、JSON 转义和重复键覆盖前的值。
非 JSON、截断 JSON 或 key 回显拒绝归档；即使正文不安全，合法数字配额头仍会被记录。
真实裁剪 fixture 在导出前后再检查，原件/裁剪件 SHA256 写进 `data/fixtures/t10/provenance.json`。

多页时原件逐页保存并计数；所有页面齐全后生成 `assembled-fixtures`（附逐页 URL/UTC/SHA256），
以最后一页采集时刻保守作为整个集合的 captured 下界，再一次核验身份并入库，防止第一页抢占歧义链接。
后续页失败保留已下载原件，整个未完成范围不入库；更早已完成赛季保持提交。单独多页片段不得离线入库。

### 字段和身份边界

时间仅取 `fixture.timestamp`（Unix UTC），不从可能改格式的显示字符串推断。
常规时间比分只取 `score.fulltime`，半场取 `score.halftime`。真实韩职 `apif:1317866`
（2024 季）`fulltime=2:1`、`extratime=1:0`、`goals=3:1`；芬超 `apif:1313003`
为 `fulltime=2:2`、`goals=3:3`，点球另列。因此 `goals` 不能替代90分钟比分，也不能取点球结果。
`FT/AET/PEN` 完赛；`NS/TBD` 和进行中状态存 scheduled、没有赛果；`PST/CANC/ABD` 存 cancelled；
`AWD/WO` 进入拒绝清单，不写比分。MLS 2023 原件还有一条 `short="Canc"`，按实证显式等同 `CANC`，
不任意容错未知状态。缺失或非法常规时间比分逐行拒绝。

复用 `ApiGame`。别名键为本源数字球队 ID，按联赛组织；approved/auto_verified 生效，
proposed 不生效，未知身份用 `apif:team:<id>` 保存。当前自动核验来自完整主客方向比赛图：
一部分经 T08 已核验的 FD 时区及规范名桥接，一部分直接核对最近三天 jc；不按名字相似度或比分选边。
别名记录附原件 SHA256、对方比赛ID、UTC差值和桥接配置哈希；新增历史球队及沿革仍需人工核验。

去重优先 `jc`，其次 `fd:`、`fdorg:`；只填空的官方 counterpart，不覆盖其赛果。
单队对齐、反向冲突、同窗来源身份不明等隔离，直接训练和读取训练历史也检查迟到重叠。
赛季回填必须显式 `--assume-known-after`，写 assumed；`--date` 增量只用实际 captured，
同比分、晚到未结算观察和更正的可知时间沿用 T09/T03 规则。

[本轮验收及缺口](../reports/2026-11-02-t10-acceptance/README.md)：韩职和澳超链接率未达到90%，
仅jc日职样本不足以完成训练；联合模型已实际回测，不据此宣称效果提升。

用途以站方当前条款为准，标注来源 [API-Football](https://www.api-football.com/)；
本任务不授予额外的数据再分发许可，也不加入 cloud_feeds 定时采集。

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
