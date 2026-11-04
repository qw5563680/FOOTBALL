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

- robots（2026-11-02 实际读取 `https://trade.500.com/robots.txt`）：`User-agent: *` 下明确
  `Disallow: /jczq/?`、`Disallow: /zc6/index.php?expect=*`，这些路径禁止请求。
  未禁止 `/jczq/` 根路径及 `/sfc/`（含期次查询）。此结论限 robots 路径规则，不替代站方使用条件。
- 竞彩赛果**近期备用**根页：`https://trade.500.com/jczq/`。`C500JcHistory` 每批只 GET 一次根路径，
  不带任何查询串，不跟随重定向，不再提供历史编号日查询；同接口请求间隔至少1秒，原件及HTTP元信息落盘。
  `--source c500` 解码 GBK，仍仅读取已结算的精确比分选项。“其他”、未结算保留未知；无精确半场比分。
  `--from/--to` 按北京时间开球自然日筛选，先从全部比赛行（含未结算）核对请求的每一天，
  缺任何日期或覆盖信息不完整时整批报错，不部分入库，提示历史回填用 jc。跨午夜比赛直接由同一根页读取，
  编号日与开球自然日分列；不靠导航链接推断覆盖，不额外请求前一编号日。
  覆盖只证明当前页可见比赛，不能保证边界日期的全天场次完整；日内已移除的比赛无法回补。
  2026-11-02 03:36 北京时间实抓：编号日11月1日5场、11月2日9场；开球自然日分别为11月2日、3日，
  共14场，精确已结算选项为0，回放无赛果写入。这次根页未提供历史比分玩法的结算表，不推测比分、
  不执行JS、不改请求路径补抓。已覆盖但无精确赛果会日志说明零场；源的实际可用性受此限制。
  `cloud_feeds` 只调用默认 jc 官方赛果，没有 c500 备用调用路径。
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
  T26 沿用 T12 的上述 robots 结论；开发环境不重新联网。自建 runner 每次 `sync` 对当期 `/sfc/`
  与公开 `/jczq/` 根路径各发送 **1 次 GET**，每班合计 **2 次**、通常每日两班共 **4 次**；
  手动 sync 每次另加2次。两次请求共用至少1秒间隔，无查询参数、无自动重试、不跟随重定向，
  不请求 `/jczq/?` 等禁止路径。复用 T07 fixture_id / 体彩 ID / 主客球队 ID / 开球核验与原件证据，
  只关联库内已有比赛；未匹配行留在快照和审计，不创建球队。只在数字桥接成立且两个500页面联赛标签一致时，
  云端允许体彩使用不同的联赛显示名，原标签与库内联赛分别留存，不添加通用名称别名。
  Claude 在旧提交 84fbad1 的 Actions 41675857136（数据分支 08c2b04）确认 26158 期抓取成功但 0/14 匹配，
  促成本次补桥接；修复后真实匹配数和托管机访问能力仍待 Claude 验收。本地只用约13KB的 T07 页面及
  明确标注为合成的完整桥接 fixture，不声称本次实测。
  T35 沿用上述 robots/根路径范围，不增加请求。已确认 ID 桥接或 counterpart 的云端映射使用
  库内 Match.kickoff_utc，500 页面原值另存 source_kickoff_utc；未映射的北京00:00且与同期其他比赛
  日期不一致时标 kickoff_basis=placeholder。未映射行不参与报告的已开球判断，但仍列出原因与原时钟。
  冲突审计保留500主客ID、jc候选与双方身份；下一次 sync 用新桥接重试，不按短名相似度猜测。
  本次只做 data/fixtures/t26 合成离线验证；下一期截止前冻结报告的真实验收由 Claude 执行，尚未实测。
- T07比赛ID对照（2026-11-01实际核实）：仅GET公开根路径 `https://trade.500.com/jczq/`，
  不发送robots禁止的 `/jczq/?` 历史查询。行字段 `data-fixtureid/data-id` 连接500比赛ID与体彩ID，
  `data-homeid/data-awayid` 必须与SFC球队链接 `/team/ID/` 方向一致；手工 T07 开球时刻也须一致。
  再检查已有官方Match的联赛和开球。当前页不覆盖的历史场次只能使用已有counterpart或精确身份匹配。
  每条实际使用的ID对照携带URL、读取UTC和原件SHA256；不根据相似简称猜球队，不在查找阶段修改别名。
  同一期再次采集时，当前页已移除的场次可复用旧snapshot中的对照；保留旧读取时刻并重新校验，
  新旧主客球队ID或体彩ID冲突时拒绝。当前页自身获取/解析失败仍返回失败，不伪装为成功。
  先有SFC简称记录、后有ID对照但竞彩记录尚未入库时，保留原c500身份并追加counterpart，仍标unmatched；
  不以相同队名/开球去覆盖已明确指向另一体彩ID的对照。手工 T07 归档对照与新开球冲突仍须核实后再导入；
  T35 云端只在 ID 关联成立时采用库内时钟，不将页面占位时间视为真实开球。
  已核实联赛对照仅 `瑞典超 -> 瑞超`（fixture1362837/jc2042440）；原始标签与官方标签分别保留。
  实抓根页面广告含非GB字节，解码允许替换无关内容，但所消费的身份字段出现替换字符就拒绝。
- 页面编码可能是 GBK，解析前按 `response.apparent_encoding` 处理。

## 3. 体彩官网 lottery.gov.cn

- 传统足彩开奖与期次信息，作为 500.com 的备用来源（需验证接口）。

## 4. football-data.co.uk（历史赛果与收盘赔率，T08）

- 无 key、静态 CSV；用途仍受站方条款约束，不能把免费读取视为任意自动访问或再分发授权。
  2026-11-02 核对 robots.txt：`User-agent: *` 不设 Disallow，仅对 GPTBot、ClaudeBot、CCBot 等具名 AI 爬虫全站禁止；
  本项目采集器 UA 为 `ZucaiResearch`，不在禁止名单内，按普通访客规则访问。只 GET，请求开始间隔至少 2 秒，不转发完整原始文件。
  T11 于北京时间 2026-11-02 再读 robots.txt，规则与上述记录一致；本轮使用既有采集器重建六季英超验收副本。
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

### T18 回填前访问复核（北京时间 2026-11-03）

03:48:10 北京时间（2026-11-02 19:48:10 UTC）重新读取
`https://www.football-data.co.uk/robots.txt`，同站跳转后最终 HTTP 200，732 字节；
原件 SHA256 为 `2dbd3891735d090f6a577d1281efeba5be6280e17bb1e4a8fd936cdc69c2d236`。
通配 `User-agent: *` 的 `Disallow` 为空；GPTBot、ChatGPT-User、ClaudeBot 等具名机器人仍被全站禁止。
规则与已有核验一致，回填沿用项目既有 `ZucaiResearch` 采集器标识和用户已确认的用途，不更换 UA 绕过限制。
本次开发前仅读取 robots，未在线下载 CSV；原件和响应头保存在忽略的 `work/t18/`。

首次规则读取随同站跳转，未记录逐跳发送间隔；随后于 03:54:15–03:54:18 北京时间
通过回填专用通道再次读取 robots，302/200 两次 GET 的实际发送间隔为 **2.100098 秒**，
最终原件 SHA256 与首次相同。本次开发共 4 次 robots GET，0 次 CSV 请求；
补测记录在 `work/t18/robots-paced.json`，没有把未测量的首次间隔作为限速验收证据。

回填运行会重新读取并归档 robots，检查每个计划 CSV 路径；规则不允许或读取失败时停止。
沿用既有限速器，每个 HTTP 跳转均限速；回填发送入口另以 2.1 秒间隔留出准备开销余量，
记录真实请求开始时间和最短间隔，周一当季采集逻辑不变。CSV/robots 请求数分列，失败请求和重定向也计数；
原件、URL/HTTP/读取时间/SHA256、请求明细与拒绝/疑似重复审计供 Actions artifact 保留 90 天。

### T08 实际核验（北京时间 2026-11-02）

已下载主文件 `E0 E1 SP1 I1 D1 D2 F1 F2 N1 P1` 的2627样本；除D2外各再下载2122–2526。
已下载合并文件 `JPN SWE NOR FIN USA BRA`，导入按 Season 起始年2021–2026筛选。
其余代码仍未核验。主文件最终URL会重定向到无 www 的同站地址，回放仅接受这两种相同路径。
验收初次下载时只限制文件请求，内部重定向未等待；审查发现后改为每一跳单独限速并留原件。
修复后实际复抓E0/2122，302到200两跳最小单调时钟间隔2.0秒、CSV哈希与原件一致。
这次复核不改变早期下载记录，完整证据见验收中的 `redirect_smoke.json`。
JPN当前标记为 `2026/2027`，保留该原值，不改写为2026。合并文件 Country/League 的边缘空格被清理后核验。

### T16 日职赛季边界核验（2026-11-02 UTC）

17:16:11–17:16:25 UTC 重新读取同站 robots.txt 与 `new/JPN.csv`，各经一次同站302后200，
共4次GET，每一跳请求开始间隔最短2.100088秒。robots通配规则为空Disallow，具名AI爬虫全站禁止；
继续使用项目既有 `ZucaiResearch` UA。完整响应及逐跳UTC、SHA256只保存在 `work/t16/jpn-verification/`。
JPN原件SHA256为 `3cb71791de545f20a52fdf0cbda11c219f6a8a907b6fec06b285e77efd49164b`。

原件中2025自然年赛季共380行，日期为2025-02-14至2025-12-06；`Season=2026/2027` 共120行，
日期为2026-08-07至2026-10-25。后者支持新赛季在8月开始；文件没有2026年1–7月记录，
因此本次不能独立确认过渡赛事的实际开闭赛日期或覆盖完整性，也不能把缺行视为没有过渡赛事。
8条原始端点记录裁剪为 `data/fixtures/t16/jpn_season_boundaries.csv`（LF换行1265字节），
同目录 `provenance.json` 保存来源、读取UTC、原件/裁剪SHA256、robots与缺口说明；测试只读离线样本。

T16已选B1的UTC分类规则为：日职/JPN在2025及以前按自然年；
`[2026-01-01, 2026-08-01)` 记为 `2026T`，永不计为完整赛季；
自2026-08-01起按每年8月1日换季，首季记为 `2627`。1月1日和8月1日是评估分类边界，
不是本次实测的赛事开球日；来源原有 `Season` 字段仍保留。最近完整季跳过 `2026T`，不改写T15冻结指标。

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
- 本源映射九个国内联赛 `PL ELC PD SA BL1 FL1 DED PPL BSA` 和欧冠 `CL`（T20）；欧洲杯、世界杯不入库。免费档限非商业使用，README 标注来源。
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

### T20 欧冠访问实测（北京时间 2026-11-03）

使用本账户只读请求确认 `CL` / 数字 ID `2001` 为 UEFA Champions League；按 7 月换季。
2022 返回 HTTP 403，2023–2026 返回 HTTP 200。2023/24 共 125 场，2024/25、2025/26 各 189 场；
2026/27 当前列出 144 场联赛阶段（54 场完赛），后续淘汰赛尚未排定，不用缺少的赛程补造记录。
前三季各含 3 场加时或点球完赛，仍按既有 `regularTime` 取 90 分钟比分，不合并两回合。

| 起始年 | HTTP | 场数 / 完赛 | 原件 SHA256 |
| --- | ---: | ---: | --- |
| 2022 | 403 | 0 / 0 | `2ed7e446e2d24b88fc0a2944c28555c9df188c3193a9ca7f0189709b821d3c44` |
| 2023 | 200 | 125 / 125 | `3584b91d4624d6b79cb90d6d717de6357073a1bdeab8023dd1cbb31490cc29e9` |
| 2024 | 200 | 189 / 189 | `1544614455fc30a1b07060dd3221981f3a8654defdb5697fd69730795890bd2e` |
| 2025 | 200 | 189 / 189 | `e0492ec52276ca36aaa10f0348638c3f7b0717de6a731cd3bf43b4879cd98a0d` |
| 2026 | 200 | 144 / 54 | `8d16386bc62c727812f6a471b81f946879f59f6ad2a3a8c6cf164c784cbf44a4` |

实现前匿名复读 API robots.txt：HTTP 404，未提供规则；原件 SHA256：
`55f7d9e99b8e2d4e0e193b2f0275501e6d9c1ebd29cadbea6a0da48a8587e3e0`。五次带凭据赛季 GET 间隔至少 6.1 秒；无赔率、阵容等额外请求。
每日 CL 并入原 fdorg `competitions=` 日期窗口，窗口和请求次数不变。核验 UTC 另见 competitions.json。

欧战别名只复用同源数字球队 ID 的已核验关联及 T09/T10 主客方向比赛图；没有证据的球队保留
`fdorg:team:<id>` / `apif:team:<id>`，写入现有未对齐审计。名称相似和比分都不作为身份依据。
正式配置与持久身份有冲突时沿用隔离/拒绝，不能自动覆盖竞彩赛果或非空 counterpart。

## 6. API-Football v3 / api-sports.io（亚洲与北欧联赛，T10）

北京时间 2026-11-02（实际采集 UTC 2026-11-01）在本账户完成带凭据验证。
基地址 `https://v3.football.api-sports.io/`，只支持 api-sports.io 直连；头为
`x-apisports-key`，值只读运行环境 `API_FOOTBALL_KEY`。T10/T22 日常采集不访问赔率、事件、阵容或统计端点；
T30 另允许审查者通过手动 Actions 对技术数据端点做有界探测，不入库、不进入模型，见下方模板。

### 账户、赛季与日期权限实测

以下为 **付费前的历史实测**，不是当前套餐权限：`/status` 返回 Free、active=true、limit_day=100。`/leagues?id=` 能列出多个赛季，
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

### T20 欧罗巴与欧协联访问实测（北京时间 2026-11-03）

`/leagues?id=3` 确认为 UEFA Europa League（欧罗巴），`id=848` 确认为 UEFA Europa Conference League（欧协联）；
两项元数据当前季均为 2026。以下为付费前的历史实测；当前权限见下方 T22 表。逐年读取 fixtures 后，只有 2022–2024 整季可读；2021、2025、2026
均是 HTTP 200 + `errors.plan`，不得当成空成功。整季响应均为一页，含资格赛，统计口径不等同正赛场数。

| ID | 2022 场数 | 2023 场数 | 2024 场数 | 2021 / 2025 / 2026 |
| --- | ---: | ---: | ---: | --- |
| 3 | 175 | 175 | 269 | errors.plan |
| 848 | 415 | 417 | 409 | errors.plan |

两次联赛元数据和十二次赛季探测共 14 次 APIF GET，均经既有 DailyQuota、reserve=10 与 6.1 秒间隔；
配额从数据分支持久状态接续，末次有效响应头剩余 91。部分响应头向上跳变，仍按既有规则记录，
不能把它解释为请求未发送或每天可额外请求。原件 SHA256、各次 UTC、权限与分页证据逐条写入
`data/api_football/leagues.json` 的 `season_probes`；裁剪样本来源和哈希见 `data/fixtures/t20/provenance.json`。

实现前匿名复读 API robots.txt：HTTP 403，提示需应用凭据，无法取得规则；没有附加 key 试探 robots。
原件 SHA256：`655336a82939309b1dee698b9e2541264ba4a22338046a8c76d9ddea4add456b`。本轮只请求授权 API 的 leagues/fixtures 端点。
日常仍用最近三个 UTC 日的全赛事 fixtures 响应，只增加 3 / 848 的本地过滤，额外请求为 0；
整季回填只由手动 backfill 触发。欧冠只使用 fdorg，APIF 配置不加入欧冠 ID 2。

### 配额和安全归档

本轮共 69 次 APIF GET，均在同一 UTC 日；本地日计数69，最后一个有效每日头为62。
有些权限拒绝无配额头，且每日头观察到向上跳变；本地请求数不等同于服务端已收费数。
实际成功响应带 `x-ratelimit-requests-limit: 100`、`x-ratelimit-requests-remaining`；
`x-ratelimit-limit: 10`、`x-ratelimit-remaining` 是分钟额度，不能用来当每日剩余。
UTC 零点重置是本地计数规则，T10 本轮没有跨午夜实测服务端重置。
后续 Claude 在 T22 审查中记录服务端于 UTC 23:00（当时巴黎零点）重置；每日响应头仍为权威值，
本地计数保持 UTC 零点重置，只会更保守，不额外释放请求额度。

每次请求前在 `data/api_football_quota.json` 持久预占，包括权限错误及网络失败；
线程锁和进程文件锁覆盖整个 GET。每日头是剩余额度的权威值；缺头则延用保守本地扣减，
T22 起本地实际发送数上限默认等于 `limit_day`，可用 `ZUCAI_APIF_DAILY_CAP` 下调；
日上限优先使用 `x-ratelimit-requests-limit`，`/status` 可提供缺失的上限，未取得二者时回退 100 并显示「回退」。
剩余缺失时按 `limit_day - used` 保守计算；已有服务端剩余额度则继续扣减，不因某次缺头而重置。
剩余 ≤ `--reserve`（默认10、范围 0 至 `limit_day-1`）停止并报错，损坏配额文件也报错。
文件只适用于使用同一持久目录的采集进程；其他机器使用同一 key 的消耗靠下一次响应头反映，不能实时共享。
不隐式重试或跟随重定向，默认间隔6.1秒。429 中止；每个后续 GET 都重新过守卫。

原始 JSON 和 sidecar 仅写 `data/raw/api-football/`，认证请求头不落盘；sidecar 仅增存合法数字的两个每日配额响应头及头部校验是否通过。复用 T09
`safe_json_for_archive`，检查原始字节、完整 UTF-8 JSON 的键和值、JSON 转义和重复键覆盖前的值。
非 JSON、截断 JSON 或 key 回显拒绝归档；即使正文不安全，合法数字配额头仍会被记录。
配额头校验仍先执行；校验失败则先保存通过安全检查的响应再报错，避免再次丢掉付费档问题的证据。
`/status` 正文与当前响应头的日上限不一致时拒绝混用计数；请求、头部处理与正文配额更新在同一把锁内完成，防止恢复已被另一请求消耗的额度。
真实裁剪 fixture 在导出前后再检查，原件/裁剪件 SHA256 写进 `data/fixtures/t10/provenance.json`。

多页时原件逐页保存并计数；所有页面齐全后生成 `assembled-fixtures`（附逐页 URL/UTC/SHA256），
以最后一页采集时刻保守作为整个集合的 captured 下界，再一次核验身份并入库，防止第一页抢占歧义链接。
后续页失败保留已下载原件，整个未完成范围不入库；更早已完成赛季保持提交。单独多页片段不得离线入库。

### T22 停用事件与付费档验证（北京时间 2026-11-03）

以下事件来自 T22 任务单和审查者的 Actions 运行记录，本次开发未发正式 key 请求：

- UTC 2026-11-02 23:01–23:10（北京时间 11-03 07:01–07:10），接口返回
  `errors.access: "Your account is suspended"`。平台给出的常见原因包括共享云 IP；
  本项目使用共享 GitHub runner 和开发云环境，任务单记录当天约 30 次，低于免费档 100 次。
  这是平台说明与任务单的判断，不能据此独立确定平台内部封禁机制。
- 所有者在北京时间 2026-11-03 开通付费订阅；按平台说明，付费会解除免费账号的此类停用。
- T20 回填 [run 41592640152](https://github.com/qw5563680/FOOTBALL/actions/runs/41592640152)
  仍失败：旧守卫只允许剩余配额 0–100，付费响应返回数千时在归档前报错。
  T22 将上限改为按响应读取，并保留安全的失败响应。

**Claude 已在 Actions 验证：当前套餐 Pro，每日上限 7500；两次 probe 的相邻 `/status` 观察均不计费。**
以下证据来自审查者的真实运行，开发环境没有向 API 发请求：

| 操作 | run ID | 数据分支提交 | 实测结果 |
| --- | --- | --- | --- |
| `apis days=3 slot=late` | [41597131650](https://github.com/qw5563680/FOOTBALL/actions/runs/41597131650) | `3f43484eab4787a521fd8b4777a7427e6c24cb48` | 3 次日期请求成功，无 plan/access 错误；limit_day=7500、remaining=7491、无回退；新增 1、链接竞彩 8、更新 2 |
| `probe leagues=3,848 seasons=2021-2026` | [41597368373](https://github.com/qw5563680/FOOTBALL/actions/runs/41597368373) | `769067c6010f6af1357ebc1a145860d7725d74ab` | 12 个赛季均可读；status 的 current 9→9，两个每日头均为 limit=7500、remaining=7491 |
| `probe leagues=98,292,188,113,103,244,119,253,169 seasons=2025,2026` | [41597683219](https://github.com/qw5563680/FOOTBALL/actions/runs/41597683219) | `8eb0cedcbb5ab39e290ac594d10c9011792f3392` | 18 个赛季均可读；status 的 current 21→21，两个每日头均为 limit=7500、remaining=7479 |

首组 status 观察 UTC 为 `2026-11-03T07:36:32.172161Z` / `07:36:38.233882Z`，两份原件 SHA256 均为
`85e68e331127f555fc7bf27318ff2820ed263eb79a5ba854ad2f0cd80ee2d138`；第二组为
`2026-11-03T07:39:17.299486Z` / `07:39:23.097367Z`，原件 SHA256 均为
`8fa8a21ab0b45ef947c18cb57b11f39560942fc605e3e2525fb6afbe78242c42`。
完整逐次证据见 [status 审计](https://github.com/qw5563680/FOOTBALL/blob/8eb0cedcbb5ab39e290ac594d10c9011792f3392/state/audit/apif_status_probes.csv)。
`data/fixtures/t22/quota_cases.json` 仍是合成测试数据；本次结论依据上述真实审计，不能用合成样本代替。

`operation=probe` 按原 6.1 秒请求间隔查询两次 `/status`，将 `plan`、`current`、`limit_day`、
两个每日响应头、UTC 和原件 SHA256 写入 `state/audit/apif_status_probes.csv`。
每次成功或失败尝试都记录 `http/outcome/reason`；无法安全归档或未收到响应时保留失败原因，HTTP/SHA 留空。
旧八列 CSV 在追加前原子补列，保留已有值与原件哈希；旧行都是成功记录，补为 `http=200/outcome=ok`。
同一 UTC 日、套餐/上限一致、正文与响应头相符时，计数不变记为 `unbilled`，增加 1 记为 `billed`；
跨日、缺头、计数冲突或其他变化记为 `unknown`，不得据此开启自动刷新。
只有持久状态 `status_free=true` 时，后续 `apis` 和 apif `backfill` 开头才自动查一次；摘要把 `/status` 请求单列。
不计费是本次相邻观测的结论，审查者仍须核对原件；共享账号的并发消耗可能让结果变成未知或计费。
套餐或上限变化会清除旧结论，等待重新 probe。本地硬上限保守计入 `/status` 的实际发送次数。

以下为付费后的实际权限表。30 个范围均 HTTP 200、`errors=[]`、`paging.current=1,total=1`；
数字为第一页 `results`，欧战包含资格赛，**不是入库场数或已完赛场数**。破折号表示本次未探测：

| 联赛 ID / 名称 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 3 / 欧罗巴 | 177 | 175 | 175 | 269 | 271 | 224 |
| 848 / 欧协联 | 423 | 415 | 417 | 409 | 409 | 366 |
| 98 / 日职 | — | — | — | — | 380 | 200 |
| 292 / 韩职 | — | — | — | — | 232 | 228 |
| 188 / 澳超 | — | — | — | — | 163 | 156 |
| 113 / 瑞超 | — | — | — | — | 242 | 240 |
| 103 / 挪超 | — | — | — | — | 242 | 240 |
| 244 / 芬超 | — | — | — | — | 179 | 179 |
| 119 / 丹超 | — | — | — | — | 193 | 132 |
| 253 / 美职 | — | — | — | — | 540 | 510 |
| 169 / 中超 | — | — | — | — | 240 | 240 |

逐次 UTC、HTTP、分页及原件 SHA256 见 [赛季权限审计](https://github.com/qw5563680/FOOTBALL/blob/8eb0cedcbb5ab39e290ac594d10c9011792f3392/state/audit/apif_season_probes.csv)。
`leagues.json` 按这 30 行更新 `season_probes`，保留 run ID 和上述数据提交；各联赛 `verified_utc`
取本次最后一条观察时间。`seasons_available` 为 3/848 的 2021–2026，以及九联赛的 2022–2026
（2022–2024 沿用 T10 已有证据，不假称本次重测）。除日职外，当季 2026 的 `current_season_accessible=true`；
日职元数据仍标 current=2027，本次未探测付费档 2027，维持 false 表示未确认，不能解读为付费后拒绝。
九联赛的 2021 同样没有付费实测，不擅自开放。离线审计副本和来源哈希见 `data/fixtures/t22/provenance.json`。

权限探测唯一入口为审查者手动 `workflow_dispatch operation=probe source=apif`。
逐赛季只查询第一页，不调用导入器；`http/errors/results/paging/raw_sha256` 保存在
`state/audit/apif_season_probes.csv`，全部原件在该 run 的 artifact，任何失败仍为 partial。
T20 已随 main `171f41a` 合入本分支；`backfill --source apif` 复用其导入与欧战身份核验，
允许范围取 `seasons_available`，默认两项欧战，也可显式选择九联赛。
这些 T22 真实验收已由 Claude 完成，run ID、数据提交与场数见
[T22 审查阶段二](reviews/2026-11-03-t22-review.md)；
欧战仍有身份隔离，因此由 T23 继续处理。
本次开发也未重新访问 robots；沿用第 6 节与 T14 的既有访问记录，真实复核留给审查者的 Actions 验证。
正式凭据只保存在 Actions Secrets，任何开发环境不得用正式 key 发请求。

### 技术数据端点实测（T30）

开发仅使用 `data/fixtures/t30/` 合成响应，没有请求真实 API、使用正式 key 或重新请求 robots；
沿用本节已有访问记录。Claude 已在 [PR #46 审查](https://github.com/qw5563680/FOOTBALL/pull/46#pullrequestreview-5764074861)
记录两轮真实探测，运行信息引用于下表；本次修复未自行读取真实原件，字段与权限结论以审查记录为准。

`operation=probe probe_kind=stats` 复用 T22 的安全归档、动态每日配额、reserve=10、6.1 秒间隔；
不改变 60 分钟 job 上限。默认七桥接＋十一采集＋欧冠 2 的 19 个 ID，年份 `2022,2024,2026`。
每个 ID 一次 `/leagues?id=` 保存逐年 coverage（七桥接同时刷新，不能把既有 true 当作统计有值）；
除欧冠 2 外每联赛季取 fixtures 首页、injuries 首页；每个样本依次请求
`fixtures/statistics`、`fixtures/events`、`fixtures/lineups`、`fixtures/players`。
端点是待验证候选；必须核对响应 `get`、请求参数回显、results 与列表、首页分页，错误保留原件，
不自动尝试别的端点，不请求赔率/预测。即使 metadata coverage=false，仍用小样本观察记录实际结果。

抽样先覆盖 FT、AET/PEN、PST/CANC 三组；每组内先选 jc 行 `counterpart_id` 关联的 apif 比赛，
再选普通 apif 来源等库内已知 ID，最后选未知 ID，同档按 ID 稳定排序；再补足同组剩余样本，
按 per_scope 截断。不抽进行中/未开始比赛，不按队名匹配，也不由 ID 大小推断比赛阶段。
这修复了小 ID 资格赛遮住已有竞彩关联联赛阶段样本的问题；无 jc 关联的历史季仍可能只抽到资格赛。
整轮首次 FT statistics 成功后最多加测一次候选 `half=true`；单列参数、原件与字段，`period_basis=unknown`，
由审查者核实是否被接受且真正含半场统计。其他样本的 FT/AET 仅标开球记录的完赛状态，
**不证明统计是 90 分钟还是含加时，更不能将 AET 数据用于 90 分钟模型**。

按所有者对 PR #46 的修复要求，CLI 与工作流默认 per_scope=1，完整范围最坏为
19＋54×(1＋1×4＋1)＋1＝344 次。显式 per_scope=2 的完整范围仍为 560 次，
会在恢复归档、写状态和发送请求前拒绝；两项欧战单季 per_scope=2 为 23 次。
预算含元数据、赛程、逐场端点、伤停和半场候选；不另查 status，不追分页，不自动重试。

CSV `state/audit/apif_stats_probes.csv` 逐请求保存 run_id、UTC、联赛/季/fixture、已知 match_id、
状态、endpoint/parameters/response_get、HTTP/errors/results/paging、coverage、响应叶字段路径、
statistics type 列表、expected_goals 出现/非空两个标志、null/空字符串字段计数、period_basis、SHA256、
耗时（含限速等待与归档）、本项实际请求数、剩余配额、outcome/reason。数值 0 和字符串 "0" 不是缺失；
expected_goals 未出现不补造。空事件/伤停不能据此判为无权限；HTTP 200 errors 非空一定失败。
有效响应还需通过对应端点的记录结构检查；只有球队信息而没有阵容/球员统计，记为空数据。
伤停正文的联赛与赛季必须匹配请求。无法抽到比赛时，四个逐场端点各写零请求 skipped 行，
注明赛程首页失败/为空或无合适状态；不补造 HTTP/原件哈希，整轮记 partial。
去重键为 run_id、联赛、季、fixture、端点、参数、原件哈希、outcome、reason；相同观察重复追加不改字节，
不同 run/原件/失败原因继续保留。网络或归档失败没有完整原件时 SHA 留空，实际发送仍计入配额；
配额触底停止整轮并保留 partial。其他请求失败继续下一项，原件归档失败不会记成功。
安全 sidecar 仅保存数字每日/分钟配额头，不保存认证头；原件在 data/raw/api-football 下随 artifact 保留。

Actions 按赛事×季×端点汇总抽样数、有效响应数、有效率、空响应数、空字段数、请求数和剩余额度。
有效率＝有效响应数 / 实际请求数（包括网络失败；未请求显示 —），不作为全季覆盖率。
有效只表示本次结构通过且有内容，不代表字段准确、全季可用或模型效果；联赛元数据不作为抽样覆盖证明。

| run ID / 日期 | 数据提交 / artifact | 实际请求 / 剩余配额 | 范围 / 抽样规则 | 结论 |
| --- | --- | --- | --- | --- |
| [41779592141](https://github.com/qw5563680/FOOTBALL/actions/runs/41779592141) / 2026-11-04 | `53288de` | 344 / 7163 | 19 联赛、2022/2024/2026、per_scope=1 | 据审查评论：54 联赛季×5端点有响应；欧战仅抽到资格赛 |
| [41783408987](https://github.com/qw5563680/FOOTBALL/actions/runs/41783408987) / 2026-11-04 | `9e85252` | 31 / 7122 | 3,848、2024、per_scope=3 | 据审查评论：补到 PEN/AET；仍需补查欧战联赛阶段 |

| 联赛 / 赛季 | 端点 / 响应 get | 样本数 / 有效数 / 空响应数 | 字段 / xG / 空值 | FT/AET/半场口径 | 原件 SHA256 | 结论 |
| --- | --- | --- | --- | --- | --- | --- |
| 待填写 | 待填写 | 待填写 | 待填写 | 待填写 | 待填写 | 未验证 |

真实验收由 Claude 触发：原件齐全、≥18 联赛季×5端点、请求≤500，并核对账户当日其他消耗后剩余≥6900；
上述两轮使用修复前的抽样规则，jc 关联优先的修复仍待 Claude 补测欧战联赛阶段。
“剩余≥6900”是实测验收值，不会伪造为每次运行的保证。结果回填本节后再作为 T31 字段字典依据。

#### 审查者回填：三轮真实探测结果（2026-11-04，run 41779592141 / 41783408987 / 41785683100，共 398 次请求）

| 项目 | 实测 |
| --- | --- |
| 配额响应头 | `x-ratelimit-limit: 300`（每分钟）、`x-ratelimit-requests-limit: 7500`（每日，Pro） |
| `/leagues?id=` coverage | 12 个采集联赛与欧冠：events / lineups / statistics_fixtures 全 true；statistics_players 芬超 2022/2024 false；injuries 日职、韩职、中超、欧协联 false，澳超 2022 false |
| `fixtures/statistics` | 固定 16 类（Shots on/off Goal、Total/Blocked Shots、Shots inside/outsidebox、Fouls、Corner Kicks、Offsides、Ball Possession `"60%"`、Yellow/Red Cards、Goalkeeper Saves、Total passes、Passes accurate、Passes % `"85%"`），偶见 `Free Kicks`；`expected_goals` / `goals_prevented` 为字符串（`"0.96"`）。**xG 自 2024 季起出现，2022 季 19 个联赛全部没有**；2024 季丹超、澳超、2026 季韩职样本为 null |
| `fixtures/statistics?half=true` | 两次实测：资格赛样本 results 0；联赛阶段样本返回与全场逐项相同数值。参数被忽略，**半场统计不可得** |
| `fixtures/events` | 54/54 联赛季 + 欧战联赛阶段有数据：`time.elapsed/extra`、`type`（Goal / Card / subst / Var）、`detail`、team、player、assist |
| `fixtures/lineups` | 全部有数据：formation、coach、startXI / substitutes（player.id/name/number/pos/grid）；赛后抓取不证明赛前已知 |
| `fixtures/players` | 国内 15 联赛有（games.minutes/rating/position、shots、passes、tackles、duels、dribbles、fouls、cards、penalty）；欧罗巴 / 欧协联**联赛阶段有、资格赛没有**；芬超 2022/2024、韩职 2024 为空 |
| `injuries?league&season` | 每季一页 200–6000 条，`player.type`（Missing Fixture / Questionable）、`player.reason`、`fixture.id/date`、team；日职、韩职、中超、葡超、欧协联三季为空 |
| 欧战资格赛 | statistics 为空或仅牌数、players 为空；联赛阶段完整（欧罗巴 AC米兰–本菲卡 xG 0.96 / 0.64） |
| 欧冠（apif 联赛 2） | 本任务只读元数据（coverage 全 true），联赛阶段未探测 |

原件：artifact `apis-source-41779592141-1`（715 文件，SHA256 `b517cd58…`）、`apis-source-41783408987-1`、`apis-source-41785683100-1`（SHA256 `db30d08f…`）；审计 `state/audit/apif_stats_probes.csv`。审查记录 `docs/reviews/2026-11-04-t30-review.md`。

**T20 范围修订（所有者 2026-11-04 批准）**：允许向 API-Football 请求欧冠（联赛 ID 2）的 `/fixtures?league&season`，**仅用于建立技术数据关联索引**（apif fixture ID ↔ 库内 fdorg / jc `match_id`），不导入赛程、不导入比分、不改 fdorg 优先级；索引规则与其他联赛一致（T31）。

以上三轮结论原样引用 PR #49 所在 `claude/clever-ride-vu4wzz` 分支的审查者回填，
作为 T31 定稿依据；覆盖上方旧模板中「尚待联赛阶段补测」的历史状态。T31 开发未请求真实 API、
未读取正式 key、未重新请求 robots，也未把合成 fixtures 当作实测结果。

### T31 技术数据字段字典（2026-11-04 定稿）

| apif statistics.type | 规范 stat_key | 单位 | fd 对应列（主 / 客） |
| --- | --- | --- | --- |
| Shots on Goal | shots_on_target | count | HST / AST |
| Shots off Goal | shots_off_target | count | — |
| Total Shots | shots | count | HS / AS |
| Blocked Shots | shots_blocked | count | — |
| Shots insidebox | shots_inside_box | count | — |
| Shots outsidebox | shots_outside_box | count | — |
| Fouls | fouls | count | HF / AF |
| Corner Kicks | corners | count | HC / AC |
| Offsides | offsides | count | HO / AO |
| Ball Possession | possession | % | — |
| Yellow Cards | yellow_cards | count | HY / AY |
| Red Cards | red_cards | count | HR / AR |
| Goalkeeper Saves | saves | count | — |
| Total passes | passes | count | — |
| Passes accurate | passes_accurate | count | — |
| Passes % | pass_accuracy | % | — |
| expected_goals | expected_goals | goals | — |
| goals_prevented | goals_prevented | goals（可为负） | — |
| Free Kicks（可选） | free_kicks | count | — |
| 无同义映射 | free_kicks_conceded | count | HFKC / AFKC |

只解析 fd 九主联赛季文件 E0/E1/SP1/I1/D1/F1/F2/N1/P1；国别合并文件不解析。
HFKC/AFKC 不能填进 fouls 或 Free Kicks。fd 缺列记 `column_absent`，空串记 `empty`；
apif 明确 null 记 `null`、缺项记 `not_provided`、非法数值记 `invalid_value`。
xG 缺值统一为 `not_provided`，2024 季之前为 `not_provided_before_2024`，不补 0。
原始数值或字符串完整保存在 raw_value；`"60%" → 60.0`，`"0.96" → 0.96`，`"0" → 0.0`。
未知 statistics.type 保留在哈希指向的原件中，不擅自赋予规范含义。

FT 的 period_basis 为 `90`；AET/PEN 为 `incl_extra_time`，同时 `extra_time_included=true`、
`eligible_90=false`；其他状态为 unknown，不进入 90 分钟特征。没有半场列，也不请求 half 参数。
这里的 FT 标记遵从所有者定稿规则，不声称统计端点自身返回了分段口径。

events 保存 type/detail、time.elapsed/extra、team/player/assist 原文；规范值另外给 elapsed+extra，
elapsed 未知则总时间未知，不把未知补成 0。lineups 保存 formation/coach/startXI/substitutes 和球员
id/name/number/pos/grid，规范 role 为 team/start/bench；captured_before_kickoff 严格按原件抓取时间 < 开球判断。
players 以 role=player_stats 保存完整原文和规范 JSON，单列 games.minutes/rating/position，数字字符串转数值；
不推断未提供的出场时间、射门或抢断定义。没有球员原文的空响应只记审计。

所有技术数据在 T31 导入均为 `backfilled`，available_utc 留 null；不借用结果可知时间，
赛后抓取的阵容不宣称赛前已知。原件与 sidecar 的 URL、HTTP、SHA256、抓取 UTC 均须核验；
原件缺失拒绝入库，HTTP 200 但 errors 非空拒绝并记审计。

T34a（2026-11-05，离线实现）：日常新增行在库内比赛 finished 且捕获时间严格晚于开球+2h时，
改记 `captured / available_utc=captured_utc`；backfill、已有记录不改。预开球阵容不满足该条件仍不进入
本轮技术强度模型。周一early只复用已下载的九主联赛fd CSV保存统计，不新增CSV/API请求；已被竞彩吸收的
fd比赛只能沿唯一既有counterpart精确关联保存，不能用名字补绑定。新null不填0，旧历史继承值不算本次捕获。
strict特征还要求捕获/可知时刻和历史开球+3h都早于本场特征截点。覆盖率captured为行版本数，非场数。
本任务无新的真实HTTP/robots权限核验；请求端点、路径和频率沿本节已记录的T30/T32结论，
开发环境没有读取正式key或触发Actions。真实captured覆盖与连续前瞻结果由审查者后续回填。

T32 使用 `data/api_football/stats_coverage.json` 的初始 unsupported 规则：players 对 3/848 **资格赛**、
芬超 244 的 2022/2024、韩职 292 的 2024；injuries 对 98/292/169/94/848。这些是探测空响应的
调度依据，不是永久权限结论；不排除 3/848 联赛阶段的 players。T31 没有实现 injuries 入库。
欧冠 2 的配置只开放 index_only，不宣称任何赛季 fixtures 权限已核实，普通比赛采集与回填仍排除。
`stats link --fetch` 仅允许审查者在 Actions 手动对七桥接＋2 请求一页 fixtures，不导入比赛；
原件回放 `--raw-file` 不发请求。以上为 T31 边界；T32 技术通道的间隔、任务时长与增量如下，普通采集请求不变。

### T32 技术数据调度、配额与覆盖率（离线实现；2026-11-04 首批真实验收见下表）

本轮只使用离线 fixtures 和合成 Release transport，没有真实 API/CSV/status/robots 请求、
没有读取正式 key，也没有触发 Actions。robots 沿用本节 T30/T14 的既有证据；未新增访问结论。
所有者于 2026-11-04 批准技术通道最小间隔 1.0 秒、分钟 remaining ≤ 20 等下一分钟、429 停批，
普通赛程与 status 仍为 6.1 秒。以下请求数为调度规则或预算估计，不是已测覆盖率：

| 范围 / 端点 | 请求 / 单位与限制 | 离线合成证据 | 真实验收 |
| --- | --- | --- | --- |
| index | 七桥接 39/140/135/78/61/88/94＋欧冠 2，自 2022 起每联赛季 fixtures 一页；约 40 次量级 | 范围、已有比赛链接、页游标；不导入赛程比分 | 待 Claude Actions 首批 index |
| fd | E0/E1/SP1/I1/D1/F1/F2/N1/P1 九主联赛季文件，免费无 key；页/文件计量 | 离线原件回放、已有 fd 比赛附着；不解析国别/赔率 | 待 Claude 首批 fd |
| statistics → events → lineups → players | 按已链接比赛逐场，约 22,075 × 4 ≈ 88,400 次；xG 自 2024 季起 | 离线游标、空值、失败/unsupported 分类 | 待 Claude 英超 2025 statistics |
| injuries | 仅英超、西甲、意甲、德甲、法甲、荷甲、丹超、瑞超、挪超、美职、澳超 11 联赛 | 允许范围、原件归档和游标；不虚构球员数据 | 待 Claude |
| 历史批次 | 单 run ≤ 7000，reserve 10＋日常预留 300；timeout 300 分钟 | 触底停批和可续跑路径 | 待 Claude 配额与重复运行验收 |
| 晚班 daily | ≤ 300；复用普通采集 fixtures 页，普通日期/status 请求数不增加 | 昨日完赛、在售 60–30 分钟阵容窗口、错过窗口记录 | 待 Claude 连续两天摘要 |

空响应最多首抓＋2 次补抓，相邻至少 7 天；unsupported 不重试，网络失败计配额且本轮不补发。
HTTP 200 + errors 判失败，不将错误正文当成功数据。字段 null 保留 null，missing_reason 明示，
red_cards 等不补 0；T33 可另从 events 推导但本任务不修改模型输入。

覆盖率快照 `state/stats_coverage.json` 与其 SHA256 随数据分支保存，按赛事×供应商赛季×端点×阶段
列应取/已取/有效/空值/失败/未支持，另外保留 requests/null_fields/missed。逐场端点只以已链接比赛作分母；
index/fd 分别以页/文件计量，不能与逐场计数相加计算百分比。摘要异常节照录六项冻结数字、观测 UTC 和源链接。
T32 后续修正：成功解析并完成拒绝审计的 index 页 / fd 文件记 ok，`rejected_rows` 单独计数，不再冒充失败页/文件；
下表是修正前真实运行的原始记录，不重写其历史数字。新口径及第四批别名的真实覆盖率待 Claude 在 Actions 验证。

| 当前总体覆盖率 | 应取 | 已取 | 有效 | 空值 | 失败 | 未支持 | 证据性质 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 索引 index（7 桥接＋欧冠 2 × 2022–2026，run 41839748435） | 40 页 | 40 | 9,779 场关联 | — | 拒绝 3,425 场（候选不全 1,916 / 身份临时 1,469 / 多对一 16 / 同窗冲突 10） | 0 | 真实（审查者 2026-11-04 Actions，剩余日额度 7,019） |
| 索引 index 重跑（PR #58 合入后，run 41856263820） | 36 页（4 页已 ok 跳过） | 36 | 10,724 场关联（英超 2022–2025 各 380、法甲 2022 380） | — | 拒绝行 2,466（单列，不计失败） | 0 | 真实；10 组 fd 分裂身份合并执行（1,116 场、6,192 行重定向）；剩余额度 6,712 |
| fd 九主联赛 × 2022–2026（run 41841959728） | 45 文件 | 45 | 217,008 行 | 0 | 442 行（2026/27 当季库内未导入场次） | 0 | 真实（0 次 API 请求，45 个分片 4.08 MB 入 Release） |
| statistics 英超 2025（run 41845338676 + 补抓 41859114220） | 380 场（全季） | 380 | 380（13,680 行） | 0 | 0 | 0 | 真实；首批 272 场，第四批别名与合并后补抓 108 场；null 字段 754（xG 等按口径保留 null）；剩余额度 6,597 |
| 同范围重跑（run 41847153226） | 272 | 0 新请求 | 0 新增 | — | — | — | 真实；分片与配额不变 |
| 生产全范围（events / lineups / players / injuries、其余联赛季） | 未实测 | 未实测 | 未实测 | 未实测 | 未实测 | 未实测 | 待后续 stats-backfill 批次 |
| 摘要测试：英超 2025 statistics | 380 | 379 | 371 | 8 | 1 | 0 | 仅 `data/fixtures/t32/digest_coverage.json` 合成冻结数字，非真实覆盖 |

真实完成门槛仍为英超 2025/26 statistics ≥95%、余额保留日常额度、中断续跑无重复、相同范围第二批 0 请求、
连续两日日常摘要有覆盖行。未验收前不声称已达标；本节后续由 Claude 据 Actions 原件回填。

默认 feeds 改为 index-only；三个技术行表按联赛×供应商赛季压缩 JSONL.zst，基名
`stats-rows-<league>-<season>` 加内容哈希后缀，和 `stats-raw-<run_id>.zip` 原件共同放月度 Release
`stats-raw-YYYYMM`，分别有 `SHA256SUMS-<run_id>` 与 `SHA256SUMS-raw-<run_id>`。
manifest 记录 tag/文件名/哈希/大小/分片行数；分支仅保存索引、游标、覆盖率、审计和哈希清单。
先验证 Release 再提交数据库；归档失败批次回滚但请求扣费不回滚。手动 collector 失败后仍发布
quota-only 状态和错误记录，使下一次 runner 延续实际已扣额度；无变更启动失败不产生提交，取消/未执行不发布。
artifact 副本保留 90 天。
`zucai stats restore-rows --db data/zucai.db --archive ../cloud-data --release stats-raw-202611`
按清单校验下载；省略 release 装回全部历史。行表只在装回后逐行校验，日常预测无需下载。

所有者已批准的 45 个七桥接 apif ID 按原 PR #49 证据使用，7 个无证据 ID 仍 proposed。
Den Haag → 海牙 保留 PR #49 的批准；[PR #56 审批评论](https://github.com/qw5563680/FOOTBALL/pull/56#issuecomment-6633553662)
追加 10 组 fd 精确名称合并与第四批 9 个 apif ID。第三批 5 个已有条目更正中文规范名并保留旧审批链接；
评论 C 的 Metz 112 与 B 重复，原正式文件无此条目，按第四批新增。8 个暂不批准 ID 不写入正式文件。
合并只按正式所有者批准清单及既有前置检查执行，不推断名字；本次无真实接口请求，离线样本不代表生产覆盖率。
模型、特征、参数、赛果可知时钟及冻结预测均不变。

### T23 identity-only 桥接（2026-11-03，Claude 已完成第一轮实测）

正式凭据仅由 Claude 在 Actions 使用，开发环境没有发 API、status 或 robots 请求。
robots 沿用上文记录，本轮未重新探测。真实证据：

- [probe 41612517821](https://github.com/qw5563680/FOOTBALL/actions/runs/41612517821)：
  7 次 `/leagues?id=` 全部 HTTP 200 / ok，数据提交 `d12e1ff032fd20f3a46d4c37328bdae46cada88b`。
- [identity 41612781709](https://github.com/qw5563680/FOOTBALL/actions/runs/41612781709)：
  14 次 fixtures，130 / 150 支自动核验，数据提交
  [`68c1c2bff2f16c983d97524c1a621812bd405754`](https://github.com/qw5563680/FOOTBALL/commit/68c1c2bff2f16c983d97524c1a621812bd405754)。

| ID | 规范联赛 | API 名称 / 国家 | metadata verified_utc（UTC） | 自动核验 / 两季球队数 |
| --- | --- | --- | --- | ---: |
| 39 | 英超 | Premier League / England | 2026-11-03T09:38:25.720340Z | 19 / 23 |
| 140 | 西甲 | La Liga / Spain | 2026-11-03T09:38:31.770928Z | 20 / 23 |
| 135 | 意甲 | Serie A / Italy | 2026-11-03T09:38:37.915228Z | 20 / 23 |
| 78 | 德甲 | Bundesliga / Germany | 2026-11-03T09:38:43.961523Z | 18 / 21 |
| 61 | 法甲 | Ligue 1 / France | 2026-11-03T09:38:50.063059Z | 18 / 18 |
| 88 | 荷甲 | Eredivisie / Netherlands | 2026-11-03T09:38:56.161772Z | 18 / 21 |
| 94 | 葡超 | Primeira Liga / Portugal | 2026-11-03T09:39:02.265570Z | 17 / 21 |

七个条目的 `verification_status=verified`，`verified_utc` 原样取
[`apif_league_probes.csv`](https://github.com/qw5563680/FOOTBALL/blob/68c1c2bff2f16c983d97524c1a621812bd405754/state/audit/apif_league_probes.csv)
对应行的 `observed_utc`，配置保留 probe run/提交/原件哈希。元数据均列 2010–2026，
不据此开放导入：仍为 `identity_only=true`、`seasons_available=[]`、`identity_seasons=[2025,2026]`，
不进入日采集、backfill 或直接 fetch 导入。

[`state/alias_candidates/apif.json`](https://github.com/qw5563680/FOOTBALL/blob/68c1c2bff2f16c983d97524c1a621812bd405754/state/alias_candidates/apif.json)
七联赛的 130 条 `auto_verified` 已按中文联赛键原样复制进正式别名，所有 evidence 字段保留。
审计副本及逐联赛内容哈希见 `data/fixtures/t23/apif_league_probes.csv` 与 `stage1_provenance.json`。
葡超实际 17 支未达到任务单逐联赛至少 18 支的目标；审批和回填验收尚未完成，不能宣称全部验收通过。

**Brighton 51 / Nacional 225 未核验原因（对 68c1c2b 的只读离线定位）。**
两队并非名称格式不一致，而是 T08 正式 fd 别名表根本没有相应条目。数据库仍分别使用原名
`Brighton`（team 480）和 `Nacional`（team 503），共有 192 / 73 条 fd 比赛（全历史口径）。
这些行不能通过 `read_counterparts` 的“双方都具备已核验中文名”条件。例如
`fd:E0:20260830:chelsea:brighton`、`fd:P1:20260919:nacional:famalicao` 均被排除。

APIF 两季分别有 38+38 / 34+34 场；竞彩同窗参考分别有 29 / 5 个，
但同窗不是同队证据。两联赛当前 jc 的所有中文球队名都已属于其他已核验 APIF ID；
在完整对照图收敛后，两队虽各有 124 / 68 条时间邻近边，兼容边均为 0，没有可证明的主客对应。
因此保留 `proposed` 是正确结果，不能按英文名称猜中文、放宽图规则或把参考列当作绑定。
后续审批清单应包含这两队；另外 18 支未核验球队全部只有 2025 季出场，标「仅 2025 季」供所有者判断。
本次不补写未经核验的 T08 别名，不替所有者批准这 20 支球队。

由审查者运行 `operation=probe probe_kind=leagues`，默认一次请求一个候选 ID，共 7 次
`/leagues?id=`，沿用安全归档、6.1 秒间隔和 reserve=10。返回 ID、名称、国家及 League 类型须全部
与候选配置相符；分页、数量或 API 错误都记失败。逐次 UTC、HTTP、原件 SHA256 和结论写
`state/audit/apif_league_probes.csv`，原件在 artifact。元数据年份不作为付费读取权限证明。

有成功 metadata 审计后，`operation=identity` 默认七联赛各取 2025/2026 两季，最多 **14 次 fixtures**；
不另查 status，不追分页。响应必须完整单页且无解析拒绝，才参与同联赛主客方向图。
对照只用已有 jc 与 T08 已核验时区和中文规范名的 fd；同场 jc/fd 已知对照优先保留 jc。
不依赖球队名字相似度，不使用比分或赔率挑选身份。输出候选和身份审计，不导入任何比赛、球队或赛果。

候选证据含来源比赛 ID、对方比赛 ID、UTC 差、整季原件 SHA256、元数据 SHA256、fd 配置 SHA256；
写入 `state/alias_candidates/apif.json` 和 `state/audit/apif_identity.csv`。
本轮已仅复制真实 `auto_verified` 条目到正式文件，保留证据交 Claude 再审查。
欧战继续按同一 APIF 数字球队 ID 桥接正式条目，待审候选不生效。
输出前跨联赛检查同源 ID 的规范名；出现冲突时撤回整轮新候选并报告 partial，不发布矛盾身份。
无法核验的球队由只读 `zucai aliases proposals` 输出审批清单；jc 候选中文名仅作未核验参考，
国家或赛季字段缺失时明确说明，建议规范名留空。新批准条目必须记录批准人、UTC 时间和 PR 链接。

回填将另存 `apif_identity_matches.csv` 的身份字段与范围完整标记，方便离线计数和核验；
此表不含赛果/赔率，不进入模型。两张 APIF 拒绝/疑似重复审计按 `(provider_match_id, raw_sha256, reason)`
去重；缺合法比赛 ID 时用原件行号区分。不同原件或原因保留，fd/fdorg 审计不变。
另由 `apif_identity_scopes.csv` 记录每季最近一次尝试的 UTC、哈希、完整状态和行数；
空响应、拒绝或失败不会沿用旧成功，重现旧哈希仍视为新的观察。评估只选最近范围对应的比赛原件。

**放宽规则仅离线评估。** `zucai aliases evaluate` 读取完整 2025/2026 证据，统计同联赛 ±3h 双向唯一
配对且同队在两场不同比赛中一致的候选，扣除现有严格图已经能核验的球队，并列冲突。
合成 fixture 中两支球队虽满足双场一致，但现有严格图同样能核验，额外解决为 0；
这只是离线测试结论，不能外推到生产库。缺完整范围时输出 `insufficient_evidence`，不宣称零冲突成功。
正式结论须等真实 identity、别名审查和所有者审批完成后再对数据分支评估；当前尚无该阶段数据。
即使额外至少 20 支且零冲突，也只为下一任务提供依据，本任务不放宽任何生产身份规则。

第一批 41 条已获所有者批准并写入正式文件，另有两条欧战运行期自动核验转正，来源见下节。
待 Claude 继续验收：复核本轮正式别名后再合并及回填，补充 2026 隔离数、
新增 jc 关联、竞彩已售比赛对应率/球队绑定率和同范围重复运行的行数不变证明。

### 字段和身份边界

T23 阶段二由 Claude 在 `fd12c4b` 真实回填：欧战 run 41615438667、九联赛 41615881200，
重复运行 41617361107 / 41617536712，数据最终提交 `75371c2`。
欧战拒绝 1215 行、新建 jc 关联 0、2026 隔离 39；根因是历史 `apif:team:<id>` 独立身份挡住新别名。
详见 [PR #34 阶段二审查](https://github.com/qw5563680/FOOTBALL/pull/34#issuecomment-6601053083)。
按新增第 8 项提供独立 `operation=reconcile`，复用已核验 ID 和导入器的规范名查找，严格检查同联赛三小时冲突，
不放宽身份推断或导入器拒绝规则；审计写 `apif_identity_merges.csv`，冻结内容与比赛编号不改。
欧战别名审计的重复时间/runner 路径不再形成新行，实际不同原件与身份依据保留。
Claude 随后在 `604726d` 真实运行 reconcile [41623813368](https://github.com/qw5563680/FOOTBALL/actions/runs/41623813368)：
合并/删除 63 队、重定向 1104 个不同比赛 ID（按联赛/球队计 1346 次）、冲突 0，比赛数不变。
欧战回填 [41624033560](https://github.com/qw5563680/FOOTBALL/actions/runs/41624033560) 新建 jc 关联 12；
重复回填 [41624065507](https://github.com/qw5563680/FOOTBALL/actions/runs/41624065507) 新增/新关联均 0，
四张 APIF 审计字节不变，最终数据提交 `1d0152677977cd807dd4f49ff3985c8ed49319b5`。
这些是本轮正式别名更新前的结果，详见 [复审评论](https://github.com/qw5563680/FOOTBALL/pull/34#issuecomment-6602226081)。

**第 9 项及首批批准（2026-11-03）。** 从上述提交的 `apif_cup_aliases.csv` 全部 825 行中筛出
status=auto_verified 且 evidence 含 counterpart_id 的欧战行，共两条，均为 jc 比赛证据：
欧协联 400 FC Copenhagen → 哥本哈根（apif:1636406 / jc:2041986），
632 Universitatea Craiova → 克拉约瓦（apif:1636482 / jc:2041989）。
name、canonical、status、完整 evidence 原样转入正式文件，保留原始时间及 runner 路径，不复制 proposed。

按 [批准评论 6602500381](https://github.com/qw5563680/FOOTBALL/pull/34#issuecomment-6602500381)，
将 [原表第 4 节](https://github.com/qw5563680/FOOTBALL/pull/34#issuecomment-6601053083) 全部 41 行写为 approved：
欧罗巴 13、韩职 12、澳超 9、挪超 1、美职 4、日职 2。来源名、中文规范名逐字取原表，
approved_by=qw5563680，approved_utc=2026-11-03T11:11:00+00:00，approval_pr 为 PR #34 完整链接。
仅改变这 43 个联赛/ID，既有 130 条桥接及表外 proposed、其他联赛原样保留；来源副本及哈希在
`data/fixtures/t23/stage3_*`，可完全离线核对。本地没有真实 API 请求、真实合并或 workflow_dispatch。

本轮批准后的 reconcile → 欧战/九联赛回填 → 重复回填仍由 Claude 验收。
按任务单最新口径，要求竞彩已售的 2026 欧战全部有 apif 对应（更新前基线 10/32、2/4），
已售球队绑定率 ≥90%（基线 16/29、6/8）。isolated_2026 继续报告；它含竞彩未售场次，
不再作为 ≤10 的门槛。当前 73 不能与此前在身份检查即拒绝的 39 直接比较。

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
本任务不授予额外的数据再分发许可；T14 已将最近三天日期增量接入 cloud_feeds 定时采集。

## T04/T13特征提取的数据可知边界（t04-v2）

本任务不新增联网采集；只读T03/T06已归档的公开赛程、赛果及had/hhad快照。
旧T04验收库有866场比赛、833个赛果、3450条赔率，赛果的captured时刻集中于
2026-11-01 13:03:07–13:26:51 UTC（北京时间21:03:07–21:26:51）。这些是实际采集时刻，
不能改成历史开球后固定时间来填满回测特征。结果表没有独立发布时间的行，不能声称当时已经可用。

依赖赛果的特征仍要求历史开球和赛果available_utc都严格早于目标的min(as_of,kickoff)，
没有可知时间的赛果排除；NULL旧来源标记仅在已有时间时兼容为captured。
assumed默认可用但逐窗披露，strict模式排除。球队近况/交锋限同来源、同联赛，
跨赛季不自动重置。t04-v2保留原43个模型字段及顺序，仅两列休息天数改定义，其余41列不变。

休息天数改为“距最近已知开球的间隔”：从matches取同来源、同球队、所有赛事中开球严格早于目标kickoff的最近一场，
以目标kickoff减去前场kickoff换算天数。scheduled/finished均可，cancelled排除，不要求Result或available_utc；
前场开球可晚于as_of但必须早于目标开球。meta_home_rest_match_id/meta_away_rest_match_id替代旧休息窗口n/assumed_n，
审计只记录赛程依据，不能伪造比分或结果可知时刻。结果历史的计数、依据与assumed占比继续描述赛果，纯赛程休息证据
不计入meta_history_n或假设占比分母；strict模式不因赛程没有赛果而将其排除。
当前matches仅保存当前版本，不能证明赛程、状态或改期在历史as_of时已经可知；间隔也可能漏掉未采集的杯赛，不能当完整赛程或真实休息期证明。

市场列独立取各玩法严格早于min(as_of,kickoff)的最新完整快照。普通had仍输出market_p_home/market_p_draw/market_p_away；
删除market_line，新增可空整数market_hhad_line及market_hhad_p_home/market_hhad_p_draw/market_hhad_p_away。
hhad三项必须同一snapshot_utc、同一整数line；不跨时刻、跨线或与had拼接。两组概率各自按逆价比例归一化去水，
无完整快照时对应组缺失。这7列均只在MARKET_COLUMNS，不进入模型白名单。
数据仅供既有个人分析用途，代码依赖的许可不授予额外的数据再分发权。

[旧T04实际验收](../reports/2026-11-02-t04-acceptance/README.md)的计数、缺失比例及完整文件保持原样，属于t04-v1，
不代表修正后的休息天数及hhad覆盖。T13归档重建库保留同一批866场比赛及833个赛果，但只有2319条赔率，
较原库缺少第三批报价采集；不是旧库的完整逐字节副本。[T13验收](../reports/2026-11-02-t13-acceptance/README.md)
分别列出同一重建库的v1/v2对比及旧T04冻结统计，不把报价缺口归因于特征定义修正。
按AGENTS.md第8条，新reports验收只提交README与SHA256清单；
全量Parquet、metadata及逐场证据归档时放data/football-feeds分支或Release附件，并在README写明位置与哈希。

## 队名对齐

不同来源的队名写法不同（如「曼联」「曼彻斯特联」）。`teams.aliases` 存所有见过的别名，
对齐规则在 `zucai/sources/teams.py` 里维护，未知队名先写入 `data/unmatched_teams.csv` 供人工确认，不要静默猜测。

## 合规

只抓公开页面，遵守各站 robots 和限速，抓取结果仅用于个人分析。

## T14 定时采集的访问复核（北京时间 2026-11-02）

实现前匿名各读取一次 robots.txt：football-data.co.uk 返回 200，通配 UA 没有禁止路径，
具名 AI 爬虫仍全站禁止；与既有记录一致，工作流沿用项目 ZucaiResearch UA，仅采已核验配置的 CSV。
football-data.org API 返回 404；api-sports API 返回 403，提示缺少应用凭据，无法读取 robots 规则。
没有为 robots 请求附加 key，也没有试探禁止路径。本轮没有在线请求 fd CSV 或 sporttery。
API 接口、分页完整性和身份规则沿用 T09/T10；在线冒烟仅各一次 GET，不重试或跟随重定向，
结果见 T14 验收 README，响应原件不入库。凭据只从环境注入，Actions 仅使用对应的两个 Secrets。

## T14a partial 原因复核（北京时间 2026-11-02）

云端各发送一次 GET：fdorg `--days 1` 返回 HTTP 200、73 场，命令成功；
apif `--date 2026-10-30` 返回 HTTP 200 但含 `errors.plan`（账户日期访问限制），
在 `ApiFootball._document()` 的 API 错误检查处抛出 ValueError，不能当无比赛成功。
这是本次云端复现结果；未下载或重放首次 Actions 运行的 artifact。
合法空响应、仅含未配置联赛的日期响应仍成功返回零行；权限、结构、分页等失败仍为 partial。
摘要只展示显式标记的本地校验说明，最多80字；动态外部异常不直接进入摘要，requests 异常使用通用原因。
fdorg 凭据在请求前区分变量未设置/为空与含非法字符，不输出值、长度或具体字符。
采集范围、解析、去重、配额和工作流均保持原规则。

## T15 静态评估库（北京时间 2026-11-02）

继续使用现有 `fetch fd` 与 ZucaiResearch UA；本轮 robots.txt 经同站302跳转后200，通配规则无禁止路径，
具名AI爬虫仍全站禁止。采集器、解析、时区、身份去重和可知时间规则均未改。
恢复 `data/football-feeds` 的 jc 行后，用已核验15联赛的2122–2627/自然年2021–2026 CSV 形成一次性静态库；
沿用 jc > fd，jc 原有赛果可知时间不重写，fd 显式使用开球后3h的 assumed 假设。
本任务不调用 fdorg/apif/jc 网络采集；原件与全量评估只保留工作目录，报告登记请求次数、间隔和库SHA256。

从T15起，旧“正式回测不消费 closing_odds”限定改为：只允许独立的回测对照列读取；
按完整B365、PS、Avg顺序取一组，去水后只用于同场指标。模型、未知球队先验、校准及选参不读取价格，
T06 不能将这些无时间戳价格当作执行SP。收盘价是赛前最后报价，由出版方赛后发布，仅作参照上限。
上述2025-07-23后的Pinnacle质量提示继续适用，报告注明其只是B365缺失时的次选参考。
