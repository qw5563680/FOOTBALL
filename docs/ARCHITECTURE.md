# 架构说明

## 总体流程

```
数据源(sporttery / 500.com / 体彩官网)
   │  fetch_raw()  ── 原始响应落盘 data/raw/<source>/<date>/
   ▼
解析 parse() ── dataclass
   ▼
SQLite data/zucai.db  (matches / odds / results / sfc_issues ...)
   ▼
特征工程 features/  ── 按比赛生成特征行（近期状态、主客场、历史交锋；赔率隐含概率单独放 market_ 列，不入模）
   ▼
模型 predict/  ── 输出每场比赛的比分概率矩阵 P(home_goals, away_goals)
   ▼
玩法推导 ── 胜平负 / 让球 / 比分 / 总进球 / 半全场 / 14 场组合
   ▼
报告 report/ ── markdown 表 + csv，含与官方赔率对比的期望值
```

一个核心原则：**模型只负责比分分布**，各玩法的概率全部从分布推导。这样新增玩法只需加一个推导函数。

第二条原则：**预测概率只来自球队数据与模型**。官方赔率的隐含概率只作为比较基准和期望值计算的分母，不作先验、不默认融合、不进入模型输入；融合实验必须显式开启并以独立模型名另存。

## 模块

### T51 国家队历史参考资料

`python -m zucai.history_supplement` 是独立离线入口，只读取固定审核计划、
`international_results/results.csv`、既有 `nations.json` 和月度归档副本。
先校验版本、SHA256、球队编号/名称、赛事、主客方向、开球UTC、常规90分钟比分、
中立场及独立官方依据，再生成全新的资料目录。16场既有赛果只作核对，3场欧洲杯另存
`supplement.jsonl`，使用 `uefa:` 比赛身份及已核验的 `apif:` 球队身份；不编造 APIF 比赛ID。

目录包含 `supplement.jsonl`、逐场 `audit.json`、完整 `plan.json`、球队映射 `registry.json`
和 SHA256 `manifest.json`。`read_supplement` 复核哈希、完整19场计划、编号与比赛的绑定、
球队映射、审计决定及全部计数。同批重复执行返回新增0；不同内容拒绝
替换旧目录。任何输入校验失败均无部分结果。补充目录必须在正式归档之外。

此功能没有数据库迁移、定时入口、网络请求，也不接入 `restore_feeds`、特征或模型。
`historical_available_utc=null`，真实取回时间另存；不能据此重写历史预测或声称当时已知。
本批只处理已核验19场；扩大样本、更新基线或正式集成需另行核对。原始CSV的含加时比分与
人工核验的90分钟比分同时保留；程序校验事实清单的一致性，不自动解释UEFA网页或PDF。

| 模块 | 职责 |
| --- | --- |
| `zucai/cli.py` | `argparse` 子命令：`fetch`、`features`、`train`、`predict`、`report`、`db` |
| `zucai/db.py` | `get_engine()`、`init_db()`，数据库路径默认 `data/zucai.db`，可用环境变量 `ZUCAI_DB` 覆盖 |
| `zucai/models.py` | SQLAlchemy ORM 表定义（见下） |
| `zucai/sources/base.py` | `Source` 抽象类、落盘与重放工具、统一的 `requests.Session`（UA、重试、限速） |
| `zucai/sources/sporttery.py` | 中国竞彩网：在售赛程 + 各玩法赔率、已完赛结果 |
| `zucai/sources/c500.py` | 500.com：历史赛果、欧赔/亚盘历史、14 场胜负彩期次 |
| `zucai/sources/football_data.py` | 历史 CSV 的下载、严格时区转换和解析 |
| `zucai/sources/fd_ingest.py` | 显式别名、官方优先去重、结果可知时间与独立收盘价归档 |
| `zucai/cloud_feeds.py` | `apis` / `sync` / `verify` / `export` / `probe` / `backfill --source fd\|fdorg\|apif`；四来源 v2 归档与 v1 恢复、SHA256、运行摘要 |
| `zucai/sources/jc_counterpart.py` | API 先到、竞彩后到的精确关联检查与离线 `backfill --relink-jc`，只补 counterpart 并归档逐对审计 |
| `zucai/cloud_backfill.py` | 仅手动的 fd 六季历史回填、范围筛选、逐跳请求与联赛增量审计；复用既有下载/导入器 |
| `zucai/cloud_probe.py` | Actions 手动权限与计费探测，逐季第一页、CSV 审计、不入库 |
| `zucai/apif_budget.py` | 跨平台配额格式校验，兼容旧数据分支状态 |

| `zucai/cloud_api_backfill.py` | 仅手动的欧战及 apif 九联赛历史范围、欧战联合核验、配额守卫、分赛事赛季入库与前后对齐统计 |
| `zucai/sources/cup_identity.py` | 欧战运行期数字 ID 桥接与 T09/T10 主客方向图；证据写审计 CSV，未知身份保持来源编号 |
| `zucai/cloud_apis.py` | hosted API 编排、UTC 周一早班 fd、独立窗口失败与跨运行配额 |
| `zucai/cloud_predict.py` | 只读恢复库 → 按已发布参数多联赛训练 → 两个北京日的纯模型预测、Actions 摘要 |
| `zucai/forecast_archive.py` | 模型与报告追加归档、最新成功模型索引、独立 SHA256 清单与无 pickle 校验 |
| `zucai/scoreboard/` | 冻结预测与赛果配对、三列概率评分、五种虚拟账户、稳健性统计与文本记分板；不训练或联网 |
| `zucai/parlay/` / `digest_parlay.py` | T45 同批单选＋双选枚举、独立 F 账本与冻结视图；不训练、不读取后续价格 |
| `zucai/daily_digest.py` / `digest_notify.py` | 只读冻结归档生成 daily Markdown；仅在 Actions 用 GITHUB_TOKEN 更新每月 Issue |
| `zucai/cloud_aliases.py` | 复用 T09/T10 离线身份图，只追加带证据的待审候选 |
| `zucai/sources/fd_verify.py` | 离线复核当前赛季与 jc 的身份图、候选时区和覆盖缺口 |
| `zucai/features/` | `build_features(session, match_ids=None, as_of=None, *, strict_availability=False) -> DataFrame`；43个球队字段，市场/元信息分离 |
| `zucai/predict/base.py` | `Model` 抽象类：`fit(df)`、`predict_score_matrix(row) -> ndarray[10,10]`、`save/load` |
| `zucai/predict/dixon_coles.py` | 基线模型，带时间衰减的泊松攻防强度 |
| `zucai/predict/derive.py` | 由比分矩阵推导各玩法概率 |
| `zucai/report/` | markdown / csv 输出，期望值计算 |

### 欧战数据来源（T20）

| 赛事 | 来源 / 代码 | 已核验历史 | 每日增量 |
| --- | --- | --- | --- |
| 欧冠 | fdorg / CL（2001） | 2023/24 至当季，7 月换季 | 并入既有 competitions 日期窗口 |
| 欧罗巴 | apif / 3 | 2022/23–2024/25，含资格赛 | 原来的最近三个 UTC 日响应，本地过滤 |
| 欧协联 | apif / 848 | 2022/23–2024/25，含资格赛 | 原来的最近三个 UTC 日响应，本地过滤 |

只增加赛事配置与导入编排，不增加 APIF 每日请求；欧冠不从 apif 回填。

### 快照与重放

抓取器用 `RawStore.save_response()` 保存 HTTP 响应的原始字节，同时生成同名的
`.meta.json`，记录最终 URL、HTTP 状态码、UTC 采集时刻 `fetched_at_utc` 和原字节 SHA256。
同一来源同一时刻的多次抓取各有独立文件。原始响应出现时，元数据已可读取。
单独导入字节可使用 `save(source, name, content, ext)`，缺失的 HTTP 字段保存为 null。
重放时将 `load(path)` 返回的字节传给纯函数 `parse`，从 `load_metadata(path)` 取回
原采集时刻，用作赔率的 `snapshot_utc`。测试环境统一阻止未登记的 HTTP 请求。

## 数据模型

T45 不加数据库表：比赛与球队身份仍取 `matches` 的主外键。新 F 决策与结算独立保存在
`scoreboard/parlay_sd_ledger.jsonl`，结构与冻结文件见「T45」节；原 A–E 账本和表结构不变。

### T31 技术数据附属表

T48 新增 `fd_stats_links`：`provider_fixture_id` 主键、`match_id` 唯一外键，保存
div/season/league、原件开球、原始主客名称与规范球队 ID、真实 captured_utc、原件 SHA256/位置、
正式别名快照哈希和逐次原件观察证据。只建立统计附着，不修改 `matches.counterpart_id`，不创建球队或比赛。
已有明确 jc→fd counterpart 在历史和日常路径均可复用；额外关联只用正式 approved/auto_verified
别名、同联赛/同向主客 ID、三小时窗口及双向唯一检查，反向、歧义或同窗冲突拒绝并审计。

该索引不加入 `STATS_COLUMNS`/`ROW_TABLES`，不改变既有技术数据 v1/v2/v3 的行快照。
feeds 将可选 `state/fd_stats_links.jsonl` 列入文件哈希及独立版本化描述，恢复时先装入并验证
全部身份/别名/原件观察，再验证和恢复技术行；writer 与 archive 共用同一关联验证。
没有额外索引的旧归档继续走既有 fd/counterpart 校验。额外关联缺失或篡改不能授权技术行。

`stats replay-fd` 使用显式的原件路径、div、season、CSV SHA256、sidecar SHA256、真实
captured_utc 和长期 raw_location。先检查全部清单，再逐原件事务重放；不依赖调度游标，
不请求网络、不改来源时钟；历史回放仍为 backfilled/null available。重放前必须在隔离副本
完整恢复旧技术分片和回执，随后本地 publish_rows/export/restore 验证，不向生产自动发布。

技术数据独立保存，不改 `matches`、`results`、`teams` 等既有表，不改赛果可知时间、冻结预测、
模型或训练选择。`stats migrate` 只创建附属表（T38 第五张，T48 另加统计关联索引）；日常 `init_db` 在新库中也会创建它们。

| 表 | 内容与逻辑身份 |
| --- | --- |
| `match_stats` | fd / apif 每场每侧每指标；`stat_key`、原始 JSON 标量 `raw_value`、数值 `value`、`unit` |
| `match_events` | apif 事件；type/detail、elapsed/extra、player/assist ID；完整原事件与规范 `value`（含 total_minute） |
| `match_lineups` | apif 阵容团队记录、首发、替补、逐场球员统计；用 endpoint 区分 lineups/players，role 区分 team/start/bench/player_stats；formation、minutes/rating/position、`captured_before_kickoff`，完整 raw/value JSON 保留教练和其余字段 |
| `apif_fixture_index` | 数字 fixture ID → 一个既有 match_id；联赛/赛季、开球、两侧供应商 ID 与规范 team_id、正式别名来源及其规范化内容哈希；fixture ID 和 match_id 两侧唯一 |
| `match_injuries`（T38） | 供应商伤停观察；league/provider_season、fixture 与球员 ID、原类型与原因、真实捕获时刻；经索引关联，未关联的 match_id/team_id/side 全为 NULL，原件保留并审计 |

前三表共用 `provider/provider_fixture_id/endpoint/record_key/match_id/team_id/provider_team_id/side`，
`period_basis/extra_time_included/eligible_90`，`captured_utc/available_utc/availability_basis`，
`raw_sha256/raw_location/parser_version/missing_reason/version`。`raw_location` 可填本地原件路径或未来
Release 归档位置；T31 回放不上传，T32 批次负责上传。`captured_utc` 必须取已校验 sidecar 的实际抓取时间；本阶段导入一律
`availability_basis=backfilled, available_utc=null`，不借用比赛赛果的时钟。
首发的 `captured_before_kickoff` 只表示抓取是否严格早于开球，不自行授予历史赛前可用资格。
FT 标 `90/false/true`；AET/PEN 标 `incl_extra_time/true/false`；其余状态 `unknown/false/false`。
没有半场统计列，含加时数据禁止作为 90 分钟特征；特征消费者留给 T33。

`sources/apif_stats.py` 只解析已归档的四类端点，先核对 get、fixture、errors、results 和完整单页。
`sources/fd_stats.py` 经 `FootballDataCoUk.parse_statistics` 解析九个主联赛季文件，不读赔率，不解析国别文件。
`stats ingest-fd` 挂到已有且联赛/开球一致的 fd 主键，或经 T48 证据核验的既有竞彩主键；不创建球队或比赛。
解析异常按来源行拒绝并写 `state/audit/stats_ingest_audit.csv`，同批其他有效行可继续。
空 statistics 对两支已知球队写缺失指标；空 events/lineups/players 只记审计，不虚构事件或球员。

`stats link` 只以正式 auto_verified/approved 的同源 ID 解析规范球队，再核对同联赛季、主客 ID、
开球 ±3 小时和两侧唯一性；可沿用其他联赛的正式 same_provider_id，但不能凭英文名或相似度猜测。
未合并的临时身份、反向、多个候选、同窗球队冲突均隔离到 `state/audit/stats_link_audit.csv`，
reason 为「候选不全 / 多对一 / 主客反向 / 身份临时 / 同窗冲突」。整页检查完才建立索引，
既有索引不自动重指向，也不折叠多个来源行来猜唯一对象。待导入整页中的反向、重复和共享球队同窗冲突
会隔离所有涉及的 fixture，而非只检查数据库里的候选；自然年联赛也核对原件声明的赛季，不能只比较两个开球时刻。
离线可回放已有采集联赛的 fixtures 原件；新增索引请求只允许七桥接 ID 39/140/135/78/61/88/94 与欧冠 2，
每联赛季一次 `/fixtures?league&season`，不跟分页。2 的 `index_only=true` 与普通导入、日常采集、backfill 隔离；
`--fetch` 仅在手动 Actions 下可运行，沿用配额 reserve=10 与 6.1 秒间隔。T31 不安排任何定时采集。
索引原件通过 `ApiFootball.fetch_index_raw` 公开入口请求一页，范围校验由客户端统一维护，
复用其配额、间隔与安全归档逻辑，不扩展日常比赛导入范围。

`stats_storage.py` 按 `(provider, provider_fixture_id, endpoint, record_key, version)` 追加，
同原件哈希和逻辑键唯一；已处理的 `(provider_fixture_id, endpoint, raw_sha256)` 在导入审计中保留回执，
包括空响应和全数值不变的响应。回执保存当次解析的 record_keys 和有值的 valid_keys；有值但不变的
较新观察同样推进该行的观察时钟，防止较旧、未见哈希的响应反向更正。相同正文哈希的新抓取只记新的
capture 时钟回执，不重复入表；再次回放同一抓取连审计字节也不变。
新 null 不覆盖已有有效值；更正值写下一 version，旧行保留。复合事件/阵容的 null 字段保留前一版本的
有效规范值，原始 JSON 仍保留本次原文，并以 `partial_null_retained` 明示；不冒充新原件实际提供了这些值。
只对同一事件身份（主客/类型/时间/球员）保留空值；教练/球员/助攻等嵌套 ID 改变时也不混入旧实体字段。
事件没有供应商事件 ID，`record_key=event:<响应中的序号>`；每次原件及旧版本均可重放，不能把序号当事件实体 ID。
事件插入/删除后的完整集合必须按选定响应原件及其回执 record_keys 解释，不能把所有位置的最新版本简单拼成当前事件集合；
本阶段不提供事件集合特征，消费时的快照选择由 T33 实现。
数据库写入、拒绝审计和回执在一次事务边界内完成，异常或提交失败恢复审计原字节并回滚所有新行。

T31 旧归档采用 feeds manifest v2 的可选 `technical_data={version:1, columns, counts}`，配套四个文件：
`state/match_stats.jsonl`、`state/match_events.jsonl`、`state/match_lineups.jsonl`、`state/apif_fixture_index.jsonl`。
每行沿用按 columns 排序的 JSON 数组；文件 SHA256 进入原 manifest.files，stats 审计 CSV 一并归档。
原有五表的 counts、sources 行哈希不改。四表扩展必须整体存在或整体缺失；旧 v1/v2 无扩展归档恢复为空附属表。
export/restore/verify 检查完整性、哈希、行数、外键和主客/索引一致性，校验失败不替换目标库。
恢复时 stats 两张审计 CSV 与所恢复的数据库同步；旧归档缺失的技术审计会从目标目录清除，防止残留较新回执误跳过数据。
没有新建或改写 forecast/report/scoreboard 文件。unsupported 初始名单保存于
`data/api_football/stats_coverage.json`，供 T32 调度使用；资格赛与联赛阶段分开，不将抽样空值推广为永久无权限。

### T38 伤停附属表、快照与外部归档

`match_injuries` 除整数主键 id 外，字段为 provider/endpoint、league/provider_season、provider_fixture_id、
match_id/team_id/provider_team_id/side、player_id/player_name、injury_type/reason、fixture_kickoff_utc、
captured_utc/captured_day/last_captured_utc/available_utc/availability_basis/captured_before_kickoff、
record_key/content_sha256/raw_sha256/raw_location/parser_version/version、完整 raw_value JSON。
captured_utc 是该内容版本首次捕获，last_captured_utc 是末次观察；内容未变只单调延长后者，不新增行。
该区间只说明首末实际观察，不假设中间未抓取时名单一直未变；逐次名单仍以压缩回执为准。
唯一键为 (provider, provider_fixture_id, record_key, version)；内容改变（含 A→B→A）保留新版本，首次来源不覆盖。
缺球员 ID 仍保留 NULL 并审计，不按名字绑定；关联仍核验索引联赛季、主客 ID 与开球±3h。

`injuries_storage` 与审计在同一事务写入。完整页快照（成员、空页、删除、冲突与拒绝明细）保存为
`state/injuries_captures/<sha256>.json.gz`，这是本地外部行表缓存，不进入 git 数据分支。
`state/audit/injuries_ingest_audit.csv` 每页只追加一条计数与压缩回执路径/哈希/字节数；无索引联赛同样仅每页计数。
压缩回执作为不可变资产发布到 stats-raw-YYYYMM Release，technical_data.injury_receipts 记录描述。
restore-rows 先校验所有回执大小/哈希再装回，缺失/损坏不能伪造完整名单。
特征按截点前最新单次完整捕获读取；即使条目复用同一版本，也不把不同抓取的球员记录拼成未出现过的名单。
缺本场、空页、身份失败仍为 null。每日入口 capture<kickoff 才标 captured/available=capture；
历史入口永远 backfilled/NULL；strict 另按真实捕获时刻判断，不改原存储来源。
PR #72 尚未部署的旧日版本表结构不作为已发布数据兼容格式；生产既有 v1/v2 技术归档保持兼容。

T38 的 technical_data.version=3 在原三张行表之外增加 match_injuries，四表仍按联赛×供应商赛季压缩上传
`stats-raw-YYYYMM` Release；未关联伤停按自身 league/provider_season 分片，不丢弃。数据分支仅增加伤停审计，
覆盖率和清单记录数量/哈希；绝不放伤停整表 JSONL。旧 technical_data v1/v2 原清单仍能校验与恢复，伤停表为空；
旧清单只读导出不改版本，首次重新发布才升级 v3。restore-rows 校验历史分片、ID、来源、外键、时间、哈希后原子替换，
历史发布校验只允许伤停版本的 last_captured_utc 单调延长，其余内容/来源字段不得改写；
已有正式 fd 身份合并清单重定向技术引用时也包含新表，合并前置规则不变。未发布的新伤停行和更正无法被普通导出悄悄丢弃。
新旧代码交替发布前需由审查者协调排队作业，旧代码不接受 v3；不通过降低校验来兼容旧写入者。

`stats_coverage.injuries` 给全部存储版本的 captured/backfilled/已关联/总行数及关联率（空集 NULL），
另列联赛×供应商季明细；不是球员去重数、比赛覆盖率或预测能力指标。日摘要与 Actions 摘要只转述这些数，
预测 report/forecast 不增加伤停信息。任务单列出的空接口联赛保留来源说明，不把空响应解释为健康名单。

### T32 批次、游标与外部行表归档

`cloud_stats backfill` 在全新隔离工作目录恢复 feeds；`cloud_stats daily` 使用普通 apis 晚班已恢复的
数据库和 fixtures 原件，不新增普通 fixtures/status 请求。技术请求走独立 1.0 秒限速通道，分钟头
remaining ≤ 20 时等到下一分钟，429 立即停止。原普通采集/status 的 6.1 秒规则不变。
历史批次至多 7000 次，除 reserve=10 外预留日常 300 次；日常至多 300 次。失败请求同样扣配额。
共用发布脚本将 fetch 与本地 rebase 分开；fetch / push 的 5xx 与网络错误共享最多3轮发布尝试，间隔30秒。
fetch-first / non-fast-forward 下一轮先 fetch/rebase 再推；权限错误与 rebase 冲突立即失败，摘要记录阶段、错误与次数，
绝不强推、创建空提交或重采数据。T36 后续修复依据 run 42198016056：原 `git pull --rebase` 网络失败位于 push 重试之外。

sporttery 发布步骤指定 `PUBLISH_RECOVERY_DIR`，本地提交后、首次网络操作前，以远端跟踪提交为 base 创建
`unpublished.bundle`（`HEAD ^base`）、`recovery.txt`（run/base/head/feed SHA）、`SHA256SUMS`。
包只含本次待发布 Git 对象，不含 `.git/config`、认证头、key 或数据库；包含本次导出的比赛、赔率、结果、审计及清单原字节。
`always()` 上传独立90天恢复 artifact，runner 工作目录另留本地副本；持续网络故障时附件上传不能保证成功。
恢复以 base 为前提校验 bundle；远端已前进时重放增量提交，冲突停下，不能用旧快照覆盖后续账本。
恢复和重复发布不运行评分/记账，只发布相同字节；已有冻结预测与账本不倒写。

sporttery job 输出 `publish_status=steps.publish_jc.outcome`，predict / digest 通过环境传递；
预测 `forecast_manifest.runs.*` 新增可选 `sporttery_publish_status` 与 `feed_snapshot`
（commit、run_id、created_utc、schedule_updated_utc、results_updated_utc），引用本次实际恢复的归档。
原 `feed_manifest_sha256` / 数据库 SHA 校验保留，旧四件套和模型算法不变。发布失败仍标 `sporttery_stale`；
日报分开呈现本次 job 发布失败和每批冻结预测的数据版本/时间，后续仅重发摘要也保留该批警告。
缺少新字段的旧冻结继续可读；归档生成时间不得替代竞彩成功采集时间，缺失写未知。
索引按七桥接＋欧冠 2 的 2022 起赛季页；fd 按九主联赛季文件；四端点依次 statistics/events/lineups/players，
injuries 仅 11 个有数据联赛。全部范围先验证，未知端点/联赛/季节参数在请求前拒绝。

`state/stats_backfill_cursor.json` version 1 记录每个来源 / 联赛季 / 端点 / fixture / 阶段单元的
attempts、结果和上次尝试时间，成功回执幂等跳过。空响应最多首抓＋2 次补抓，补抓间隔至少 7 天；
成功解析的 index 页与 fd 文件记 ok，已审计拒绝行单列 `rejected_rows`，resume 不为这些行重新请求整页；
拒绝行不把批次或 collection_status 标为 partial。响应 errors、解析失败、HTTP 错误仍记失败；正常空响应单列 null，预算/限流停批仍保持 partial。
resume 在每个端点内优先已有未完成单元；no-resume 按所选范围顺序枚举，两者都不清空成功/重试/权限回执。
unsupported 不再重试。HTTP 200 + errors、网络失败、权限不足分别记录 failed/unsupported，网络失败本批不补发；
限流和配额触底保留已完成部分，下一批可 resume。赛前阵容仅在 60–30 分钟窗口尝试一次，记录实际捕获 UTC；
错过窗口写 missed，赛后阵容不能补造赛前可用性。
请求前在限速等待之后再次检查阵容窗口；HTTP 状态独立于响应体读取，非 JSON 的 429 也停批，
401/403 也将该联赛季全部端点记 unsupported。日常 injuries 按各联赛当前供应商季取一页；
默认历史范围单独包含日职已开始的 2027 季，不扩展其他联赛的未来季。

T32 引入的 `technical_data.version=2`（T38 扩展为 v3，见上节）仅保留 `state/apif_fixture_index.jsonl` 与
`state/stats_rows_manifest.json` 描述副本、游标、覆盖率和 `state/audit/stats_*.csv`。
`match_stats` / `match_events` / `match_lineups` 按联赛×供应商赛季导出 JSONL.zst，文件名由
`stats-rows-<league>-<season>` 基名、内容哈希后缀组成；同月新内容用新名字，不覆盖不可变资产。
三个行表全部历史版本、身份和空值语义一起保存。原件与 sidecar 打包 `stats-raw-<run_id>.zip`；
原件和行分片同在月度 Release `stats-raw-YYYYMM`。每批清单为 `SHA256SUMS-raw-<run_id>` / `SHA256SUMS-<run_id>`，
manifest 记录 release tag、文件名、SHA256、大小和行数，数据分支不承载大行表。
只建索引且三张行表均为空时，不生成或上传行表校验清单，`checksum_asset` 省略（读取也接受空分片时为 null）；
索引、游标、覆盖率和审计仍正常发布与恢复。原件目录为空时同样不生成或上传 ZIP 和原件校验清单。
默认 `restore_feeds` 只恢复索引、清单、覆盖率和审计；`check_stats` 在这种模式只验索引。
`zucai stats restore-rows --db data/zucai.db --archive ../cloud-data --release stats-raw-202611`
按持久清单下载、解压、逐分片校验后原子替换数据库；省略 `--release` 恢复所有月份并核对完整快照。
逐行校验仅在行表装回后执行，旧 version 1 归档仍支持。
同一批次 restore 与 publish 共用临时的按 SHA256 命名的压缩分片缓存，历史分片只下载一次；
发布前仍校验缓存大小、哈希、行数及历史行内容，批次结束即删除缓存，不增加数据分支文件。
版本切换不是自动兼容发布：T31 代码不接受 v2；T32 普通导出拒绝丢弃尚未发布分片的 v1 行表。
真实验收时由审查者协调暂停普通作业（含已排队旧代码运行），完成首次 stats-backfill 迁移和 T32 合入，
确认代码与归档版本一致后再恢复定时任务；并发写锁不能解决格式不兼容。

原件和行分片须先完成上传与哈希核验，再接纳暂存数据库、游标及归档；归档失败整批不入库，
已经发送的请求仍保留配额扣减。手动 stats-backfill 的发布步骤在 collector 成功或失败后均执行，
把归档失败时的 quota-only 状态与错误记录写回数据分支；数据库/游标仍保持回滚。取消或 collector 未执行时跳过，
启动失败且无归档变化时沿用发布脚本的无变更退出。晚班技术步骤 continue-on-error，普通 feeds 可照常发布。
Release HTTP 失败原因保留状态码与响应正文的前 200 字符：先隐藏上传令牌，再截断并转义换行，不附加请求头或请求 URL。
Actions 保留 raw/state/stats_assets 90 天，整个工作流共用 `write-football-feed-data` 写锁；
独立手动 stats-backfill job 超时 300 分钟，不触发其他 job。API key 和 Release token 仅注入技术采集步骤，
沿用 contents:write；没有新增 cron 或权限。

`state/stats_coverage.json` version 1 的 `observed_utc` 与 rows 按 league/season/endpoint/phase
保存 expected/fetched/valid/null/failed/unsupported，以及 requests/null_fields/missed/rejected_rows。
逐场端点分母为已链接 fixture，index/fd 的单位分别为页/文件。null_fields 表示空字段，不能作为 0 值；
不从请求数推算覆盖率。该文件 SHA256 进入 feeds files；daily_digest 仅在异常节照录六项冻结计数、来源链接、
观测 UTC，旧归档没有该文件时摘要字节不变，不重写任何预测/报告/评分证据。
`stats_identity.reconcile_fd_aliases` 读取正式 fd 别名文件里 status=approved、approved_by=qw5563680 且审批字段完整的精确名称对；
不使用 proposed / auto_verified 作为合并授权。除已批准目标本身持有同一英文别名外，任意第三方名称冲突仍拒绝；
保留原身份 aliases 为空、仅被批准联赛 fd 比赛引用、同场/主客检查，以及三张技术行表和索引与比赛一致的检查。
仅重定向球队外键，比赛 ID、原件、技术版本及其他字段不变；审计和数据库同事务，失败一起回滚。
历史分片的 identity_redirects 记录来源联赛、英文名、规范名、前后球队 ID 和审批链接，并对照正式批准清单验证；
兼容旧 Den Haag 三字段记录。旧分片不覆盖，重新发布时仅允许这些已批准身份对应的 team_id 差异。
第三批 apif 规范名更正保留 approved_by / approved_utc 等原字段，approval_pr 由单链接扩展为按先后顺序的链接列表，
每个链接仍必须是本仓库 PR / 审批评论，原审批链接不丢失；Metz 112 原正式文件无条目，按第四批新增一次。

### 既有赛程与赛果存储

T08 增加 `closing_odds`，不改变既有 timestamped `odds`。FD 导入每行独立 SAVEPOINT，
文件内合格行在一个外层事务提交；错误行有行号、原件哈希和原因。缺 Time、无赛果、夏令时歧义不造值。
同联赛、规范主客和三小时内的 jc 比赛优先；只补空 counterpart，不覆盖官方比分。
身份不完整且存在同联赛同时间官方场次，或有反向主客冲突时隔离。读取历史与直接拟合亦拒绝已知的 jc/fd 重复。
当前赛季核验得到的别名跨历史赛季使用，球队沿革仍须人工维护。历史结果固定延时只属于 assumed。

T09 用 `FootballDataOrg` 下载原始响应，用 `ApiGame` 传递 UTC 赛程和常规时间比分。
`fdorg_ingest` 以联赛和本源**数字球队 ID**解析别名，优先链接同一联赛、同主客、开球相差不超过三小时的
`jc`，其次链接 `fd`，不覆盖其比分。只匹配一队或主客冲突的候选隔离到
`fdorg_suspected_duplicates.csv`。新比赛的键为 `fdorg:<数字比赛ID>`。预定比赛没有
`Result`；历史导入须显式标为假设，增量使用抓取时间，比分更正按 T03 规则重新计时。
`lastUpdated` 和返回的赔率字段都不进模型。若可信观察显示开球后比赛仍未结束，历史假设不可早于或等于这次观察。
读取训练历史和直接拟合均拒绝三个来源间已知的同场重复。较高优先级赛程晚到时，如果 fdorg 场次只有部分球队
已对齐、主客颠倒，或临时球队身份无法排除同窗候选，会报告比赛 ID 并阻止这些身份进入同一训练集。

T24 补充 fd 后到的路径：只有同联赛、开球相差不超过三小时、规范主客球队 ID 完全相同，
且两侧均唯一、没有不同 counterpart 或其他比赛占用时，才把既有 fdorg 的 `counterpart_id` 指向 fd；
fd 行照常保存；按最终保留的开球时刻复核链接，后续改时超出窗口时隔离，旧观察不改变原时间规则。
fdorg 重采确认该链接，不更新 fd 比分，也不重写原 fdorg 比分/可知时间。
jc > fd > fdorg 优先级不变；多候选、反向主客或已有冲突链接仍隔离，不使用名字匹配。
fd 两张拒绝/疑似重复 CSV 保持 `(行键, raw_sha256, reason)` 去重，行键为联赛代码和原件行号。
fdorg 两张表改按 `(provider_match_id 或 row_number, reason, candidates)` 去重：优先数字比赛 ID，
解析缺 ID 时回退原件行号。响应中的易变字段会使原件哈希变化，因此 fdorg 忽略 `raw_sha256` 判断重复，
保留首次出现行的原件哈希作溯源；原因或候选列表变化时仍追加。四张表既有历史行（含重复）均原样保留，
只阻止重复追加，不改写首次证据。
`workflow_dispatch operation=relink-fdorg` 调用 `cloud_feeds backfill --relink-fdorg`，入口拒绝本地和 schedule，
不注入凭据、不联网、不执行回填下载。它扫描同窗存量对，只补 counterpart，遇 jc 候选亦跳过；
清单和补链/跳过原因写入 collection 与 Actions 摘要。沿用写锁、归档恢复校验和冻结预测哈希校验，
不删比赛、不改赛果/可知时间/训练选择；再次运行新增链接为 0。

T10 新增 `ApiFootball`、`apif_quota`、`apif_ingest`、`apif_command`、`apif_verify`；复用 `ApiGame`，
不新增表或列。来源 `apif`、主键 `apif:<fixture_id>`，球队绑定 `apif:<league>:team:<id>`，
未知球队名 `apif:team:<id>`。别名三状态复用 T08 规范查找，数字ID负责本源身份。
入库按 jc > fd > fdorg > apif 选择同一比赛，已知同场只链接，不覆盖高优先级比分。
取消/延期/腰斩保留 cancelled 场次且无结果，判罚/弃权拒绝。历史结果须显式 assumed，日增量为 captured。
配额在数据库外的持久JSON中提前扣减，以线程锁和文件锁串行化请求；合法每日响应头更新剩余额度，
即使正文无法安全归档也不丢失该配额观察。分页全部读完后一次核对完整集合并提交，失败保留原始分页。
为保持 T09 既有函数不改，CLI 通过新增 `apif_cli.extend_cli` 注册和路由，训练 guard 通过
`apif_guard.extend_guard` 扩展；原来的函数体保持不变，直接 Model.fit 与 read_history 均获得四来源重叠保护。

T40 补充 API 先到、竞彩后到的路径：后续 apif/fdorg 再入库时，仅当既有行仍为本源独立行、
本身没有 counterpart 且未被其他比赛声明，并存在唯一同联赛、同向规范主客球队 ID 完全相同、开球相差 ≤3 小时的 jc 行，
且 jc 同样没有 counterpart、未被其他比赛声明，才设置 `jc.counterpart_id=<API match_id>`，outcome 记为 `linked_late_jc`。
多候选、主客相反、任一侧被占用或已有不同链接、`apif:team:<id>` / `fdorg:team:<id>` 临时身份继续隔离；不按名称猜测。
此路径沿用 `linked_existing` 的官方优先语义，API 自己的 Result 不覆盖竞彩赛果。

`workflow_dispatch operation=relink-jc` 调用 `cloud_feeds backfill --relink-jc`，拒绝本地、schedule、
`--relink-fdorg` 及来源/范围参数组合。离线扫描 apif/fdorg 存量并应用同一精确规则，只更新 jc 的 counterpart_id；
比赛行、两侧比分/可知时间及其他字段不变。技术数据和伤停继续按 provider fixture ID 保存，特征沿 T34a counterpart 取用。
报告以 `collection.jc` 保存，`state/audit/jc_relink.csv` 逐对列 API/竞彩 ID、联赛、双方 UTC 开球、
linked / already_linked / 跳过原因及候选；摘要列计数和原因分布。审计随 SHA256 清单导出、恢复和校验，
沿用同一写锁、竞彩同步水位与冻结预测校验，不注入凭据、不采集、不训练、不运行记分板或日报。
`predict/pooled.linked_representatives` 按既有优先级排除已链接的 API 行并写 `linked_rows_excluded`；
未链接或冲突的重复对仍由 `reject_cross_source_overlaps` / `apif_guard` 拒绝，不放宽守卫。

模型元数据改用 `sources: list[str]`，旧可信模型的 `source` 读取为单元素列表。
`training_source_counts` 与范围分开；严格模式或截点过滤后某来源计数可为零，但不能推导模型对未选择来源的支持。
新 T06 冻结 JSON 为 schema 2，`models[].sources` 必须是非空列表；比赛自身仍用单值 source。
每个 league/source 只能被一个已加载模型覆盖，交叠时拒绝；T07 按同一集合匹配。schema 1 旧报告保持可读。
未提供 source 且训练库有多个来源时仍要求显式选择；所有训练保持单一联赛。

T04不改数据库结构。CLI以query_only连接并显式BEGIN固定读取快照，读取既有Match/Result/Team/Odds。
公共build_features函数同样确保SQLite实际事务已开始，刷新已有ORM对象以避免混用旧身份与新比分。
事务结束由调用方负责；Python调用请使用短生命周期、正常autoflush的Session，已有事务会继续使用其快照。
T13将feature_version升为t04-v2，MODEL_FEATURE_COLUMNS的43列及顺序保持不变；仅home_rest_days/away_rest_days
改为赛程开球间隔，其余41列的定义和计算不变。每场有效截点取min(as_of,kickoff)，依赖赛果的历史开球与
结果available均严格小于截点；NULL可知时间不入赛果窗口。performance按source/league/team分组，
H2H按目标主队视角转向；原有窗口n/assumed_n与逐场赛果证据继续保存于meta_列及DataFrame.attrs/metadata JSON。

rest直接从matches按source/team跨赛事选取开球严格早于目标kickoff的最近一场，间隔为目标kickoff减前场kickoff的天数。
scheduled/finished均可，不依赖Result、available_utc或其来源依据，cancelled排除；前场开球可位于as_of与目标kickoff之间。
meta_home_rest_match_id/meta_away_rest_match_id替代原休息窗口n/assumed_n，审计独立记录赛程证据，不附造出的赛果字段。
meta_history_n/meta_history_basis/meta_assumed_history_share继续描述有合法可知依据的赛果历史，保持原含义；
纯赛程休息证据不增加赛果历史数或假设占比分母，strict_availability不排除无赛果的有效赛程。

MARKET_COLUMNS共7列：market_p_home/market_p_draw/market_p_away分别来自普通had最新完整快照；
market_hhad_line与market_hhad_p_home/market_hhad_p_draw/market_hhad_p_away来自独立选择的hhad最新完整快照。
两个玩法的报价时刻均严格早于min(as_of,kickoff)，概率分别按逆价比例归一化；hhad三项必须同一时刻、同一整数line，
禁止跨时刻、跨line或跨玩法拼接。旧market_line删除，market_hhad_line为可空整数；缺完整快照则该组缺失。
这7个市场字段与meta_身份/截点列均不进入43列模型白名单。
生成完成时刻与as_of分别保存。独立导出Parquet、metadata JSON及manifest；不自动写Prediction、不触发训练，
不改变T05/T06现有模型。本次不新增联网采集。历史特征使用当前数据库版本重建；赛果仍按可知时间过滤，
赛程当前版本不能证明其在历史as_of已知，亦无法恢复所有改期/取消版本。休息间隔可能漏掉未采集赛事。
旧T04验收冻结文件保留t04-v1语义，不覆盖。按AGENTS.md第8条，新reports验收只提交README与SHA256清单；
全量Parquet、metadata和逐场证据归档时放data/football-feeds分支或Release附件，并在README记录位置与哈希。

T07在既有 `sfc_issues` 中新增可空 `observed_utc/source_url/raw_sha256/date_basis/snapshot`。
`snapshot` 保存14场原始身份、来源fixture/球队ID、匹配状态、候选ID及已匹配的规范身份。
`mapping_evidence` 记录counterpart或当前公开竞彩页ID对照（附URL、读取UTC和SHA256）；
查找不修改Team，仅确需新增未匹配比赛时创建未知球队。原联赛标签与规范身份标签分别保留。
当前页面移除旧比赛后，重抓同一期复用其snapshot内原ID对照与原读取时刻，再核验球队ID/开球；
新旧ID冲突须人工核对，不因翻页丢失规范身份或创建简称副本。
`sale_end_utc` 改为可空，历史页面没有截止时间就保存null；旧库仅重建该表并保留原期号/截止，
迁移内检查外键，恢复每连接外键开关。`sfc_issue_matches` 的期号/序号/比赛外键关系保留。
抓取在单事务中替换当期最新映射，原始HTTP快照逐次保留，较旧采集不能倒写；官方比赛不被覆盖。
`report sfc` 从经哈希核验的T06冻结JSON取纯模型概率，按当期期次映射及数据库再核身份。
14场概率齐全才计算完整复式覆盖；动态规划每个精确注数保留最大对数覆盖概率状态。
输出冻结JSON内嵌T06证据批次，Markdown/CSV从同一字节载荷渲染，不改变原预测版本和生成时间。
CSV独立保留每行的P2、模式、BET/执行禁用、原预测截点/生成时刻、报告时刻和截止状态。
T26 将期次表与诊断报告加入 cloud_feeds 自动归档，云端路径不创建未匹配球队/比赛，细节见 T26 一节。

时间字段接受带时区的 ISO 8601 字符串或 datetime，统一存为固定微秒精度的 UTC 字符串
（如 `2026-10-31T16:15:00.000000Z`）。不接受无时区的时间。每个 SQLite 连接启用外键检查。
`zucai db init` 只创建缺失的表。业务写入通过 `session_scope` 一起提交，异常时整批回滚。

### `teams`
| 列 | 说明 |
| --- | --- |
| `id` | 自增 |
| `name` | 规范化队名，唯一；外文名称也可保留 |
| `aliases` | JSON 数组，各来源出现过的别名 |

队名匹配使用规范名和已记录的别名。首次观察到的未知规范名会建一条独立记录，
并在 `data/unmatched_teams.csv` 留下首次来源、原名、规范名和 UTC 观察时刻
（`source, raw_name, normalized, created_utc`），供人工核对。清单记录首次观察，
数据库写入另由事务管理。女足、青年队标记保留。

### `matches`
| 列 | 说明 |
| --- | --- |
| `match_id` | 主键，`来源:来源ID`，如 `jc:2026110101` |
| `source` | `jc` / `c500` / `fd` / `fdorg` / `apif`；FD 主键如 `fd:E0:20250815:liverpool:bournemouth`，FDORG 使用来源数字比赛 ID |
| `league` | 联赛名 |
| `home_team_id` / `away_team_id` | 外键 `teams.id` |
| `kickoff_utc` | 开赛时间 |
| `jc_num` | 竞彩编号，如 `周六001`，可空 |
| `status` | `scheduled` / `finished` / `cancelled` |
| `result_checked_utc` / `result_provider` | 最近一次被接受的状态观察时间与来源；fdorg 未结算观察还限定赛果可知时间。旧快照不得倒写比分 |
| `counterpart_id` | 备用源报告的候选体彩ID，或官方记录已经核对的备用ID。只有编号、开球、主客均匹配才将同场归一，并保留既有预测和报价 |

`upsert_match(session, match_id=..., **fields)` 接收完整赛程字段，同一主键再次导入时
更新本次提供的字段。主客队不同，来源前缀与 `source` 一致。

### `odds`
| 列 | 说明 |
| --- | --- |
| `id` | 自增 |
| `match_id` | 外键 |
| `pool` | `had` / `hhad` / `crs` / `ttg` / `hafu` / `euro_<book>` / `asia_<book>` |
| `line` | 让球线或盘口，`had` 为 0 |
| `selection` | 选项，如 `h`/`d`/`a`、`2:1`、`3+` |
| `price` | 赔率 |
| `snapshot_utc` | 抓取时间，同一场可有多次快照 |

`match_id / pool / line / selection / snapshot_utc` 组合唯一。不同采集时间可保留不同价格。

### `closing_odds`（T08）

| 列 | 说明 |
| --- | --- |
| `match_id / bookmaker / pool` | 联合主键，match_id 外键；bookmaker 限 PS/B365/Avg/Max，pool 固定 had |
| `h / d / a` | 完整、有限且大于1的三项收盘价 |
| `basis` | 固定 publisher_closing；没有任何报价时间戳列 |

官方已匹配比赛的收盘价可归在 jc ID 下，但不写 `Odds`；仅覆盖这张独立表的同庄家组。
原始版本保留在 CSV 快照。T15 仅回测对照允许读取此表；模型、先验、校准、T06 报价及球队特征不读取，不能当当前可执行 SP。
T14 将 `closing_odds` 加入 `cloud_feeds.TABLES`，v2 逐行导出/恢复全部来源的五张表；
旧 v1 缺少此表时恢复为空表。共享球队只记全局哈希，其他表另按所属比赛来源计算行数和哈希。

### `results`
| 列 | 说明 |
| --- | --- |
| `match_id` | 主键 |
| `home_goals` / `away_goals` | 常规时间含补时，加时及点球分开处理 |
| `ht_home_goals` / `ht_away_goals` | 半场，可空 |
| `available_utc` / `available_source` | 当前比分版本的最早可知时刻及依据。可核实发布时刻为 published，本次两个数据源默认用实际采集时刻 captured |

历史批量导入可显式传 `--assume-known-after 3h` 使用开球后延时，写作 assumed，并输出实际写入量。
相同比分保留已有依据（含 assumed）和原时刻，按未变化计数。仅来源可核实的 published 时刻可替换 assumed。
比分更正时若缺少新的发布依据，改为新版本的实际观察时刻。
旧库按缺列逐项迁移，有日期但没有标记的旧记录按 captured 处理。既缺日期又缺标记的旧行仍为未知。
取消状态没有比分行。官方日期分页必须完整；单场比分或身份冲突记入 `data/rejected_results.csv`，
其余合格场次继续处理。独立回填命令在有拒绝记录时仍返回非零；仅当请求页全部读完时抛出专门的
`ResultRowsRejected`。T39 云端对各阶段预期的网络、分页和解析错误记录失败，保留已完成的独立窗口，
只发布已完整采集的赛程/赛果；未完成分页仍不部分入库。失败结果窗口不推进 through_beijing。
数据库、I/O 和未预期程序错误仍抛出，不绕过归档校验；独立回填 CLI 的错误语义不变。
拒绝清单随规范化表按哈希保存和恢复。按 `(日期, 比赛ID)` 取观察时刻最新的拒绝，只有同来源的完赛或取消
状态有严格更晚的接受时刻才解除；等时刻冲突和后续页面缺行均保留待补抓。`date_windows` 将未解除的旧日期
加入回看，不依赖该场是否已存在或仍为 scheduled。清单保留历史，manifest 的 `collection_status`、
`pending_result_count`、`pending_result_dates` 表示当前待补抓状态，Actions 摘要明确显示 partial。
旧归档没有拒绝清单仍可恢复。当前云端同步只用 jc 官方源。重跑保留已冻结的预测值。

### `sfc_issues` / `sfc_issue_matches`
`sfc_issues` 的主键是字符串 `issue_no`，保存截止时间 `sale_end_utc`。
`sfc_issue_matches` 以 `(issue_no, seq)` 为主键，`seq` 为 1..14，
`match_id` 引用比赛。同一期中一场比赛只能占一个位置。

### `predictions`
| 列 | 说明 |
| --- | --- |
| `id` | 自增 |
| `match_id` | 外键 |
| `model_name` / `model_version` | 如 `dixon-coles` / `2026.11.01` |
| `p_home` / `p_draw` / `p_away` | 胜平负概率 |
| `score_matrix` | JSON，10x10 比分概率 |
| `created_utc` | 生成时间 |
| `batch_id` / `mode` / `as_of_utc` | T06冻结批次、prematch/historical_replay模式与数据截止；旧行可空，未知不得算作赛前实录 |

单次预测由比赛、模型名、模型版本和生成时间共同区分。三项概率均在 [0, 1]，
总和与 1 的误差小于 0.000001。比分矩阵的形状由 T05 的模型层校验。

T06加载同来源同联赛的可信模型文件，以文件SHA256标记版本，完整报价只在报告/显式混合实验使用。
`report/batch.py` 生成纯模型预测和逐场排除清单，`report/command.py` 管理事务与开球截止复核，
`report/render.py` 不读取数据库或模型，仅从冻结JSON产生Markdown/CSV并校验清单。
训练/加载、生成和文件输出的时刻分开记录：固定 as_of 后运行模型，再取真实计算完成时间；
导出后和数据库提交前仍须早于所有已预测场次开球，超时则回滚本批并清理自己创建的文件。
比分、赔率、队名的后续更新不改变批次JSON。报告版本只追加，不覆盖。
预测冻件当前通过私有PR保存，不在 `cloud_feeds.TABLES` 中；feeds同步不承担预测报告归档。

## 模型路线

1. **Dixon-Coles**（T05）：每队攻击/防守强度 + 主场优势 + 低比分修正，按时间衰减加权拟合。
   不需要任何外部特征，只要历史比分，适合作为基线。
   T37 在预测时检查单场 rho 合法区间；必要的限制写入冻结元数据和报告比赛行，不回写训练参数。
2. **赔率基准**（T05/T06）：官方赔率去抽水后的隐含概率只作为回测对比基准和期望值分母，不作先验、不默认融合、不进入模型输入。融合实验必须显式开启并以独立模型名另存。
3. **GBDT**（后续）：在特征表上用 LightGBM/sklearn 预测胜平负，再与比分模型融合。

## 评估

T05 在单一联赛和来源内封装 `penaltyblog==1.13.1` 的 Dixon–Coles 模型（MIT）。
每周 UTC 周一前冻结一次参数，只用开球和赛果可知时刻都早于截点的记录。
每支球队的攻防、主场优势及低比分修正从赛果拟合。时间权重使用
`exp(-ln(2) × 日数 / 半衰期)`，默认 90 天。预测元数据保留训练截点、最近可知赛果时刻、
训练场数和 0–9 球截断丢弃的质量。比分矩阵以主队进球为行、客队为列，截断后归一化。
半全场推导以终场比分为条件，近似假定各粒进球均匀落在上下半场。T15 默认对新球队使用训练联赛保守先验；样本不足仍披露跳过。

训练命令按场次选取严格早于开球、三项价格齐全的最后一次 `had` 快照，按倒数价格等比例去水。
三分类 Brier 为三个类别平方误差之和的场均值。校准表汇总胜平负三类的预测与实际频率。
市场覆盖不全时分别报告全体模型、同场模型和同场市场三组结果。市场报价可比每周模型训练截点更接近开球。

评估指标包括对数损失、Brier 和按预测概率分桶的校准。评估只用赛前已知的信息构造预测。

`read_history` 同时读取 `available_source`，NULL 兼容为 `captured`。可知时刻未知的行不入训练或评估。
模型只用严格早于截点已知的赛果；回测按 UTC 周一前一微秒冻结模型，每周单独拟合，市场报价只作对照。
报告的顶层和每个 fold 均有 `assumed_training_share` 与 `assumed_eval_share`；顶层训练比例按 fold
训练样本数加权，评估比例覆盖实际成功预测的比赛。任一已使用训练或评估行不是 published/captured 时，
`availability_basis` 为 mixed，引用指标必须同时引用假设占比。`--strict-availability` 排除全部 assumed
后训练模型，并在同一 JSON 的 `backtest` / `strict_backtest` 中并列提供两种回测。
模型文件和预测元数据保存实际训练样本的 `availability_basis` 与 `assumed_training_share`。

T11 的 `train --replay-known-after 3h` 默认关闭，与 `--strict-availability` 互斥。`read_history`
在读出后仅对 `captured/published` 且 `available_utc - kickoff_utc > 延时` 的行创建内存重放副本，
将可知时间设为开球加延时，来源标为 `replayed`，并通过 `attrs["replay_delay"]` 保存延时。
时间未知、恰好等于延时、其他来源的行不重放。训练入口只读已有库，不初始化、不迁移、不写赛果。
T06 真实预测路径不应用这项转换，仍使用库中的可知时间。

回测顶层及每折新增 `replayed_training_share`、`replayed_eval_share`，分别按实际训练样本数加权、
按成功预测的评估样本数计算；未知球队等跳过样本不计入评估分母。任一已使用训练或评估行被重放，
`availability_basis` 为 `replayed`，优先于 `mixed`；`assumed` 占比照常独立披露。
模型文件及元数据保留训练重放占比、延时，以及训练命令关联的周滚动回测 `replayed_eval_share`；
单独 `fit` 或旧模型没有关联回测时后者为 null，不声称是最终模型的独立留出结果。
**带 `replayed` 的指标只能叫“开发阶段历史回放”，不能当作前瞻验证。**

报告顶层及每折的 `uniform` 使用与模型完全相同的成功评估集，主/平/客概率各为 1/3，
非空集的对数损失为 ln(3)，三分类 Brier 为 2/3；空集保持 n=0、指标 null。
这是固定参照，不是误差的数学下限；市场基准仍单独报告，不参与拟合。

## T14 云端持久化

`apis` hosted job 与可选 `sporttery` 自托管 job 使用工作流级 concurrency
`write-football-feed-data`；sporttery 依赖 apis 完成后才 checkout 数据分支，避免基于同一个旧快照写回。
手动 sync 时 apis 跳过；verify 在 apis 内执行只读恢复。
`FOOTBALL_FEEDS_RUNNER` 是单标签字符串（`football-local`），直接传给 sporttery 的 runs-on，变量为空时跳过。每次写回先提交，再
`git fetch origin data/football-feeds`、`git rebase FETCH_HEAD`、普通 push；冲突保留原件及竞彩增量恢复包 artifact 并失败。
运行目录按 run ID/attempt 隔离，Windows 步骤用 Git Bash 和 setup-python 3.12。
API 模块按需导入，竞彩与 verify 不加载 Unix-only API 配额锁。

`state/{teams,matches,results,closing_odds}.jsonl` 与 `state/odds/YYYY-MM.jsonl`
保存原始规范化列；月度展示 CSV 按 UTC 开球月分组，同时含 `source`、UTC 与北京时间。
manifest v2 的 `sources` 包含各来源 matches/results/odds/closing_odds 行数和按全列稳定排序的
JSONL SHA256，`files` 覆盖所有表、CSV、配额、审计及候选；旧 v1 可恢复并在下次导出升级。
校验在替换目标数据库前完成。API 单源/单窗口失败保留已完成结果，导出 collection=partial；
`through_beijing` 只由竞彩同步推进，防止 API 运行跳过官方历史补抓。

工作库旁 `data/state/` 与数据分支 `state/` 一一映射：配额是 `api_football_quota.json`，
审计是 `audit/<source>_*.csv`，候选是 `alias_candidates/{fdorg,apif}.json`。
T42 竞彩完整清单与跳过审计改存 `state/audit/jc_catalog/<YYYY-MM>.csv`、
`state/audit/jc_skipped/<YYYY-MM>.csv`，月份仅取 `captured_utc` 的 UTC 月，不按营业日或北京时间分片。
每次采集只写对应月的两个文件（空响应保留字段头），字段与原去重键不变；缺采集时刻拒绝写入，不借机器当前时间。
旧 `jc_catalog.csv` / `jc_skipped.csv` 属于 2026-11，保留原字节、不迁移、不再追加；
11 月写入仍对照旧记录检查同键冲突与重复，避免把旧记录复制进新分片。
分片按精确的目录/月名白名单进入 manifest SHA256 清单，导出、校验与恢复保留原字节；恢复时清理本快照之外的旧月份，
只处理这两个目录下合法月份 CSV，不动其他文件或赔率分片。
竞彩兼容的旧拒绝清单保留，并同步到 audit；未知球队清单按来源拆分。恢复这些状态后
继续追加/去重，不把每天的配额重新置零；原始响应和运行期 lock 文件不进入数据分支。
候选只来自本次成功完整响应与 jc 身份的双向唯一方向图；解析拒绝的范围不生成候选，
同次运行完整响应先按联赛合并、按比赛 ID 去重，保留跨窗口的竞争边。任何相关 jc 比赛的
±180 分钟邻域未被成功完整响应覆盖时，本次该联赛延后核验，避免边界别名成为传播锚点。
已确认别名作为锚点，待审候选从不作为锚点或自动入库配置。不同批次证据保留，
跨原件同 ID 身份冲突、候选与正式别名冲突、多个候选 ID 指向同一规范名均需人工复核。
竞彩逾期未结补抓仅查询 jc 所有的场次；API 赛程不会触发永远无法由竞彩解除的补抓。
T26 起 sync / apis 把本轮 collection 按来源合入恢复的 manifest，未运行来源原样保留；
新记录含 run_id / observed_utc，表示各来源最近一轮，非同一批采集。collection_status 汇总保留状态及待补抓记录；
Actions 步骤摘要仍只列本轮，完整历史保留在 Actions，manifest 不是历史运行日志。其他手动操作的口径不变。

没有数据库迁移；T14 不改变解析、去重、比分可知性或训练输入。

## T22 按套餐配额与手动权限探测

`state/api_football_quota.json` 延续 `date_utc`、`used`、`remaining`，新增：

| 字段 | 含义 |
| --- | --- |
| `limit_day` | 服务端每日上限，响应头优先；未知时 100 |
| `plan` | `/status` 返回的套餐名，未知为 null |
| `observed_utc` | 最近成功接收配额头或 status 的 UTC 时间，旧文件为 null |
| `limit_source` | header / status / stored / fallback，摘要显示 fallback 为「回退」 |
| `current` | 最近 status 的服务端已用次数，未知为 null；不同于本地 used |
| `status_free` | 两次 status 计数对照的结论：true / false / null（未知） |
| `status_checked_utc`、`status_evidence` | 计费对照时间与两份原件 SHA256；结论可查证 |

`used` 记录本地所有实际尝试，包括失败与 status；服务端剩余额度以每日响应头为准。
请求前持锁预占；`reserve=10` 不变，本地日上限为 `min(limit_day, ZUCAI_APIF_DAILY_CAP)`，未设置时等于套餐上限。
UTC 换日仅清空日计数及剩余额度，保留最近套餐信息；上限/套餐变化清除旧计费结论。
缺失 remaining 时用 `limit_day-used`，已有剩余额度不因缺头补满。旧三字段文件继续读取并标记回退，
无效状态拒绝重置。`verify` 使用不依赖 fcntl 的 `apif_budget` 校验新增字段，Windows 路径不加载配额文件锁。
配额头先校验，安全原件随后落盘；配额校验错误延迟到落盘后抛出，认证回显等不安全原件仍拒绝保存。

`probe` 仅允许手动 Actions，固定允许两个欧战与原有九联赛、2021 至当前 UTC 年，
默认 3,848 / 2021–2026；每范围一页，不调用比赛导入器或别名核验。
先做两次 status 计费对照；只有持久结论为不计费时，后续 `apis` 和 apif `backfill` 才开头刷新一次 status。
status 审计逐次记录 `http/outcome/reason` 和可用的原件 SHA256；旧八列记录原子补列，原值保留。
两个审计 CSV 按运行追加；恢复/导出沿用 feed manifest v2 与统一写锁，表行、竞彩同步日期、模型及预测冻结文件保持原值。
probe 不自动改写权限配置。本次按 Claude 的两次真实 probe 更新允许年份和证据哈希；
T20 回填已接入，九个国内联赛仅扩展范围选择，继续使用原导入器及生效别名，不把欧战推断套用到国内联赛。
apif 回填的每季和联赛摘要新增前后对齐率：同一批不同球队 ID，回填前按正式别名及既有数据库绑定统计，
回填后按本轮完整响应联合核验得到的映射统计；该指标不改变身份导入策略。
仅链接竞彩、未形成持久球队绑定的映射，下次仍需按相同证据重新核验，不计入回填前对齐率。

## T23 身份桥接与审批（不导入桥接联赛比赛）

`ApiFootball` 默认只加载非 `identity_only` 联赛；显式身份客户端只加载七个桥接联赛。
日期解析、直接 fetch 和 backfill 均无法把桥接联赛导入比赛库。候选初始状态为
`verification_status=needs_verification`、`verified_utc=null`；Claude 实测后改为 `verified`，
时间逐条取元数据审计，`metadata_probe` 保留 run、数据提交与原件哈希。
`seasons_available` 仍为空；`identity_seasons=[2025,2026]` 只限定身份核验目标，不开放比赛导入。

`probe --probe-kind leagues` 只调用 `/leagues?id=`，将 ID、名称、国家、类型及安全原件核对结果
写入 `state/audit/apif_league_probes.csv`。`identity` 必须找到各联赛最新的成功记录，
并与当前候选配置匹配；元数据没有通过时不会发赛季请求。
默认七联赛 × 两季共 14 次 fixtures，不查询 status，不追加分页；分页或解析拒绝使该范围无效。
同联赛完整响应合并后复用 `apif_verify.infer_aliases`。对照是已有 jc，以及 T08 已核验时区、
且双方规范名均为生效别名的 fd 记录；已知同场 jc/fd 对照优先保留 jc，其他竞争记录不省略。
仅已核验正式别名可作锚点，旧候选不参与推断。正式别名、数据库与模型均不写。
输出前还核对所有正式/候选联赛的同源 ID；发现跨联赛规范名冲突则撤回本轮全部新增候选，
审计记为未核验并报告 partial，不能保留恰好先检查到的一方。

候选保持 `state/alias_candidates/apif.json` 原结构，证据含 APIF 比赛 ID、对方比赛 ID、
UTC 差、整季原件 SHA256、元数据 SHA256 和 fd 两份配置 SHA256。
`state/audit/apif_identity.csv` 按运行记录球队状态、来源名、国家、两个赛季出场数和证据。
需实现者把真实运行中的 `auto_verified` 条目保留证据复制进正式文件，经过审查后才生效。
欧战 `same_provider_id` 已遍历所有正式联赛别名，直接复用七联赛的数字 ID；不新增名字匹配规则。

别名状态仍为 `proposed` / `auto_verified` / `approved`。新批准条目附：

| 字段 | 含义 |
| --- | --- |
| `approved_by` | 所有者的 GitHub 账号，非空 |
| `approved_utc` | 带偏移的 UTC 批准时间 |
| `approval_pr` | 本仓库批准所在 PR 的完整链接 |

读取仓库正式别名时，新 `approved` 条目强制填齐并校验三项。显式指定的旧版离线别名文件与底层导入接口
允许无审批字段的历史记录，但一旦出现任一审批字段就须填齐；不编造历史批准来源。
审批清单从只读归档及仓库正式配置生成。建议规范名留空，jc 参考列明确未核验；清单既不是别名文件，
也不参与自动桥接。国家、赛季或完整覆盖缺失会明确披露，不把未知场数写成整季覆盖。

APIF 两张拒绝/疑似重复表选择**汇总表去重**方案：按
`(provider_match_id, raw_sha256, reason)` 去重，原子重写并整理已有重复行；没有合法比赛 ID 的拒绝行
用原件行号区分，防止丢掉不同坏行。不同原件或不同原因保留。fd/fdorg 审计写法不变。
完整采集另写 `state/audit/apif_identity_matches.csv`：只保存联赛、赛季、数字球队/比赛 ID、名称、开球、
原件 SHA256、完整标记和范围行数，供离线计数及评估；不含赔率或赛果，也不用于训练。
该表复用 feed manifest 的 `state/audit/apif_*.csv` 布局，无数据库 schema 迁移。
`apif_identity_scopes.csv` 单独保存每个联赛/赛季最近一次尝试的 UTC、原件哈希、完整状态与行数。
请求前先标未完成，空响应、解析拒绝或请求失败不会遗留旧成功状态；相同哈希重现也更新最近观察。
离线评估先按这张范围表选原件，再检查对应比赛行；没有范围记录的旧归档视为证据不足。

回填摘要新增本次真正新建的 jc counterpart 数、2026 季本次仍隔离的不同比赛数，
以及同联赛 jc ±3h 窗口候选球队对齐率。窗口分母保守包含同时开球的未知球队，不声称全是已确认在售球队。
`aliases evaluate` 只读完整的 2025/2026 范围，比较双向唯一窗口、同队两场一致的候选与现有严格图；
已能严格核验的 ID 不算额外解决。缺范围或行数不完整则 `insufficient_evidence`，不会把无数据当零冲突成功。
输出只作研究结果，即使达到至少 20 支、零冲突，也不改变任何生产身份规则。

### 存量同源身份合并（T23 第 8 项 / T29）

`cloud_feeds reconcile` 是独立的手动 Actions 操作，不接收来源/联赛/年份选择，不访问网络或注入 key。
`apif_reconcile` 遍历 `apif:team:<正整数>` 和 `fdorg:team:<正整数>` 临时行及其有比赛的每个联赛。
T29 的合并授权只来自各自正式文件 api_football.json / football_data_org.json 中的 active 别名：
同一来源数字 ID 在任意联赛为 `auto_verified/approved`，便对它在所有联赛的存量历史生效，
包括“欧战正式别名 → 丹超历史”。不额外推断比赛图、不读取待审候选；数据库里单独残留的绑定不能替代正式授权。
只有 proposed 的 ID 不处理。目标查找复用 `_Teams.one`（规范化 name/aliases）；APIF approved 要有完整批准来源，
FDORG 既有批准格式继续兼容，有审批来源字段时仍校验。两源数字空间隔离，不因 ID 数字相同跨源认队。
导入器的“已有独立身份”拒绝规则保持原样。正式别名须先按下面的传播步骤提交审查，再做真实合并。

`aliases propagate --archive <目录>`（`alias_propagation.py`）校验并只读恢复归档，按临时身份的
同源比赛找出有历史的联赛。只从运行前两份正式文件各自的 `auto_verified/approved` ID 传播，
不读待审候选，不根据名字推断；目标已有 active 条目保持原样，无 active 来源或无临时历史就不写。
同 ID 的多个 active 来源规范名须一致；选原始证据条目优先于已传播条目，其余按来源联赛名稳定排序。
canonical/status 取来源原值，已有目标来源名保留（无则取来源名），不沿用目标旧 proposed 的拒绝原因；
evidence 替换为一条 `{kind: same_provider_id, source_league, source_status, source_aliases_sha256}`，
哈希是本轮写入前该来源正式文件的原始字节 SHA256。approved_by/approved_utc/approval_pr 原样复制；
approved 来源缺审批信息则拒绝传播，不补造批准字段。跨联赛规范名冲突、目标反向别名冲突拒绝本次全部写入。
两份输出全部检查通过后才原子写各文件，第二份写入失败恢复前一份；没有变化则不写文件。
命令逐条 logging 输出改动和来源证明，不改数据库/数据分支，重复运行 0 条；改动先走 PR 再供 Actions 使用。

reconcile 另检查目标的正式落地：APIF 取配置中非 identity_only 联赛，FDORG 取 cloud_apis 的
COMPETITION_CODES 对应配置，分别排除 CUP_LEAGUES。若这些日常采集的非杯赛目标缺少正式 active
(联赛, ID)，写审计 reason=`目标联赛正式别名未传播`、记冲突跳过并保留该联赛临时历史。
欧战仍按既有跨联赛桥接检查；同一临时身份已合并的其他联赛不受这个跳过影响。
本轮丹超/400 由欧协联/400 原 auto_verified 传播为哥本哈根，保留同源 ID 证据；未改 `_Teams.resolve`。
PR #45 评论 6616192600 的 15 个 approved ID 另外按有历史的欧战联赛落地，随后传播新增 6 个联赛条目；
批准字段及来源原文保存在 t29 离线审批 fixture。实际身份合并仍交由审查者在 Actions 验收。

逐联赛前置检查：同源 ID 的各正式来源联赛规范名一致、来源/目标联赛无反向别名冲突；
目标唯一存在且不是临时行、临时 aliases 为空、目标无不同的本联赛同源 ID，当前 ID 未被其他球队占用；
同联赛目标的任何比赛与待移动比赛相差 ≤3h 均算冲突，临时行若被其他来源的比赛引用也跳过。
T41 仅允许一种同场例外：用已批准的规范球队 ID 替换待移动身份后，复用
`jc_counterpart.late_jc_candidate(..., team_ids=规范主客ID)` 的只读判定，冲突恰为唯一同向同联赛的 jc 行，
开球相差 ≤3h、双方 counterpart 均为空且未被其他比赛声明。反向、多个候选、非 jc 冲突及任何占用仍保留原拒绝原因。
同一联赛/ID 的全部待移动比赛须先检查完，再更新 matches 的 home_team_id/away_team_id、追加来源绑定；
符合例外时在同一事务设置 `jc.counterpart_id=<API match_id>`，API counterpart 保持空，竞彩赛果与 API 原赛果均不改。
身份审计 reason=`merged_and_linked`，同时复用 T40 写入 `state/audit/jc_relink.csv` 的
`outcome=linked, reason=reconcile` 回执；无同场链接的原路径仍为 `merged`。APIF/FDORG 共享此路径，不新增名字判断或放宽训练守卫。
所有联赛不再引用临时行才删除它；无别名/未批准行保留。SQLite 事务内处理，审计写入失败时回滚数据库；
数据库最终提交失败时恢复两源身份审计及 jc 补链审计（原本不存在则移除），任一审计写失败亦回退已写文件，避免失败产物误报成功；本轮不发布。
除上述球队外键与 jc counterpart 外，match_id、开球、状态及其他字段、results/odds/closing_odds/predictions/sfc_issue_matches 均不改，无 schema 迁移。

`state/audit/apif_identity_merges.csv` 与新增 `fdorg_identity_merges.csv` 分开保存任务单九列，
防止相同数字 ID 的两个来源发生审计键冲突。alias_source 包含 kind=`same_provider_id`、provider、
source_leagues、source_entries（原状态/批准来源/证据）、正式文件哈希、目标联赛与 ID；
按 `(league, team_id, canonical)` 保留一行，语义不变时保留原 run_id/字节；冲突解决后的成功更新原行而不重复追加。
为兼容既有手动工作流，reconcile 报告仍放 `collection.apif`，T29 各总数为 APIF+FDORG 合计；
`merged_teams` 计本轮实际移动过的不同临时行，`rekeyed_matches` 计不同比赛 ID（主客都移动也只算一场），
`deleted_teams` 计最终删掉的无引用行，`skipped_conflicts` 只计已有解析结果却未通过检查的联赛/ID。
没有 active 解析结果单列 unresolved_pairs，不计冲突。队表减少数等于 deleted_teams，比赛数保持不变。
恢复/导出仍使用原数据分支格式与全局写锁，竞彩水位不前进；再次恢复及 verify_forecasts 校验后发布。

T29 的 `cup_history` 只读统计 `coverage_before/coverage_after`，与 `aliases proposals --scope cups-history`
使用同一口径：2025-07-01 UTC 起库内 jc 欧冠/欧罗巴/欧协联球队（任务单基线，不等于实时销售状态），
分别计库内不同 match_id 少于 20、少于 60 及 20–59 的球队数；少于 60 是包含少于 20 的累计数。
两源临时身份数含无比赛的行，欧战出场按球队/比赛计，双方均临时的一场计两次；均含所有比赛状态，
不是训练样本数，不改训练筛选。清单只读，因此本次合并/重定向/冲突跳过均标 0；reconcile 摘要给实际本轮数及前后覆盖。

`cups_history_proposals` 复用 `aliases_snapshot` 校验/临时恢复数据分支，输出 manifest SHA256。
只列仍临时、欧罗巴/欧协联合计 ≥10 的 APIF ID，按合计降序/数字 ID 升序，一 ID 一行，原名取杯赛审计。
赛季按既有 7 月换季从开球日期分组；近两季相对于归档 `through_beijing`，不使用运行当天时间。
对手参考只接受库内 jc 身份或正式 APIF/FDORG active 别名精确对应的规范队，最多三个；不列同窗比赛，规范名填写格保持空白。
另列未授权 fdorg 身份（原名取 unmatched 审计），末尾附没有 APIF 正式/持久绑定的 jc 欧战球队及 fdorg 正式别名。
只写新的 Markdown，不覆盖已有文件或写入归档；参考列不影响任何绑定判定。默认 T23 清单和导入器 `_Teams.resolve` 不变。

`apif_cup_aliases.csv` 改为按球队状态和语义证据去重并整理旧重复行：比较 evidence 时递归忽略
`seen_utc`、`sidecar`，保留首次原始证据供追踪；不同 raw_sha256、规范名、状态或实际匹配依据仍保留。
不修改 fdorg 的别名审计写法。身份合并审计走既有 `state/audit/{apif,fdorg}_*.csv` 归档白名单。

## T15 评估与参数文件（无数据库迁移）

T16按所有者选择的A1采用策略绑定：`model_readiness` 不再要求训练库与评估库SHA256相等；
仍核对相同来源集合、累计非重放训练策略、半衰期、先验方法、校准开关、模型训练截点不早于参数选择时刻，
以及证据在预测时已存在且覆盖最新两个完整季。门槛数值及BET禁用保持不变。
训练模型保留旧 `database_sha256` 兼容字段，同时保存 `training_database_sha256` 与
训练时配置中的 `evaluation_database_sha256`；T06优先读回模型保存的评估库哈希，模型未保存该值时
才回落到预测时配置。P2策略与证据检查仍使用预测时配置。冻结JSON保留两个哈希，模型表并列显示，
并在不同时标注“评估库 ≠ 训练库”。旧冻件没有新字段时维持原渲染；配置更新不回写旧报告。

参数文件按显式 `--params-file`，或 `ZUCAI_PARAMS` → 当前目录 → 源码相对路径定位；
条目及P2所需证据字段先校验，错误使用点名联赛与字段的ValueError。找不到文件时90天回落会记录WARNING与尝试路径。
参数来源增加 `params_path`；显式校准诊断须指定独立参数输出文件，不能写入任一默认候选路径，
包括尚不存在的 `ZUCAI_PARAMS` 目标，比较前统一展开用户目录并解析相对路径与符号链接。
`train --predictions-out` 将完整逐场记录写JSONL，`evaluation` 区分默认/严格回测；stdout只留汇总或文件引用，
不改变 `evaluate()` 供选参使用的完整返回值。

赛季边界改为按联赛配置的分段表：日职/JPN在2025及以前按自然年，2026年1–7月标签 `2026T`，
2026年8月起按8月换季标签 `2627`。过渡季不能成为完整评估或选参季；最近完整季回溯和新选参验证均跳过它。
2025选参窗口仍在2026年1月1日冻结，但验证从2627的2026年8月1日开始，不能用过渡期赛果回选参数。
旧已发布日职窗口标签 `2026` 只在训练配置兼容层映射为 `2026T`，不改写配置字节或旧冻件。

`predict/seasons.py` 定义 UTC 跨季/自然年边界，`backtest.py` 在价格表与结果历史分离的前提下，
按周或月冻结、跨季累积训练，并输出赛季、总体、同场收盘、未知球队及 jc 链接子集。
`predict/tuning.py` 从赛季 S 的完整 monthly walk-forward 选择半衰期，仅在 S+1 验证，
最终另跑 weekly，`predict/parameters.py` 原子更新 `data/model/params.json` 的选择时刻、窗口、
数据库哈希、来源、先验政策及 P2 门槛证据。最近两完整季的规则见 README 评估一节。
`predict/calibration.py` 只接收训练折预测平局概率、实际赛果和时间衰减权重，拟合单个log-odds截距。
默认是否开启只由选中候选的S季未校准平局偏差决定（绝对值>3个百分点），S+1不能回选开关。
每个验证季的半衰期和校准开关分别冻结；CLI覆盖各自独立，模型元数据记录实际值及来源。
训练入口只读同一数据库快照；模型输入只含球队身份、比分和可知时间。赔率不进入先验或选参。
未知球队按最弱四分位攻防向联赛均值收缩50%，沿用训练集的主场优势和低比分修正；
T37 将预测时的非负约束扩展到已知球队：设当场进球强度为 λ、μ，rho 合法区间是
`[max(-1/λ, -1/μ), min(1, 1/(λμ))]`，对应 00 / 01 / 10 / 11 四格的修正因子非负。
采用方案 A，仅限制当场计算使用的 rho；不修改模型保存的 rho、攻防、先验、半衰期与平局校准。
强度直接取 penaltyblog 同一底层计算函数的返回值，避免两套指数运算的舍入差异；若合法边界仍产生
极小负值，至多将 rho 向零移动 8 个浮点可表示步长并记录次数，仍无效则按原检查拒绝，绝不直接把负格填零。
元数据记录 `rho_clipped/rho_before/rho_after/rho_legal_interval/rho_clip_reason/rho_roundoff_steps`；
原因是 `outside_legal_interval` 或 `boundary_roundoff`，无调整时原因 null；未知球队保留兼容的
`prior_rho_clipped`（仅未知球队调整时 true）。报告比赛行标「低比分修正受限」及前后值，Actions 摘要复用该行；
该场仍参与原评分，P2 / BET / 虚拟账户规则不变。正常已知球队保持原路径，旧模型默认保持跳过未知球队。
训练内校准仍使用原路径及原无效行排除规则；不重算 T33 旧产物。裁剪只保证概率合法，不代表极端强度已校准，
原 0–9 球截断及 `excluded_score_mass` 披露不变。

T06 冻结 JSON 仍兼容 schema 1/2；新批次 `validation_status=league_gated`，每场另有
`p2_ready/p2_label`，模型内附 params SHA256 与门槛证据。旧批次 experimental_unadopted
保持原渲染字节。读取新配置不会改变已拟合权重或旧报告；BET 始终 false。


## T21 欧战联合模型

`DixonColes(pooled=True)` 允许混合联赛历史，模型名仍为 dixon-coles，模型联赛为「欧战」。
同一规范球队名共享攻防参数，未知 `apif:team:*` / `fdorg:team:*` 原样作为独立身份；
不按名字猜测、不补别名、不重写数据库。全池只有一个 `home_advantage` 和一个低比分修正参数，
无联赛系数、Elo 或赛事类型主场偏移。先验、时间衰减与平局校准沿用 T15。
国内及单联赛 pooled 原样使用 penaltyblog 拟合路径。多联赛使用
`pooled_dc_full_gradient_al_slsqp_v1`：保留原 DC 负对数似然、参数边界、attack 和等式约束及最终
SLSQP 精度；为完整似然提供解析梯度（含四类低比分修正及原 `1e-9` floor 的导数）。
依赖原梯度漏掉低比分修正对进球强度的导数，不能把它与新路径称作数值等价。
同折、同边界的增广拉格朗日 L-BFGS-B 只求初值，固定最多 8 外步、每步 300 内迭代，
不引用其他折的参数或验证结果；最终仍经原目标的 SLSQP，独立检查成功、有限值、边界与 attack 和残差 ≤ `1e-6`。
预求解失败回落本折原始起点，最终失败拒绝产物。`fitting_method/solver_diagnostics` 保存这一路径，
参数条目和 P2 证据均绑定方法版本；单联赛路径不能冒用多联赛证据。数值加速不是预测改善证据。

参数条目 `欧战.leagues` 固定三个预测目标；`training_leagues` 为累计训练范围，`sources`
记录所选来源。默认范围是三项欧战，加已有国内联赛条目中、截止前通过球队 ID 有欧战参赛关系的联赛。
来源保持 `jc > fd > fdorg > apif`：只在 pooled 内存视图中选择**已经有 counterpart 链接**、
同联赛同主客身份且符合既有三小时时窗的高优先行，记录排除的低优先 match_id；
不推断新链接、不改采集/去重/可知时间规则。未链接或冲突的交叠继续由原训练守卫拒绝。
只读事务与读取前后库哈希检查仍生效。

模型保存 `training_leagues={实际联赛: 截点前场数}`、声明范围 `training_league_scope`、
`prediction_leagues`，以及 `team_training_leagues`。逐场 `predict_with_metadata` 另给
`pooled`、全池场数、`home_training_leagues` / `away_training_leagues` 和共同主场参数。
这些数量只来自严格早于训练截点、可知时间有效的记录；声明范围不能代替实际支持量。

`evaluate(target_leagues=...)` 训练用全语料，只对三项目标评分；统一 UTC 7 月换季，
保持周/月起点前一微秒冻结、跨季拆折和物理隔离 S+1 的选参规则。
逐场保留真实赛事名，`by_league` 给赛事 × 赛季的 model / uniform / market 与配对模型指标。
`home_mean/home_rate/home_bias` 分别为平均主胜概率、主胜频率、两者差值；共同主场参数是 log 进球强度尺度，
不是胜率百分点。报告披露这些指标，主队偏差只作为 T21a 的研究依据，不在本任务补偏移。
市场只读取 jc 严格赛前最后一份三项完整 had 快照并倒数归一化；缺失为 null。
欧战不读 closing_odds，赔率不进入模型、先验、校准或候选排序。

欧战单独门槛 `CUPS_GATE`：最新两完整季合计模型 n ≥ 500、相对等概率 LL 改善 ≥ 0.05、
jc 市场配对 n ≥ 300、配对模型 LL 不比市场差超过 0.05；weekly、两季非空、无跳过/不可知标签等规则保留。
证据同时绑定「欧战」组、三个目标和训练联赛范围；`model_readiness` 再核来源、实际模型范围、
参数/先验/校准、策略与证据时效。两库哈希继续采用 T16 A1 追溯规则。
市场证据不足即「P2 实验」，不借其他赔率或降低门槛。15 个国内条目及其 P2 证据保持原值，BET 禁用。

T06 注册键为 `(jc, 欧冠)`、`(jc, 欧罗巴)`、`(jc, 欧协联)`，共享一次训练的同一模型对象及版本；
冻结批次 `models` 只有一条「欧战」模型，预测行保留实际赛事名和来源，渲染器按声明目标校验证据。
T07 读取同一冻结目标映射；T19 仍按预测行赛事名计分和统计虚拟账户，无评分或账户算法改动。

## T17 定时训练与冻结预测

T39 保留 UTC 02:40 / 09:40，新增 UTC 07:40（北京15:40）。`schedule-gate` 只在新班次读取
数据分支 `state/sfc/` 的停售日期：当日/次日有期次运行，明确没有则结束，缺失、损坏、未知截止或
读取失败则保守运行。该班次跳过 apis，仅 sporttery → predict → scoreboard → digest。

T43 的 `workflow_dispatch operation=daily` 用于补跑 GitHub cron 排队迟到的整条链，三条 cron 与全局写锁不变。
工作流入口如下；旧操作的采集范围、失败处理和业务计算均保持原义：

| 入口 | 期次检查 | 后续 job |
| --- | --- | --- |
| schedule 02:40 / 09:40 UTC | 不运行 gate | apis → sporttery → predict → scoreboard → digest |
| schedule 07:40 UTC | schedule-gate | 允许时 sporttery → predict → scoreboard → digest；仍跳过 apis |
| daily，gate=false（默认） | 不运行 gate | apis → sporttery → predict → scoreboard → digest |
| daily，gate=true | schedule-gate | 允许时运行五个 job；明确无期次时五个均跳过 |
| apis / sync / predict / scoreboard / digest 等原手动操作 | 不受 gate 影响 | 保留各自原来的范围与后续 job |

gate 成功且 `run_early=false` 才拦住下游；无法读取或 gate 失败时沿用保守执行。apis 新增对 gate 的依赖，
使用 `!cancelled()` 避免正常跳过 gate 时被 Actions 隐含 `success()` 拦截；四个后续 job 沿用相同守卫。
daily 强制 `--slot auto` 和默认 days=3；按实际 UTC 09:00 分早晚，fd 仍仅 UTC 周一 early。
predict 保留 `needs.apis.result != 'failure'`，预测失败不妨碍记分板结算旧批次；digest 继续显式依赖 sporttery，
把其真实状态与本次 run ID 传给 daily_digest，更新次数沿用 T39 已送达版本计数，不改报告内容规则。
daily 各 job 摘要先写「手动整条链（gate=…）」；Windows sporttery 在设置好 Git Bash 后写入，gate 单独跳过时也有该标记。
dispatch 保持10项输入：新增 boolean gate 默认 false，将原 slot 放入已有 options JSON，仍为 auto/early/late、默认 auto，
采集前验证枚举；daily 不读取其他操作的 options。没有外部调度实现，不修改 docs/RUNNER.md。

运行来源只作归档元数据：`feed_manifest.json` 的 `run` 字符串不变，增加并列 `event_name` / `operation`；
`forecast_manifest.json.runs[run_id]` 增加同名字段。事件名取 `GITHUB_EVENT_NAME`，操作由 workflow 的
`ZUCAI_OPERATION=${{ inputs.operation }}` 传入；schedule 的 operation 为 null，daily 为 `workflow_dispatch` / `daily`，
非 Actions 默认 local / null。旧清单缺这两个字段仍可校验和恢复，表行、已冻结文件与 manifest 版本不变。

每次 schedule / daily 采集后（早晚班及迟到班次）或 `workflow_dispatch operation=predict` 运行 hosted
`predict` job，依赖 `apis` 和 `sporttery`；手动 apis 不触发预测。定时采集传 `--slot auto`，
以实际 UTC 09:00 为界判定 early/late，apis job 输出 slot；fd 仍仅 UTC 周一 early 采集。沿用工作流级 `write-football-feed-data` 锁，
`apis` 失败或整体取消时不运行，竞彩 job 跳过仍运行并提示「竞彩数据非今日」。手动 predict 不采集。
`sporttery_stale` 同时检查竞彩 job 结果、`through_beijing` 与 `collection.jc.schedule_status`；
赛程阶段非 `complete` 时即使 job 成功、赛果水位已到当日，也提示旧赛程与赔率未确认更新。旧归档缺该字段时默认 `complete`，兼容原判断。
安装现有 `[model]` extra，`ZUCAI_PARAMS` 指向 app 内的绝对配置路径。单次上限 45 分钟。

`cloud_predict.run` 在临时目录调用 `restore_feeds` 并记录工作库 SHA256。每个联赛调用现有 `train`，
来源严格取配置，国内条目只允许 jc/fd；T21 的「欧战」条目允许 jc/fd/fdorg/apif。
配置决定半衰期、先验和校准，默认累计历史、非 strict、无 replay。
`since=cutoff−56d` 仅缩短冒烟回测，训练仍使用截点前的累计可知历史；不导出逐场回测，不重算 P2 门槛。
单联赛失败记录原因、整次标记 partial，其他训练继续。模型 P2 标签由 `model_readiness` 计算。
欧战只训练一次，摘要附实际 `training_leagues` 场数；注册到三个 jc 杯赛键，归档模型键仍为「欧战」。

预测阶段按真实时间取北京今天与明天的开球日，仅选 jc 比赛 ID。复用 `predict_batch`，显式
`persist=False, allow_empty=True`，SQLite 连接 `query_only=ON`；普通 T06 CLI 的默认行为不变。
两个日期各自输出四件套，空日也有空批次；缺模型和已开球场次照常列出排除原因，不借用旧模型。
生成、导出及归档后再次检查开球时刻；任何预测已经开球则撤销本次发布。运行前后工作库和参数
SHA256 必须一致，不插入 Prediction、不迁移输入库、不改 schema。

数据分支以 `models/<run-id>/<league>.pkl`、`<league>.train.json` 保存本次训练，失败只有 JSON；
`models/latest.json` 保留每联赛最新成功模型，失败时仍指向上次成功项。
`forecasts/<YYYY-MM-DD 北京日>/<run-id>/` 保存 T06 四件套，不覆盖旧 run。
同一北京日的多个 run 分目录保留；记分板按生成时刻选择同场开球前最后一批，不按目录日期去重。
T25 摘要分别选今明开球日最新的冻结批次展示，不改变 T19 的逐场取批和评分规则。
独立 `forecast_manifest.json` v1 的 `runs` 记录训练库/参数 SHA256、截点、联赛状态和 P2、两日批次及
预测/排除数量；`files` 覆盖模型、训练 JSON、所有四件套与 latest 索引的 SHA256。
T45 追加 `parlay_sd.json` 及不可覆盖的内容哈希版本，均纳入同一清单；原四件套与 experimental.json 不改字节。
发布失败只回滚本次新目录与索引；已有冻结文件保持原字节。数据分支 Git 属性关闭新归档路径的换行转换。

`export_feeds` / `archive_info` 和 feed manifest 保持原接口。`cloud_feeds verify` 额外调用独立
`verify_forecasts`：验证路径、文件哈希、T06 内层清单、批次/模型对应关系和最新模型索引，绝不反序列化 pickle。
没有预测目录的旧归档仍通过；有预测而缺清单、损坏文件或符号链接则失败。
Actions artifact 保存本次模型、两日报告及索引 90 天，长期证据以数据分支为准。摘要复用实际 report.md
的比赛表，顶部固定免责声明；所有概率仍是纯模型，市场仅对照，BET 禁用。
T26 在 predict job 的两日冻结步骤后运行 `zucai.sfc_reports`，只读取当日最新 run 的两个冻件，不扩大模型预测日期范围。
SFC 步骤标 `continue-on-error`，失败保留运行记录并仍发布已成功冻结的竞彩数据；SFC 产物一并保存 artifact。


## T18 一次性 fd 历史回填

`cloud_feeds backfill` 恢复到全新工作目录的 `data/zucai.db` 后调用 `cloud_backfill.collect_backfill`。
`backfill_scopes` 只接受 verified 配置，默认九个按季联赛各 2122–2627、六个合并文件各筛 2021–2026；
显式联赛、起始/结束范围先校验。源文件每份独立处理；合并文件保持来源 Season 标签，按既有起始年函数筛选。

一次运行使用一个既有 `FootballDataCoUk` 下载器，逐文件落盘后通过 `fetch_fd(raw_file=..., assume=3h)`
离线导入。解析、别名、官方优先去重、结果可知时间和收盘赔率规则完全复用；不执行 API 历史采集或别名推断。
同一结果保留原可知时间；幂等依靠现有比赛/赛果/收盘价主键和导入规则，不增加 schema。
文件失败隔离到 `failed_scopes`，其他文件继续；行拒绝/疑似重复也使本次状态 partial。

回填专用 HTTP adapter 复用 `RateLimiter(2.1)` 在实际发送入口留出余量，原有共享 2 秒 limiter 不改。
禁用隐式 HTTP 重试，重定向仍由原采集器逐跳处理。robots 在同一限速通道读取并归档，每个计划 CSV
须被现有 ZucaiResearch UA 允许；robots 获取/校验失败不导入数据。记录全部开始时刻、跳转/失败状态和响应 SHA256，
CSV/robots 次数分列，少于两次 CSV 请求时最短间隔为 null。

新请求审计追加到工作库旁 `state/audit/fd_backfill_requests.csv`；原有 fd 拒绝/疑似重复/未知队名清单继续沿用。
每文件的统计与原件索引、每联赛前后 fd 赛果数写入 feed manifest 的 `collection.fd`，完整运行 JSON
和原始 HTTP 字节仅进 raw artifact。`export_feeds` / `archive_info` 格式不变，竞彩水位保持原值；
结束后恢复新归档并校验 feed 与已有 forecast manifest，旧模型/预测冻结目录保持原样。

`workflow_dispatch operation=backfill` 只运行 apis hosted job，60 分钟上限；共用既有写回锁、发布脚本和
90 天原件/审计 artifact，sporttery/predict 跳过。定时事件及周一当季采集逻辑不变。完成回填后另行手动
`operation=predict`，以实际训练场数核验历史覆盖，不改 params/P2 标签与门槛。

## T19 前瞻记分板与虚拟账户

T34a 另读 `experimental.json`，实验评分在同一 `scored.jsonl` 追加，以
`(match_id,batch_id,model_name)` 为键并标 `experimental_unadopted`。原评分键、正式累计与原证据门槛不变；
`paper.py` 仍仅接收原 `forecast.json`，不传入任何实验行。`probability.model_leagues` 增加正式模型名×联赛，
`experiments.groups` 按实验名×联赛分列自身有效评分、同场同批共同样本、LL/Brier配对差及UTC周整块自助区间。
选最新尝试时保留 missing 和迟到，计算评分时另检查开球前实际发布；缺失附件也不能缩小正式覆盖分母。
原先已追加的实验评分也保留，汇总仅选择当前有效批次。实际类零概率的实验 LL 序列化为 `Infinity`，
不改旧评分裁剪口径，也不让实验非有限损失阻止正式结算。

`python -m zucai.scoreboard run --archive <data-branch>` 校验既有 feed / forecast 清单，
恢复到临时数据库并用 SQLite `mode=ro` 读取常规时间赛果；结束复核库和输入清单哈希。
不迁移数据库，不读取价格表，不加载 pickle，不调用训练或来源采集器。

- `score.py`：通过 `load_batch` 读取全部冻件，保留 `batch_id/generated_utc/model_version` 和原件 SHA256。
  排除回放/事后生成，按生成时间及批次 ID 稳定排序，同场选择开球前最后一批；再核对身份、完整比分、
  `generated < available <= as_of` 与 captured/published 依据。三分类 LL 使用 1e-15 下界，Brier 为平方误差和，
  市场概率只由原 SP 倒数归一化，缺报价不剔除模型行。评分按比赛/批次追加去重；后到批次不删除原评分，
  汇总以当前最后一批的键筛选，每场只计一次。比分更正留审计，不静默改写已存证据。
- `paper.py`：按真实批次时序处理冻件，各门槛独立 A–E 账户。
  新批次只允许冻结行 `p2_ready is True` 进入候选及热门列表；false/缺失时 A/B/C × 三门槛各记
  `model_not_p2_ready`，D/E 候选不足沿用原原因；不重算 P2、不读参数文件，已处理批次与既有注单保留并照常结算。
  门槛比较的是 `ratio=p×冻结 SP`，严格 `ratio > 1.00/1.05/1.10`，等价于 `edge=ratio−1 > 0.00/0.05/0.10`；
  用十进制概率与 SP 直接相乘比较，恰好等于门槛不入选。单场选最大 ratio（与最大 edge 同序），
  C 只看最低 SP，串关同一北京开球日取前两/三场。
  A/B/C 每场只生成一次有效注单，D/E 每天只生成一次；跳过原因和已处理批次也进入 ledger。
  冻结字段 SHA256 不包括 settlement，结算只填后者。历史补记标明 replayed，旧批次迟到不回写资金历史。
  可用现金为初始 10000 减已投入加决定时刻已知回报，未结本金保留占用；凯利 1/4、2% 上限、10 下限，
  上下限或余额不能同时满足则跳过。金额使用十进制与分精度，不借款。串关全部结果齐全后才结算；缺失/取消保持待结。
- `statistics.py`：ROI 只用已结投入，资金/可用现金并列，最大回撤和连亏按结算时序；北京周资金表。
  稳健性去掉总回报最高的 ceil(5%)，固定种子 20261103、bootstrap 1000 次和 95% 分位区间。
  A/1.05 满足已结 n≥500、ROI>0、trimmed ROI>0、区间下限>−2% 时只写模拟证据标记，不改变 BET。
- `command.py` / `render.py`：先读并验证现有评分/账本，分阶段写四文件，异常恢复旧字节；独立 `summary.json`
  保存其他三文件 SHA256，维持原 feed/forecast 清单接口。输出联赛、联赛×版本、月份、总体三列指标及校准，
  默认展示 1.05，附表另两档，固定免责行及单关偏差。`--replay-days` 限定新补记批次，不伪造历史预测；
  已有累计记录保留，显式截点不得倒退已发布状态。`summary.gates.prospective_verified` 只表示至少一个联赛通过，
  真正标签按各联赛 n≥200 且模型 LL≤等概率独立判定。
- `evidence.py`：新 T06 批次可携带预测之前已存在的记分板证据和摘要 SHA256；默认未知即「仅历史证据」。
  只新增报告元数据/标签，P2 和模型概率不变。旧冻件缺新字段时维持原渲染，后续记分板变化不回写。
  `cloud_predict._summary` 仅复制原 `report.md` 的比分前五节，空批次不复制；不为摘要改写报告或哈希。

数据分支保存 `scoreboard/scored.jsonl`、`ledger.jsonl`、`summary.json`、`scoreboard.md` 四件套；
代码分支没有运行产物或 reports 新增。发布脚本显式暂存 scoreboard，并用 `scoreboard/** -text` 保持字节哈希。
`scoreboard` job 依赖 `[predict]`，早晚定时、手动 predict 后和手动 scoreboard 都可运行；预测跳过或
失败时仍能结算旧批次，取消则停止。托管机 30 分钟，共用写回锁，artifact 90 天，Actions 贴前 60 行。
漂移仅提示：四个连续完整且有样本的北京周平局偏差绝对值均>5个百分点，或最近100场LL高于等概率；不自动 tune。

## T45 单选＋双选：独立纸面组合与账户 F

`parlay/single_double.py` 只读同 batch 的原精度 TEAM 概率与同目录独立实验概率。
`status=predicted`、纯模型 `market_weight=0`、版本、身份、未开球、普通胜平负冻结 SP 都须成立。
`eligibility` 以规范 team_id 占用所有四队，并核对当前比赛主客、联赛和实际开球时刻；
`single=0/allUp=1` 可以过关审核，Selling/固定奖 had 不符排除；未知资格仅进参考。
市场倒数归一化单独构成 MARKET 参考榜，不填 model_probabilities。

`Config` 保留 single_direction_gap=0.10、double_fragile_gap=0.05 作为未经验证的实验筛选参数，
仅供 `legacy_filter_diagnostic` 对同批冻结输入做旧规则影响对照；threshold_filters_enabled 固定 false，
没有启用筛选的实验模式。max_groups=3（1–5）、rounding=ROUND_DOWN（或 ROUND_HALF_UP）不变。
默认不按差值排除；单选最高项完全并列时 selection/original_direction=null、原因 single_top_tied，
独立模型最高项不一致仍不能当单选。保留 top_probability、second_probability、direction_gap、
top_below_half；小于 50% 只是文字说明。双选三搭配保留 coverage_probability、omitted_probability、
selected_min_minus_omitted 与 selected_gaps；完全同分按 h+d、h+a、d+a，标 numerical_tie/tied_selections，
顺序不代表更可靠。双选无 1.20 限制，单选等于 1.20 仅价格通过。

伤停从 `experimental_archive.freeze_parallel` 已恢复技术行的隔离库经只读回调获取，复用 T38 strict
快照，不另下 Release、不写训练库。`confirmed_injured_out` 作为选择证据名映射 T38 `missing_n`，
保留 captured_utc、availability_basis、原 evidence 与 missing_reason；其实际含义是供应商 Missing Fixture
人数，不声称全为已确诊伤员。失败隔离且未知不填 0，绝不退用市场或主观概率。
`parlay/evidence.py` 将两队资料分 confirmed_none / confirmed_absences / unknown；缺失、覆盖不足、
捕获不早于截点时 confirmed_injured_out=null，不直接否决纸面比较。可选
`injuries[match_id].research_invalidation={reason, source, observed_utc}` 须有具体理由、来源且 observed_utc
严格早于预测截点，才记 research_invalidated 并排除该场两种配腿；不从人数或缺失推断研究失效。
组合的 injury_coverage 检查两场四队，missing/complete 随冻结记录和 F 注单保存。
`legacy_filter_diagnostic` 独立记录旧差值/伤停门槛的逐场原因、恢复参加排序的有序配对和分联赛数，
不反馈候选、排名、账户 F；恢复配对数不是新增票数，跨联赛配对在涉及联赛各计一次，不作收益或准确率结论。

每一证据等级完整枚举 9×N×(N−1)，保存枚举/合格/参考/各拒绝原因计数。用 Decimal 从冻结值的
十进制表示计算 q1+q2 后排序，同分按两个 match_id 及 h/d/a；贪心登记比赛与四队 ID。
主展示依次消费前瞻已验证、历史门槛通过、P2 实验等级，跨等级共用占用、总数不超过 max_groups；
不混级配腿，不宣称全局最优。独立实验与市场榜仅对照，不算新增组、不进 F。
另存每榜覆盖最高与盈利概率最高组；不计算跨组联合或「至少中一组」。

现金流固定两注各 2、总成本 4；跨场独立近似，逐分支金额为 `2×SP_A×SP_B` 按配置取分。
概率乘积、排序值另保留十进制字符串；利润严格 >0 才计盈利概率，=0 回本，<0 亏损；EV 与 ROI 分列。
默认取位及税费/奖金封顶未核实，只是纸面假设。所有组 BET=false、可执行性未核实、动作 PASS。

| 文件或字段 | 内容 / 不变性 |
| --- | --- |
| `forecasts/<日>/<run>/parlay_sd.json` | 当前主组、分级榜、实验与市场参考榜、诊断、配置、证据和两注现金流 |
| 同目录 `parlay_sd-<sha256>.json` | 不可覆盖副本；旧版本链仍可读，新批首次冻结后即锁定，不再替换 |
| `forecast_manifest.runs.*.forecasts.*` | parlay_path、parlay_versions、parlay_published_utc；files 纳入各版本 SHA256 |
| `scoreboard/parlay_sd_ledger.jsonl` | account=F、rule=single_plus_double_v2（兼容 v1）；两 ticket、一 group 与跨规则 batch 锁 |
| `summary.parlay_sd` | rules 按版本/伤停资料/证据/是否事后重建分列；totals 仅全账核对；待结原因、更正审计、ledger_sha256 不变 |
| `daily/<日>/<run>.json` | 另存 parlay_pending 与 parlay_versions，原 A–E pending 不动 |

`cloud_predict` 在原预测与实验已冻结后追加组合，不改原五个文件。组合发布前再次检查最早腿未开球；
parlay_published_utc 是归档写入时刻，不是官方停售凭据。schema_version=2、rule=single_plus_double_v2，
rule_effective_utc 记录新规则对本批开始生效的实际生成时刻，不倒填发布前的历史。
首次冻结即锁定（包括零组、尚未记账批次），换规则或配置也不重写；新规则只作用于新批次。
F 已有任意 v1/v2 记录的 batch_id 均不重开票；原决策哈希、赔率、注额、结算保留，旧文件无新增字段仍可读。
组合失败只记录错误，不撤销正式预测。日报只读取各日最新预测对应冻件（缺失不借旧批补齐），
去掉发报时已开球/状态变化组。今明批次按日期显示时共用四队/比赛占用与展示上限，
只删去重复或超限的冻结组、不重新配组或重算；原各批次 F 账本不受展示过滤影响。

`parlay/paper.py` 复用原身份和赛果可知时间校验，每张基础注仅追加 settlement；其余决定字段带哈希。
F 允许原模型 P2 实验组；原 A–E 的 P2 门槛、账户 D 规则和历史完全不变。组级 covered 与 profit 分开，
单选命中率、基础注结果、覆盖率、盈利率、已结投入/盈亏/ROI 按证据等级及 replayed 分列；
取消、缺赛果或身份变化保持待结，比分更正只提示。历史首次补记标 replayed，不冒充当时已开票。
F 账本与记分板在同一发布事务内写入，失败恢复旧字节；同批次重复运行账本字节不变。
日报附 F「较上一版」补结数、覆盖/盈利/亏损数，沿用 T39 更新序号和成功推送回执。

## T25 每日摘要与 Issue 推送

`python -m zucai.daily_digest --archive <data-branch> --out <data-branch>/daily` 先用
`archive_info`、`verify_forecasts` 和 `read_state` 校验 feed / forecast / scoreboard 的路径与字节哈希，
只读 JSON/JSONL，不恢复数据库、不反序列化模型、不调用预测或评分。输出 `daily/<北京日>.md` 与
完全相同的 `daily/latest.md`，首行固定「非购买建议 · BET 禁用」；只允许归档内 daily/ 或归档外目录，
拒绝符号链接。T39 同时写 `daily/<北京日>/<run>.md|json` 不可覆盖版本，含生成UTC、输入哈希、
本日更新序号、上一版引用与每账户待结 record_id / 最早腿开球 / 发报时分类；写失败一起恢复旧字节。
同run但不同输入或时刻另加内容后缀，相同输入重复生成字节一致；禁止倒退当日已有版本。
旧冻结文件与清单不改；daily目录仍由数据分支 Git 保存。

T39 页面依次是头部、今日与明日未开球预测、14场、复盘、昨日评分、虚拟账户、异常。
T45 在14场后增加纸面二串一小节与独立 F 收支；其余分节顺序不变。头部独立展示
`collection.jc.schedule_updated_utc/results_updated_utc` 最近成功时刻，前者标“竞彩接口最近成功检查”，
不能称为所有场次赔率更新时间；失败保留旧成功时刻，未知不以
through_beijing/归档生成时间替代。赛程阶段失败提示「赛程更新失败」，赛果阶段失败提示「赛果更新失败」，
赛果 `partial` 提示「赛果部分未到，还有 N 场待核对」，不将部分结果未到称为失败；兼容旧 `failed_stages`。
实际 sporttery job 失败也传入日报；若没有同次运行的失败阶段记录，提示「竞彩同步失败，失败阶段未记录」，不猜失败阶段。
各提示引用对应run，待核对数仅为本周冻结预测中已开球无已知赛果的场数；两阶段同时失败则分别显示。
`digest_odds` 将接口阶段结果、`collection.jc.odds_snapshots` 新增入库报价数、逐场冻结赔率时间分开显示。
报价数是各玩法选项的新增记录数，不是场数；成功但零新增时给出历史快照警告，不能单凭零新增判断停售
（十分钟内原价重采也可能去重）。日报已有 `run_id=GITHUB_RUN_ID-GITHUB_RUN_ATTEMPT` 与
`jc_sync_run_id=GITHUB_RUN_ID`，完整上下文传入 header/collection，严格比较归档 `collection.jc.run_id`
的运行编号和正整数重试次数，不再以前缀或缺少 attempt 的旧编号认定本次。发布状态必须明确 success；
本次阶段 complete、有效检查时间不晚于日报截点，才把计数解释为本次。不同尝试、发布 skipped/缺失/failure，
本次新增保持未知，归档最近一次结果/接口检查时刻/新增数单列。`jc_sync_status=skipped` 优先标“本次未执行采集”，
不误称发布失败，不把归档最近一次的零新增提醒写成本次零新增；无运行上下文的离线展示只称归档最近一次。
旧记录缺字段、阶段失败的初始 0 保持未知；竞彩 job 失败阶段的归属也沿用相同的完整尝试号匹配。
今明每批及复盘逐场读取 `forecast.json` 的 `market.snapshot_utc`，不取数据库当前最大报价时间，
不借 run/归档时间补全；比该批 feed_snapshot 接口检查早的时间标历史快照。部分更新不代表其他场次更新。
没有 market 为“未冻结赔率”，有报价但时间缺失/非法为“未知”；来源和概率不变。
仅日报调用 `verify_forecasts/load_batch(allow_unknown_odds_time=True)` 兼容旧冻件未知时间，保留原值且仍校验
全部文件哈希、概率与批次/模型一致性；默认读取、写入冻结报告、记分板等继续严格要求有效报价时间。
日报重放不改变预测、原清单、纸面账本或结算，不补报价、不重新计账。

每个开球日按生成 UTC、batch_id 取最新冻结批次（空批次不借旧场次补齐），
只显示 kickoff > digest.generated_utc 的比赛，按竞彩编号展示原胜平负、完整模型版本、P2 ready/experimental 与原 top_scores
前五，不排序或重算概率；excluded 行统一显示「未预测：原因」，不把已开赛等原因说成缺模型。数据来源链接指向对应数据分支文件。

昨日评分只过滤 summary.audit.active_keys，按 available_utc 的北京日归类；匹配原批次球队名称，缺失时
保留 match_id。日均 LogLoss/Brier 只对同一筛选结果中 `scoreboard/scored.jsonl` 的 `model.log_loss` /
`model.brier` 原值求算术平均，旁注「由当日 N 场逐场评分平均，非记分板原字段」，同时逐场显示原值。
均值在显示舍入前计算，日均展示四位小数；N=0 显示「昨日无已结算场次」，不填零或做除法。
这只是摘要汇总，不从概率和赛果重新评分，不写回 scoreboard 的任何文件、不改变其公式或输出。
累计、联赛、月度数字凡展示均直接取原 summary，不通过摘要的逐场子集重建。
两道门槛展示原联赛样本 n/200、A/1.05 已结数/500 与原布尔标记，样本数量不代替其余条件，BET 仍禁用。
复盘只读本周已开球记录，复用 `prematch_rows` 取每场最后有效赛前批次，与 active_keys 原评分核对；
赛后 excluded 不替换旧有效概率。首行统计已结/首项命中/未中/待核对（并列沿 h/d/a 次序），不重算损失。
`audit_rows` 默认仍只读取本周涉及月份；T46 日报清单/跳过核对明确传 all_shards=True，
读取 manifest 声明的各月分片及旧单文件，避免相关比赛捕获在更早月份时被漏掉，未来捕获行仍排除。
取每个营业日/编号的最新清单后，按实际北京开球日保留本周及未来场次；营业日仅作编号身份。
开球时间未知/非法时只保留本周一前一天起捕获的条目，保留未知，不据营业日猜日期；无关旧记录不进入日报。
原有 `archive_info` 全归档哈希校验保持，不能用选择性读取绕过文件完整性检查。
完整清单仍按营业日/编号逐项核对实际展示的预测集合，包括末尾编号；旧批有预测但未列入当前表也明确提示。
confirmed 与 reason_known 独立保存；已入库但无有效赛前预测只说明状态，不证明原因。
原因已知须有采集器支持的明确跳过原因、相同营业日/编号/ID和与最新清单相同的捕获时刻；
缺证据、过期或未知原因均归原因未查明并计入漏预测总数。单场原因不推断整批失败原因。
T46 在范围筛选之前区分证据是否可得：截点前没有任何可用清单行（包括只存在未来捕获记录）
传 None，显示“截点前没有可用清单，无法核对是否漏预测”，不输出漏项计数；已有截点前清单，
经范围筛选后为空则保留 []，说明范围内为空；范围内有行则正常核对。audit_rows 的共享接口不变。
部分营业日清单不为其他日期作完整性保证。
两表由原竞彩响应解析生成，保留条件不变；按营业日/编号/真实捕获时刻去重，不用名称绑定球队。

五账户并列 accounts.1.05 的 settled、settled_stakes、returns−settled_stakes、roi 和资金/现金；
聚合值不从裁剪账本重建。另照录昨日注单与冻结 SP/注额/返还/盈亏/replayed。
待结以本版 generated_utc 与最早一腿开球比较，等于开球算已开球；缺时间单列未知，不猜未开球。
“较上一版”仅用上版冻结待结ID集合，照录本版新增可见结算的中/错/原盈亏之和，账户分别展示，不与模型命中混算。

四来源状态、疑似重复与 partial 原因照录 feed_manifest.collection；T26 的 sync / apis 保留各来源最近状态，
旧归档缺记录时明确「未记录」，不从全局 complete 推断来源成功。APIF 配额缺少 collection 字段时可读 feed 清单中
已验哈希的 state/api_football_quota.json，注明快照 UTC 日期；不查询实时配额。页面标示采集归档时间与
记分板截点，旧快照不冒充本次采集结果。源文本含禁止购买措辞时失败，不发布被改写的摘要。

`digest` job 依赖 schedule-gate、scoreboard、sporttery，定时、手动 predict/scoreboard/digest 时运行，取消时停止；上游失败时仍可
展示最后归档的状态。手动 digest 时其他四个 job 均跳过（包括从旧采集标签发起），不采集、不训练、不结算。
沿用工作流全局写锁和发布脚本；显式暂存 daily/，设置 `daily/** -text`，发布成功才通知，artifact 保存 90 天。
工作流顶层仅保留 `contents: write`；`issues: write` 仅在 digest job 声明（连同该 job 发布数据需要的 contents）。
只有通知步骤得到显式 GITHUB_TOKEN 环境变量，源码仓库 checkout 不持久化凭证。

`python -m zucai.digest_notify --file <daily/latest.md>` 仅允许 Actions schedule/workflow_dispatch：
校验首行、唯一北京当日标记与单评论长度，正文与发布文件完全相同。REST 分页精确查找「每日报告 YYYY-MM」，
不存在则创建，GraphQL pinIssue 置顶；按 `<!-- zucai-daily:YYYY-MM-DD -->` 查找 github-actions[bot]
当天评论，找到则 PATCH，否则 POST。同名 Issue/同日机器人评论重复则报错，不任意挑选；整体由写锁串行化。
每月 1 日首次运行轮换（漏跑后补做），取消上月置顶，新报告成功后关闭上月；重试不重复建 Issue/评论。
T39 云端带 `--require-notified-history`，仅从已成功更新同日评论的版本选“上一版”。通知步骤在正文确认成功后
写独立 `<version>.notification.json`（issue/comment ID、正文SHA），再推送回执；通知失败不标成功。
回执缺失时不推断该版已发出；本地默认按归档版本离线对比，只表示重放，不证明实际通知。
不改无关 Issue 或人工评论。HTTP/GraphQL 失败显式报错，日志不输出 token 或响应正文。通知是否到手机、
编辑是否再次提醒、GITHUB_TOKEN 实际置顶权限由 Claude 的 Actions 运行验收，不以离线测试冒充实测。

## T26 14 场胜负彩每日诊断

`sources.sfc.collect_current_sfc` 仅在 sync 自建 runner 路径运行：复用 C500Sfc 的静态页面解析，
每班无参数 `/sfc/` 与 `/jczq/` 根页各 GET 一次，共用无重试 session 与至少1秒间隔，拒绝重定向。
`C500FixtureBridge` 解析出的 bridges 连同原URL/读取UTC/原件哈希传入 `ingest_issue(mapped_only=True)`；
校验 fixture_id、体彩 ID、来源主客球队 ID，沿用已有 counterpart、归档桥接及精确身份匹配。
T35：云端已由 ID 桥接/counterpart 确认的场次采用库内开球时间，不因500页面时间不同拒绝已确定的关联；
无 ID 桥接的精确身份匹配仍要求同开球时间。手工 T07 的桥接时间核验不变。
同场候选经 counterpart 或同联赛/开球/主客 ID 确认后，按 jc > fd > fdorg 取唯一最高优先级；
真实冲突及最高优先级并列仍 ambiguous。桥接命中体彩 ID 且两个500页面联赛标签一致时，云端允许
库内联赛显示名不同，原标签与 matched_identity.league 分存，不生成通用别名，手工 T07 联赛校验不变。
不创建或修改 Team/Match。未匹配/歧义的
snapshot 行保持 match_id、matched_identity 为 null，全部14行仍保留；只有已匹配行写 sfc_issue_matches。
旧手工 T07 默认行为保留。审计 sfc_unmatched.csv 按期号/序号/fixture/reason/candidates 去重，保留首次原件哈希。
T35 补 provider_home_id、provider_away_id、conflicting_identities 列；`conflicting_identity` 保存
来源双方 ID/名称、桥接双方 ID/体彩 ID、候选库内双方身份及冲突原因。下一次 sync 使用新桥接重试，
旧失败证据不等于已生效绑定；已有成功桥接与新桥接相冲突时仍拒绝该行并保留前后证据。
英冠/巴甲短名在 jc 开售后沿同一500 fixture/球队 ID 桥接补映射，不生成名字相似度规则。
SFC 失败不撤销已采集竞彩，collection.sfc 标 partial 并保存安全原因，手机摘要也显示该状态。
sync 另写 collection.jc（赛程处理数、赔率快照数、结果窗口数、未解决拒绝行及状态）；
请求总数与新入库数未计量则为 null，不把处理数误写为新增数。sync / apis 合并上一份来源状态，避免互相抹除。

`sfc_state.py` 从数据库导出 `state/sfc/<issue>.json`，含 schema_version=1、issue 全字段及 mapped matches；
纳入 feed_manifest 的路径/哈希清单。恢复时在临时数据库原表数据恢复之后校验14序号、fixture唯一性、
外键及快照/关联表一致性，再恢复现有 SfcIssue 表；不增加 schema，不改变原 TABLES 或旧归档兼容性。
早期只有期号、没有 snapshot 的记录留在原库，导出时记 warning 并跳过，不补造对阵，也不阻塞其他数据归档；
已存在但结构损坏的快照仍报错，不能按旧记录静默跳过。
不存在新造球队/比赛以填补外键的路径，原子替换目的库前完成校验。期次重采替换当前映射，原请求字节仍存 artifact。

`sfc_reports.py` 恢复归档到临时只读库，选北京当日最新一次 run 的两份 T06 JSON，保留原模型概率、版本及
逐行原批次时点。两份 JSON 以 forecast_sources 原路径、SHA256、完整正文存证；forecast_batch 只是引用合并视图，
不是新增模型推断。复用 T07 build_sfc_report 的身份/时间/模型核验和 optimize_cover 动态规划。
云端缺概率行显示「无模型」，概率/单选首项保持 null、复式强制三选；先扣除 3^missing 所需注数再优化已知场。
最少注数超64时 combo=null，显示最低注数和不可行原因，不伪造覆盖率。完整三选覆盖的缺模型行无需补造概率，
其余覆盖仍明确跨场独立近似。T35 候选生成条件为「截止前且所有已映射比赛未开球」，写入前再次检查
截止和这些库内开球时间；未映射行不参与已开球判断，显示「未映射：原因」。
快照/报告新增 `source_kickoff_utc` 保留页面时钟，`kickoff_basis` 为 database / provider / placeholder；
已映射行的 kickoff_utc 和 matched_identity.kickoff_utc 一律来自库内 Match。
未映射行仅在北京整点00:00且日期与同期期内其他场次不一致时标 placeholder，保留原值并注明待核实，
同日期的正常午夜场不自动标占位。报告每次只读重查未映射行，复用归档桥接并查询最新恢复库，
已映射行刷新库内时钟，仍核对原身份与冻结预测。不会改写源期次、球队、比赛或预测文件。

每期最新四文件在 `sfc/<issue>/{report.md,report.csv,sfc.json,manifest.json}`，相同内容冻结在
`sfc/<issue>/history/<run-id>/`；同 run 补上映射或来源证据变化时另存
`<run-id>-mapping-<rows/source哈希前16位>`，相同行内容/来源重复运行不新增，旧历史永不覆盖。
换批保留旧历史，写失败恢复当前视图。每份清单验证JSON/MD/CSV原字节，
引用的 T06 原件也核验；`sfc/** -text whitespace=cr-at-eol` 保留发布与恢复字节。

T39 新候选报告和manifest均记录 publication_schema=1、generated_utc（真正算完时刻）、sale_end_utc、
published_utc=null、sale_state=deadline_unconfirmed、publication_commit/evidence=null。
首次push后 `sfc_reports --confirm-publication` 从远程 data/football-feeds 可达的完整历史读取首次新增
candidate sfc.json 的提交 `%cI`，核对该提交原始blob SHA256；仅本地未推送提交、浅克隆、缺提交或哈希不符都不确认。
追加 `<candidate>-published-<commit12>` 四文件版本并刷新当前视图，再发布；原候选、forecast.json与哈希不变。
存好严格早于停售才标 published_before_deadline，等于或晚于标 missed_deadline；缺证据标 deadline_unconfirmed。
Git提交时刻只有整秒，计算时刻保留微秒；同秒截断仅用于检查发布时间是否早于计算，不改变停售的严格比较。
初次生成与确认都写Actions摘要，首轮push失败仍显示存好未知。Git提交时刻按任务单使用，不声称能查到服务器接收日志。
digest对旧报告可只读查Git补证据，无法取得则未知；这两种不合格状态只显示对阵及“无冻结概率”，无概率/复式列。
`read_reports` 默认过滤新格式未确认/迟到候选，旧诊断记录和手工T07兼容；不修改 scoreboard.sfc 的计分/结算公式。

`scoreboard.sfc` 选择截止且所有比赛开球前最后一份报告，不把事后重建作为诊断。只在14场全部有 finished、
完整90分钟比分、身份一致、captured/published 且报告之后/结算截点之前可知时追加 `sfc/settled.jsonl`。
每条含 issue_no、报告路径/哈希/生成时间、settled_utc、single_hits、single_evaluated、combo_all_hit、14行原概率/首项/复式项/赛果。
缺模型行不计首项命中，不可行复式的 combo_all_hit=null；一期仅追加一次，不覆盖旧诊断。
summary.sfc 保存 issues、average_single_hits、hit_distribution、latest、settled_sha256 与固定诊断/BET禁用标记。
记分板末节显示诊断；SFC JSONL 与记分板四文件一起写，失败一起回滚，既有概率评分和竞彩账户公式不变。

`sfc_digest.section` 只读并核验以上档案，在今明预测之后显示最新期号、第三方截止、14行概率、复式注数与覆盖、
上一已结算期命中数；没报告时仍从归档快照列14行。T35 将未映射、占位开球时间和无模型分开展示，
占位时钟注明待核实，缺概率的三选明确「全包＝无预测」。结算条件及记分口径不变，未齐14场身份/赛果继续待结。
不新增资金账户、不估奖金、不做任九。
真实 sync/predict/摘要/结算验收由 Claude 在 Actions 执行，离线 tests 不承担访问能力证明。

## T20 欧战采集与一次性 API 回填

`cloud_feeds backfill --source` 默认仍为 fd，完整保留 T18 路径；fdorg 只允许 CL，
默认从配置的最早可读年到按 7 月换季的当季；apif 默认 3/848 的已核验 2021–2026；显式选择还允许原九联赛，年份只取配置的 `seasons_available`。
API 范围只接受四位起始年，禁用未核验历史范围；真实套餐/分页失败记 partial，不当空成功。
沿用原下载器限速、原件校验、APIF 持久配额与 reserve=10，所有分页完整后才进入后续导入。

`cup_identity` 仅作用于三项欧战：先从同源生效的国内联赛数字 ID 与库内已确认的杯赛 ID 绑定
建立锚点，再复用 T09/T10 主客方向图；不改变国内联赛待审候选流程。只用比赛 ID、主客方向和
UTC 时间，不用队名相似或比分。源联赛的完整生效别名须不存在反向冲突，未出现在杯赛中的竞争 ID 也检查。
请求或响应带状态/轮次等筛选、解析拒绝或不完整分页的原件不能提供新图证据；
fdorg 还须有与原件行数相符的 `resultSet.count`，不能把缺计数的片段视为完整响应。
日期/赛季覆盖按赛事单独核对，相关竞彩比赛 ±180 分钟邻域必须完整。竞争边、跨原件身份和反向别名冲突不猜解。
回填先下载所选全部赛季、联合核验后才逐个离线重放，较新赛季的竞彩证据可用于较早季的同源 ID。
直接 fetch 重复指定多个杯赛 `--season` 也先联合核验，后续下载失败仍导入此前完整范围再报错。
日增量和单范围 fetch 在当前完整响应内核验；边界不足时保留未知身份。
旧 `fdorg_verify` 的国内联赛双循环完整性检查对 CL 明确给出不适用原因，不套用联赛队数或异常退出。

运行期生效别名不改仓库 JSON；证据追加 `state/audit/{fdorg,apif}_cup_aliases.csv`，
导入器通过既有 `teams.aliases` 保存来源 ID 绑定，随原表归档；之后导入复用已确认绑定。
未知身份仍使用来源前缀并写原未对齐 CSV。存量独立身份不自动提升或合并，沿用原导入器拒绝规则。
jc > fd > fdorg > apif、只填空 counterpart、常规时间比分、历史 assumed=3h、增量 captured 均保持原样。

`collection.<source>` 记录分赛事赛季解析、来源入库/赛果、链接竞彩、拒绝/疑似重复和球队对齐统计；
分母为该范围不同的球队数字 ID，跨季总体再次去重。Actions 摘要展示相同口径，日增量单列三项欧战。
不改变 schema、feed manifest 版本、导出表格式或既有冻结文件；仍恢复归档并校验来源行数与 forecast 哈希。
workflow_dispatch 的 source/leagues 通过环境变量和 Bash 参数数组传递，API key 仅从 Secrets 注入。
backfill 不触发模型训练，欧战模型由 T21 处理。

## T33 赛前技术特征与独立实验（第一轮）

T34a 的前瞻入口独立于下述历史回放命令，不改第一轮产物、协议或结论。

`features/stats.py` 从已恢复的 `match_stats` 建立只读版本索引，不创建表、不抓取数据。
`StatsFeatures.build` 的截点为 `min(as_of,kickoff)`；窗口保留同来源、同联赛最近5/10场
已结束比赛，历史开球+3小时必须严格早于截点。同场对手视角取同一 match_id 的另一队，
不重复累计 counterpart。缺比赛/统计、非90分钟、未知时段均保留缺失，不用更早场次凑数。
每项输出有效 n/N、算术均值、90天半衰期加权均值、比赛ID、原件哈希、版本与可知时间证据。
权重为 `exp(-ln(2)*距截点天数/90)`，只有 N 场都有效才输出均值；0有效，null不填0。
研究字段包括 xG/xGA、射门/射正/被射正、角球、主客子集、红牌比赛标记、T13休息天数；
对手DC参数只在参数截点早于比赛特征截点时返回。本轮阵容不入模，标 not_used_in_first_round。

时间口径分开保存：T34a 按所有者决定扩展 `strict`，backfilled 行以真实 captured_utc 作为
effective_available_utc，严格早于截点才可用，证据保留原 backfilled/available/captured；不改库中旧行。
T33 原数据的回填 captured_utc 晚于其历史截点，仍以 backfilled_not_known 拒绝，冻结协议与结果不变。
captured 行要求自身 available_utc 与 captured_utc 均早于截点，并且 available 不早于 captured。
`backfill_replay` 的规则不改，显式以历史
开球+3小时假设回填可用，携带真实后采时间，不称当时已知；最新回填可能含后来更正。
captured 更正仅选择截点前可用版本。训练样本的 feature_cutoff 是样本自身开球，
fit_cutoff 是该轮训练边界，两个时间分别保存，评价周不能倒灌进过去的训练特征。

`predict/experiments/intensity.py` 为独立 DC 强度回归：
`log λh=b0+home+b_for*log1p(home_for)+b_against*log1p(away_against)`，
`log λa=b0+b_for*log1p(away_for)+b_against*log1p(home_against)`。
只用四项同供应商近期衰减统计解释强度，以90分钟整数进球作似然标签；xG不强转整数，
射正数不当进球。截距、主场项、两项系数、低比分rho均在训练折拟合，系数边界仅作数值约束。
新名为 dc-xg-v1 / dc-sot-v1；消融去掉 against 项，两臂共同要求四项齐全、至少11场，
训练比赛及回退行集合一致。预测rho若需合法性裁剪则记录。仅复用T15训练折平局偏移，
不增加第二轮的特征校准层。TEAM 拟合、先验、校准、选窗均不读取赔率。

`evaluate.py` / `metrics.py` / `command.py` 实现冻结协议：2024/25月度选择5/10场窗口，
2025/26按T15周一及7月季初边界逐周重拟合；所有臂半衰期固定90天。
基线是原样 DixonColes 实现及未知球队先验，不声称复现线上后来调参的配置。
统计不足或拟合失败时两臂均回退对应基线，逐场保存实际与请求版本及缺失原因，
全目标和真正实验共同样本分开。只在所有拟合/选窗结束后，将明确counterpart且同联赛、
主客规范ID一致、开球相差不超过3小时的 jc had 截点前完整报价去水，作为 MARKET 列；
无同场同时报价则为 null，不用 fd 收盘价替代。赔率不进入历史训练帧。

指标包括不裁剪的LL（零概率为Infinity字符串）、三分类求和Brier、并列首项均分命中贡献、
10桶校准、平局偏差和覆盖率；比分Brier使用现行归一化0..9网格加概率0的其他类，
超网格真值仍评分，另留归一化前排除质量，不声称尾部已校准。六组配对对照固定同为raw
或同为calibrated，按UTC周整块自助2000次、seed3301、95%区间；跨联赛同周共用块。
联赛/赛季、冷启动、统计缺失分别汇总，欧战没有足够统计时明确未覆盖。
`group_comparisons.json` 对冷启动/统计缺失的两组分别给出全部六项配对区间，供应商和时间口径分开。
它只读取已保存预测，不重新拟合或选窗；记录生成代码版本和原预测文件哈希，可单独追加复核。
历史回放不是新增未见验证；无选择期实验时默认5仅作诊断，不宣称有效选参或模型改善。

入口用SQLite只读URI与query_only，并核对前后库哈希；输出禁止覆盖。
数据分支 `experiments/t33/<批次>/` 保存 protocol/input_manifest/summary、逐场预测及特征证据、
每折参数和训练ID、选窗输出（JSONL.zst）及SHA256SUMS。正式分支 reports 只提交
README和SHA256SUMS，合计不超过200KB。无生产模型注册，不修改 params/P2/BET/T28。

## T34a 并行模型与冻结输入、实际可知时间

`store_rows(..., daily=True)` 只在 Match.finished 且本次真实 captured_utc 严格晚于开球+2小时时
写 `availability_basis=captured / available_utc=captured_utc`；历史 backfill、未完赛、边界相等均保持
backfilled/NULL。相同原件/数值沿 T31 去重，不改已有行可知时间；更正新增 version，含历史值保留的
partial_null_retained 复合行仍标 backfilled，不把继承值伪装成本次观测。三张行表格式、分片、恢复列不变。
`stats_availability` 按三表全部存储版本计数，coverage 增加 availability_basis_counts、availability_rows；
每个请求范围行另加 captured_rows。fd 起始年2026映射供应商季2627；同范围不同 phase 引用同一存储计数，不能相加。

现有晚班 cloud_stats daily 原端点及预算不变。现有 apis job 的早班新增一次无体育网络请求的本地处理步骤：
若本工作目录已有普通采集下载的 fd 原件，调用 daily --endpoints fd。`stats_captured_fd` 只读取本UTC日已抓取、
不晚于运行时刻、在计划范围内的九主联赛CSV；URL/SHA/sidecar 仍经 load_raw 校验，按原件哈希记游标，requests=0。
早班这一步只保留所需 GITHUB_TOKEN，不传 API_FOOTBALL_KEY。
FD普通赛程抓取仍只有UTC周一early，不重复请求。回填 ingest_fd 仍只附着实体 fd 行；日常路径还允许不存在实体fd时，
沿唯一既有 jc.counterpart_id 精确链接、同联赛且开球±3小时附着；provider_fixture_id 保留原fd ID，
match_id/team_id来自既有jc比赛。store_rows/check_stats验证这个确切链接，多个候选拒绝；不创建/修改比赛球队、
不改任何导入器或 _Teams.resolve。外部行表仍先验证上传后接纳，失败照旧回滚。

`cloud_predict` 完成原模型四件套与清单发布后，再调用 `experimental_archive.freeze_parallel`。
另复制临时库、从既有 Release 恢复技术行，原训练库/参数/预测四件套不修改。恢复/计算/拟合/上传失败
隔离记录空缺，实验附加发布失败回滚实验文件和清单，不回滚正式预测。run记录 experimental_protocol 激活截点。
两个模型严格固定射正九联赛N10、xG英西意N10/德N5、半衰期90。所有者采纳 PR #68 建议修 1 后：
训练标签仍须 `kickoff < available_utc < 拟合截点`；训练特征在样本各自开球前用 backfill_replay，
对历史统计作开球+3h 可用假设，与 T33 一致。目标特征截点仍是当批原模型训练截点，使用 strict：
backfilled 行须真实 captured_utc 严格早于截点，有效可知时间取 captured_utc；原标记、捕获时间保留，
captured 行既有校验不变。已捕获的回填可以用于之后的预测，避免把存储标签误当成永远不可知；
不把训练回放冒称历史实时验证。T33 历史截点早于回填实际捕获时刻，原协议、测试与历史结果不改。
fits、每条训练证据与目标 features 均写 training_availability_mode=backfill_replay 和
target_availability_mode=strict_capture_time，实验文件头和预测行也保留两字段。availability_mode
在目标文件仍为 strict（兼容旧冻结读取），训练快照为 backfill_replay。沿用 StatsIntensity 原始未校准分布，
参数由拟合估计；特征/先验/选窗/拟合/校准不接收市场字段，不传正式模型作回退。

`experiments.history` 只在内存中按已核验 counterpart（规范主客ID和联赛一致、开球±3h）或完全相同
联赛/开球/主客ID合并历史出场；代表顺序 jc > fd > fdorg > apif，统计仍保留供应商、原 match_id/stat_id。
历史可结束边界用组内最晚开球+3h，训练特征截点用组内最早开球，冲突标签跳过并计原因。
这是实验窗口去重，不改变库内身份或正式训练数据选择。所有已知来源的同规范球队历史均进入窗口，
不只保留有统计的场次；目标最近N场有null/非90分钟/截点之后或相等时才捕获的统计即缺失，不往前补一场。
有效行冻结四项输入的n/N、历史ID、每项值、原件哈希、版本、captured/available、系数哈希和模型版本；
无效行冻结null概率及明确原因，始终p2_ready=false、experimental_unadopted。

每个两日目录的 experimental.json 由稳定排序JSON序列化，SHA256进入 forecast_manifest.files；
原 forecast/report 内层清单不变，旧归档无实验字段照常校验。attach_experiments 先写文件/哈希，
提交清单前记录 experimental_published_utc；记分板采用 generated/published 中较晚时间判断赛前。
load_experiments 按文件先完整校验再接纳行；JSON、身份、时间、概率或必要字段格式异常时整文件跳过，
同一比赛/模型重复、缺失原因缺漏、非法P2标签类型及夹带market对象也在该边界拒绝，避免进入下游后才失败。
将相对路径、批次、invalid_experimental_file 与错误明细写入 summary.experimental_audit.invalid_files，
不接纳该文件此前已解析的部分行。其他附件、正式评分和账本继续运行；归档 SHA256 校验不放宽。
完整每日JSON副本及训练样本特征、系数、训练ID打包不可变ZIP到 Release experiments-YYYYMM，
复用 stats_release.upload_verified/upload_checksums，不上传空校验清单。Release JSON与数据分支冻结副本
同字节；实际交付裁决以 forecast_manifest 时间为准，晚于开球则摘要记空缺且不评分。
数据分支 experiments/<批次>/ 不新增全量文件（本任务无需建立该目录）；若后续加索引，只能README/SHA≤1MB。
main无实验结果大文件，T33已入数据分支历史不重写。

summary.experiments.rules 固定：每联赛前瞻共同n≥200；实验−正式LL<0且UTC周整块2000次/seed3301自助
95%上限<0；共同样本三类概率等宽10桶、按桶内数量加权的绝对校准偏差不劣；共同有效数/实验启用后的
同联赛正式已评分数≥90%。迟到、缺附件、空缺留在分母，只匹配同比赛同批次。规则满足仅提示
“达到采用门槛，待所有者决策”，不替换模型、不改params/P2/BET/T28。日摘要照录 experiments 小节，
首行“实验 · 不下注”；原昨日结算均值先过滤实验评分，原累计和账户统计不混入实验。
真实验证包括首次晚班、captured积累一周及至少14天并行，由Claude执行，本地离线测试不冒充这些结果。

## T38 伤停特征与独立离线比较

`features/injuries.py` 是只读构建器，目标截点 min(as_of,kickoff)。strict 继承 T34a 的真实捕获时间规则：
backfilled 行的 effective_available_utc=captured_utc，严格小于截点才可用；保留 basis 与原 available=NULL。
captured 行要求 capture/available 均在截点前，不能用后来补抓的名单预测过去。选择最新合格页的完整快照；
本场名单缺失、最新页为空、身份不明或同次捕获内容冲突均返回 null 与原因，不沿用一份已被后续观察否定的旧名单。
人数按供应商 player ID 去重，不按名字相似合并。missing_n/questionable_n 为本场该队已知名单中两种原类型人数；
missing_last_starters_n 为缺阵 ID 与该队上一场首发 ID 的交集。上一场须真实已结束且开球+3h<截点，
不往更早比赛补首发；无完整阵容则该项 null。每项保留 n、captured、basis、effective time、原件、版本、缺失原因。
同场多来源先按规范 ID / 精确开球或已核验 counterpart 合组；上一场的结束边界与阵容回放时间使用组内最晚开球+3h，
目标截点及伤停名单回放假设使用组内最早开球，避免来源间时差令资料过早进入或错误排除。

训练单独用 backfill_replay：明确假设历史伤停名单在该场开球前已知（不证明当时版本），历史技术统计仍为 T33 开球+3h假设；
标签须 kickoff < available < 拟合截点。协议 v2 目标分 strict_capture_time 与 backfill_replay 两张独立表；
后者假设最近归档伤停名单开球前已知、统计在开球+3h已知，属于历史回放，不能充作前瞻证据。
两表独立选窗、拟合、指标/配对区间、阵容子集分析，不混算、不互相填空，口径进入路径、证据及参数文件。
T34b 在协议冻结时尚未合入，因此使用 dc-sot-v1；主候选只加 missing_n 主客差，客队取反，系数折内拟合。
missing_last_starters_n 原值作为双方阵容完整子集的附加分析列；缺失不填0，不阻断主候选。消融去掉人数差，与主候选共用完整训练掩码。
不改 T33 模块、生产注册或每日并行模型，原模型不作为候选的填空来源。

协议先提交于 docs/experiments/T38-*，时间切分、六列与六组配对、自助和分组见协议。无共同候选样本时报告证据不足，
不能把合成测试指标当精度结论，也不把回放训练当新增前瞻验证。选窗按共同候选场次，市场对照仅在全部拟合与选窗之后附加。
`python -m zucai.predict.experiments.injury_command` 只读库，输出禁止覆盖，输入前后 SHA256 一致才归档；
全量逐场证据/参数/指标/协议 ZIP 使用 stats_release.upload_verified/upload_checksums 上传 experiments-YYYYMM，
不写数据分支；main reports 仅 README/SHA256SUMS≤200 KB。上传失败不得声称已发布，保持本地证据供审查者接续。
