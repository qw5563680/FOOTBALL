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
| `zucai/cloud_feeds.py` | `apis` / `sync` / `verify` / `export`；四来源 v2 归档与 v1 恢复、SHA256、运行摘要 |
| `zucai/cloud_apis.py` | hosted API 编排、UTC 周一早班 fd、独立窗口失败与跨运行配额 |
| `zucai/cloud_aliases.py` | 复用 T09/T10 离线身份图，只追加带证据的待审候选 |
| `zucai/sources/fd_verify.py` | 离线复核当前赛季与 jc 的身份图、候选时区和覆盖缺口 |
| `zucai/features/` | `build_features(session, match_ids=None, as_of=None, *, strict_availability=False) -> DataFrame`；43个球队字段，市场/元信息分离 |
| `zucai/predict/base.py` | `Model` 抽象类：`fit(df)`、`predict_score_matrix(row) -> ndarray[10,10]`、`save/load` |
| `zucai/predict/dixon_coles.py` | 基线模型，带时间衰减的泊松攻防强度 |
| `zucai/predict/derive.py` | 由比分矩阵推导各玩法概率 |
| `zucai/report/` | markdown / csv 输出，期望值计算 |

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
期次表与报告尚未加入cloud_feeds自动归档，当前真实验收快照通过PR保存在私有仓库。

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
`collection` 与 `collection_status` 描述最后导出 job 的批次；此前 job 的运行摘要保留在 Actions，
manifest 不充当跨 job 历史运行日志。

没有数据库迁移；T14 不改变解析、去重、比分可知性或训练输入。

## T15 评估与参数文件（无数据库迁移）

`predict/seasons.py` 定义 UTC 跨季/自然年边界，`backtest.py` 在价格表与结果历史分离的前提下，
按周或月冻结、跨季累积训练，并输出赛季、总体、同场收盘、未知球队及 jc 链接子集。
`predict/tuning.py` 从赛季 S 的完整 monthly walk-forward 选择半衰期，仅在 S+1 验证，
最终另跑 weekly，`predict/parameters.py` 原子更新 `data/model/params.json` 的选择时刻、窗口、
数据库哈希、来源、先验政策及 P2 门槛证据。最近两完整季的规则见 README 评估一节。
训练入口只读同一数据库快照；模型输入只含球队身份、比分和可知时间。赔率不进入先验或选参。
未知球队按最弱四分位攻防向联赛均值收缩50%，沿用训练集的主场优势和低比分修正；
对未知组合的 rho 仅作非负概率约束，逐场标记裁剪。已知球队保持原路径，旧模型默认保持跳过未知球队。

T06 冻结 JSON 仍兼容 schema 1/2；新批次 `validation_status=league_gated`，每场另有
`p2_ready/p2_label`，模型内附 params SHA256 与门槛证据。旧批次 experimental_unadopted
保持原渲染字节。读取新配置不会改变已拟合权重或旧报告；BET 始终 false。
