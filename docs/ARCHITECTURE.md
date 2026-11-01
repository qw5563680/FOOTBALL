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
| `zucai/features/` | `build_features(match_ids) -> DataFrame` |
| `zucai/predict/base.py` | `Model` 抽象类：`fit(df)`、`predict_score_matrix(row) -> ndarray[10,10]`、`save/load` |
| `zucai/predict/dixon_coles.py` | 基线模型，带时间衰减的泊松攻防强度 |
| `zucai/predict/derive.py` | 由比分矩阵推导各玩法概率 |
| `zucai/report/` | markdown / csv 输出，期望值计算 |

### 快照与重放

抓取器用 `RawStore.save_response()` 保存 HTTP 响应的原始字节，同时生成同名的
`.meta.json`，记录最终 URL、HTTP 状态码和 UTC 采集时刻 `fetched_at_utc`。
同一来源同一时刻的多次抓取各有独立文件。原始响应出现时，元数据已可读取。
单独导入字节可使用 `save(source, name, content, ext)`，缺失的 HTTP 字段保存为 null。
重放时将 `load(path)` 返回的字节传给纯函数 `parse`，从 `load_metadata(path)` 取回
原采集时刻，用作赔率的 `snapshot_utc`。测试环境统一阻止未登记的 HTTP 请求。

## 数据模型

T07在既有 `sfc_issues` 中新增可空 `observed_utc/source_url/raw_sha256/date_basis/snapshot`。
`snapshot` 保存14场原始身份、来源fixture/球队ID、匹配状态、候选ID及已匹配的规范身份。
`mapping_evidence` 记录counterpart或当前公开竞彩页ID对照（附URL、读取UTC和SHA256）；
查找不修改Team，仅确需新增未匹配比赛时创建未知球队。原联赛标签与规范身份标签分别保留。
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
| `source` | `jc` / `c500` |
| `league` | 联赛名 |
| `home_team_id` / `away_team_id` | 外键 `teams.id` |
| `kickoff_utc` | 开赛时间 |
| `jc_num` | 竞彩编号，如 `周六001`，可空 |
| `status` | `scheduled` / `finished` / `cancelled` |
| `result_checked_utc` / `result_provider` | 最近一次被接受的赛果或取消状态的观察时间与来源；阻止旧快照倒写 |
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
半全场推导以终场比分为条件，近似假定各粒进球均匀落在上下半场。新球队或样本不足时报告跳过场数。

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
