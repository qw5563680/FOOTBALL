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
其余合格场次继续处理。存在拒绝记录时命令返回非零，云端不发布该批。重跑保留已冻结的预测值。

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

单次预测由比赛、模型名、模型版本和生成时间共同区分。三项概率均在 [0, 1]，
总和与 1 的误差小于 0.000001。比分矩阵的形状由 T05 的模型层校验。

## 模型路线

1. **Dixon-Coles**（T05）：每队攻击/防守强度 + 主场优势 + 低比分修正，按时间衰减加权拟合。
   不需要任何外部特征，只要历史比分，适合作为基线。
2. **赔率基准**（T05/T06）：官方赔率去抽水后的隐含概率只作为回测对比基准和期望值分母，不作先验、不默认融合、不进入模型输入。融合实验必须显式开启并以独立模型名另存。
3. **GBDT**（后续）：在特征表上用 LightGBM/sklearn 预测胜平负，再与比分模型融合。

## 评估

`zucai train` 必须输出滚动回测指标：对数损失、Brier、按赔率区间分桶的命中率。
评估只用比赛开始前的信息，严禁使用赛后数据构造特征。
