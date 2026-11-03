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
| `zucai/cloud_backfill.py` | 仅手动的 fd 六季历史回填、范围筛选、逐跳请求与联赛增量审计；复用既有下载/导入器 |
| `zucai/cloud_probe.py` | Actions 手动权限与计费探测，逐季第一页、CSV 审计、不入库 |
| `zucai/apif_budget.py` | 跨平台配额格式校验，兼容旧数据分支状态 |

| `zucai/cloud_api_backfill.py` | 仅手动的欧战及 apif 九联赛历史范围、欧战联合核验、配额守卫、分赛事赛季入库与前后对齐统计 |
| `zucai/sources/cup_identity.py` | 欧战运行期数字 ID 桥接与 T09/T10 主客方向图；证据写审计 CSV，未知身份保持来源编号 |
| `zucai/cloud_apis.py` | hosted API 编排、UTC 周一早班 fd、独立窗口失败与跨运行配额 |
| `zucai/cloud_predict.py` | 只读恢复库 → 按已发布参数多联赛训练 → 两个北京日的纯模型预测、Actions 摘要 |
| `zucai/forecast_archive.py` | 模型与报告追加归档、最新成功模型索引、独立 SHA256 清单与无 pickle 校验 |
| `zucai/scoreboard/` | 冻结预测与赛果配对、三列概率评分、五种虚拟账户、稳健性统计与文本记分板；不训练或联网 |
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
`ResultRowsRejected`。云端只捕获这个异常，继续后续日期窗口并归档有效数据，不捕获网络、分页或结构错误。
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
`git pull --rebase origin data/football-feeds`、普通 push，冲突保留原件 artifact 并失败。
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

### 存量 APIF 身份合并（T23 第 8 项）

`cloud_feeds reconcile` 是独立的手动 Actions 操作，不接收来源/联赛/年份选择，不访问网络或注入 key。
`apif_reconcile` 遍历 `apif:team:<正整数>` 临时行及其有比赛的每个联赛，只用正式 active 别名；
欧战直接复用 `cup_identity._bridge` 的同源 ID/既有绑定解析，不额外推断比赛图，不读取待审候选。
`proposed` 不处理。目标查找复用 `_Teams.one`（规范化 name/aliases），兼容 `auto_verified` 与具完整批准来源的 `approved`。
正式别名文件与导入器的“已有独立身份”拒绝规则保持原样。

逐联赛前置检查：目标唯一存在、临时 aliases 为空、目标无不同的本联赛 APIF ID，当前 ID 未被其他球队占用；
同联赛目标的任何比赛与待移动比赛相差 ≤3h 均算冲突，临时行若被非 APIF 比赛引用也跳过。
冲突只跳过该联赛/ID，写明原因；通过后仅更新 matches 的 home_team_id/away_team_id，追加来源绑定。
所有联赛不再引用临时行才删除它；无别名/未批准行保留。SQLite 事务内处理，审计写入失败时回滚数据库；
数据库最终提交失败时恢复原审计（原本不存在则移除），避免失败产物误报成功；本轮不发布。
match_id、开球、状态及其他字段、results/odds/closing_odds/predictions/sfc_issue_matches 均不改，无 schema 迁移。

`state/audit/apif_identity_merges.csv` 保存任务单九列，alias_source 包含正式文件哈希与解析证据；
按 `(league, team_id, canonical)` 保留一行，语义不变时保留原 run_id/字节；冲突解决后的成功更新原行而不重复追加。
`collection.apif.merged_teams` 计本轮实际移动过的不同临时行，`rekeyed_matches` 计不同比赛 ID（主客都移动也只算一场），
`deleted_teams` 计最终删掉的无引用行，`skipped_conflicts` 只计已有解析结果却未通过检查的联赛/ID。
没有 active 解析结果单列 unresolved_pairs，不计冲突。队表减少数等于 deleted_teams，比赛数保持不变。
恢复/导出仍使用原数据分支格式与全局写锁，竞彩水位不前进；再次恢复及 verify_forecasts 校验后发布。

`apif_cup_aliases.csv` 改为按球队状态和语义证据去重并整理旧重复行：比较 evidence 时递归忽略
`seen_utc`、`sidecar`，保留首次原始证据供追踪；不同 raw_sha256、规范名、状态或实际匹配依据仍保留。
不修改 fdorg 的别名审计写法。新审计仍走既有 `state/audit/apif_*.csv` 归档白名单。

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
对未知组合的 rho 仅作非负概率约束，逐场标记裁剪。已知球队保持原路径，旧模型默认保持跳过未知球队。

T06 冻结 JSON 仍兼容 schema 1/2；新批次 `validation_status=league_gated`，每场另有
`p2_ready/p2_label`，模型内附 params SHA256 与门槛证据。旧批次 experimental_unadopted
保持原渲染字节。读取新配置不会改变已拟合权重或旧报告；BET 始终 false。


## T17 定时训练与冻结预测

每次 schedule 采集后（早晚班及迟到班次）或 `workflow_dispatch operation=predict` 运行 hosted
`predict` job，依赖 `apis` 和 `sporttery`；手动 apis 不触发预测。定时采集传 `--slot auto`，
以实际 UTC 09:00 为界判定 early/late，apis job 输出 slot；fd 仍仅 UTC 周一 early 采集。沿用工作流级 `write-football-feed-data` 锁，
`apis` 失败或整体取消时不运行，竞彩 job 跳过仍运行并提示「竞彩数据非今日」。手动 predict 不采集。
安装现有 `[model]` extra，`ZUCAI_PARAMS` 指向 app 内的绝对配置路径。单次上限 45 分钟。

`cloud_predict.run` 在临时目录调用 `restore_feeds` 并记录工作库 SHA256。每个联赛调用现有 `train`，
来源严格取配置且只允许 jc/fd；配置决定半衰期、先验和校准，默认累计历史、非 strict、无 replay。
`since=cutoff−56d` 仅缩短冒烟回测，训练仍使用截点前的累计可知历史；不导出逐场回测，不重算 P2 门槛。
单联赛失败记录原因、整次标记 partial，其他训练继续。模型 P2 标签由 `model_readiness` 计算。

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

`python -m zucai.scoreboard run --archive <data-branch>` 校验既有 feed / forecast 清单，
恢复到临时数据库并用 SQLite `mode=ro` 读取常规时间赛果；结束复核库和输入清单哈希。
不迁移数据库，不读取价格表，不加载 pickle，不调用训练或来源采集器。

- `score.py`：通过 `load_batch` 读取全部冻件，保留 `batch_id/generated_utc/model_version` 和原件 SHA256。
  排除回放/事后生成，按生成时间及批次 ID 稳定排序，同场选择开球前最后一批；再核对身份、完整比分、
  `generated < available <= as_of` 与 captured/published 依据。三分类 LL 使用 1e-15 下界，Brier 为平方误差和，
  市场概率只由原 SP 倒数归一化，缺报价不剔除模型行。评分按比赛/批次追加去重；后到批次不删除原评分，
  汇总以当前最后一批的键筛选，每场只计一次。比分更正留审计，不静默改写已存证据。
- `paper.py`：按真实批次时序处理冻件，各门槛独立 A–E 账户。门槛比较的是 `ratio=p×冻结 SP`，
  严格 `ratio > 1.00/1.05/1.10`，等价于 `edge=ratio−1 > 0.00/0.05/0.10`；
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

## T25 每日摘要与 Issue 推送

`python -m zucai.daily_digest --archive <data-branch> --out <data-branch>/daily` 先用
`archive_info`、`verify_forecasts` 和 `read_state` 校验 feed / forecast / scoreboard 的路径与字节哈希，
只读 JSON/JSONL，不恢复数据库、不反序列化模型、不调用预测或评分。输出 `daily/<北京日>.md` 与
完全相同的 `daily/latest.md`，首行固定「非购买建议 · BET 禁用」；只允许归档内 daily/ 或归档外目录，
拒绝符号链接。两文件写入失败恢复旧字节。旧冻结文件与清单不改，摘要历史由数据分支 Git 保存。

页面依次是头部、今日与明日预测、昨日结算、虚拟账户、异常。每个开球日按生成 UTC、batch_id 取最新
冻结批次（空批次不借旧场次补齐），按联赛展示原胜平负、完整模型版本、P2 ready/experimental 与原 top_scores
前五，不排序或重算概率；excluded 行统一显示「未预测：原因」，不把已开赛等原因说成缺模型。数据来源链接指向对应数据分支文件。

昨日评分只过滤 summary.audit.active_keys，按 available_utc 的北京日归类；匹配原批次球队名称，缺失时
保留 match_id。日均 LogLoss/Brier 只对同一筛选结果中 `scoreboard/scored.jsonl` 的 `model.log_loss` /
`model.brier` 原值求算术平均，旁注「由当日 N 场逐场评分平均，非记分板原字段」，同时逐场显示原值。
均值在显示舍入前计算，日均展示四位小数；N=0 显示「昨日无已结算场次」，不填零或做除法。
这只是摘要汇总，不从概率和赛果重新评分，不写回 scoreboard 的任何文件、不改变其公式或输出。
累计、联赛、月度数字凡展示均直接取原 summary，不通过摘要的逐场子集重建。
两道门槛展示原联赛样本 n/200、A/1.05 已结数/500 与原布尔标记，样本数量不代替其余条件，BET 仍禁用。
五账户只读 accounts.1.05 的资金、现金、ROI；账本只展示该档昨日生成或昨日结算的 ticket，保留原注额、
SP、返还、盈亏与 replayed 标记。聚合账户值不从裁剪后的账本重建。

四来源状态、疑似重复与 partial 原因照录 feed_manifest.collection；T26 的 sync / apis 保留各来源最近状态，
旧归档缺记录时明确「未记录」，不从全局 complete 推断来源成功。APIF 配额缺少 collection 字段时可读 feed 清单中
已验哈希的 state/api_football_quota.json，注明快照 UTC 日期；不查询实时配额。页面标示采集归档时间与
记分板截点，旧快照不冒充本次采集结果。源文本含禁止购买措辞时失败，不发布被改写的摘要。

`digest` job 依赖 scoreboard，定时、手动 predict/scoreboard/digest 时运行，取消时停止；上游失败时仍可
展示最后归档的状态。手动 digest 时其他四个 job 均跳过（包括从旧采集标签发起），不采集、不训练、不结算。
沿用工作流全局写锁和发布脚本；显式暂存 daily/，设置 `daily/** -text`，发布成功才通知，artifact 保存 90 天。
工作流顶层仅保留 `contents: write`；`issues: write` 仅在 digest job 声明（连同该 job 发布数据需要的 contents）。
只有通知步骤得到显式 GITHUB_TOKEN 环境变量，源码仓库 checkout 不持久化凭证。

`python -m zucai.digest_notify --file <daily/latest.md>` 仅允许 Actions schedule/workflow_dispatch：
校验首行、唯一北京当日标记与单评论长度，正文与发布文件完全相同。REST 分页精确查找「每日报告 YYYY-MM」，
不存在则创建，GraphQL pinIssue 置顶；按 `<!-- zucai-daily:YYYY-MM-DD -->` 查找 github-actions[bot]
当天评论，找到则 PATCH，否则 POST。同名 Issue/同日机器人评论重复则报错，不任意挑选；整体由写锁串行化。
每月 1 日首次运行轮换（漏跑后补做），取消上月置顶，新报告成功后关闭上月；重试不重复建 Issue/评论，
不改无关 Issue 或人工评论。HTTP/GraphQL 失败显式报错，日志不输出 token 或响应正文。通知是否到手机、
编辑是否再次提醒、GITHUB_TOKEN 实际置顶权限由 Claude 的 Actions 运行验收，不以离线测试冒充实测。

## T26 14 场胜负彩每日诊断

`sources.sfc.collect_current_sfc` 仅在 sync 自建 runner 路径运行：复用 C500Sfc 的静态页面解析，
每班无参数 `/sfc/` 与 `/jczq/` 根页各 GET 一次，共用无重试 session 与至少1秒间隔，拒绝重定向。
`C500FixtureBridge` 解析出的 bridges 连同原URL/读取UTC/原件哈希传入 `ingest_issue(mapped_only=True)`；
校验 fixture_id、体彩 ID、来源主客球队 ID 与开球，沿用已有 counterpart、归档桥接及精确身份匹配。
同场候选经 counterpart 或同联赛/开球/主客 ID 确认后，按 jc > fd > fdorg 取唯一最高优先级；
真实冲突及最高优先级并列仍 ambiguous。桥接命中体彩 ID 且两个500页面联赛标签一致时，云端允许
库内联赛显示名不同，原标签与 matched_identity.league 分存，不生成通用别名，手工 T07 联赛校验不变。
不创建或修改 Team/Match。未匹配/歧义的
snapshot 行保持 match_id、matched_identity 为 null，全部14行仍保留；只有已匹配行写 sfc_issue_matches。
旧手工 T07 默认行为保留。审计 sfc_unmatched.csv 按期号/序号/fixture/reason/candidates 去重，保留首次原件哈希。
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
其余覆盖仍明确跨场独立近似。未截止且尚无开球场次才发布，写入前再次核截止。

每期最新四文件在 `sfc/<issue>/{report.md,report.csv,sfc.json,manifest.json}`，相同内容冻结在
`sfc/<issue>/history/<run-id>/`；同 run 不覆盖，换批保留旧历史，写失败恢复当前视图。每份清单验证JSON/MD/CSV原字节，
引用的 T06 原件也核验；`sfc/** -text whitespace=cr-at-eol` 保留发布与恢复字节。

`scoreboard.sfc` 选择截止且所有比赛开球前最后一份报告，不把事后重建作为诊断。只在14场全部有 finished、
完整90分钟比分、身份一致、captured/published 且报告之后/结算截点之前可知时追加 `sfc/settled.jsonl`。
每条含 issue_no、报告路径/哈希/生成时间、settled_utc、single_hits、single_evaluated、combo_all_hit、14行原概率/首项/复式项/赛果。
缺模型行不计首项命中，不可行复式的 combo_all_hit=null；一期仅追加一次，不覆盖旧诊断。
summary.sfc 保存 issues、average_single_hits、hit_distribution、latest、settled_sha256 与固定诊断/BET禁用标记。
记分板末节显示诊断；SFC JSONL 与记分板四文件一起写，失败一起回滚，既有概率评分和竞彩账户公式不变。

`sfc_digest.section` 只读并核验以上档案，在今明预测之后显示最新期号、第三方截止、14行概率、复式注数与覆盖、
上一已结算期命中数；没报告时仍从归档快照列14行和「无模型」。不新增资金账户、不估奖金、不做任九。
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
