# FOOTBALL · 中国足彩比赛分析与预测

用公开数据对中国体育彩票的足球玩法做数据分析和结果预测。核心是一个
**每场比赛的比分概率分布模型**：有了比分分布，竞彩胜平负、让球胜平负、
比分、总进球、半全场，以及 14 场胜负彩/任选 9 场，都可以从同一个分布推导出来。

> 本项目仅做数据分析与概率估计，不构成任何投注建议。

## 支持的玩法

| 玩法 | 预测输出 | 数据来源 |
| --- | --- | --- |
| 竞彩 胜平负 (had) | 主胜 / 平 / 客胜概率，对比官方赔率算期望值 | 中国竞彩网 |
| 竞彩 让球胜平负 (hhad) | 带让球线的三项概率 | 中国竞彩网 |
| 竞彩 比分 (crs) / 总进球 (ttg) / 半全场 (hafu) | 由比分分布推导 | 中国竞彩网 |
| 传统 14 场胜负彩 / 任九 | 每期 14 场的胜平负概率表与组合建议 | 体彩官网 / 500.com |

## 技术栈

- Python 3.11+，命令行工具 `zucai`
- SQLite 存储（单文件 `data/zucai.db`），SQLAlchemy 建模
- `requests` + `lxml` 抓取，抓到的原始页面落盘到 `data/raw/` 便于回放
- `pandas` 做特征，`scikit-learn` / `scipy` 做模型（起步用 Dixon-Coles 泊松模型，再加梯度提升）
- `pytest` 测试，抓取逻辑全部基于 `data/fixtures/` 里的离线样本测试

## 快速开始

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
zucai --help
zucai db init                         # 建表，已有比赛和快照会保留
zucai fetch jc --date 2026-11-01      # 抓取该竞彩编号日在售赛程及各玩法报价
zucai fetch results --days 30         # 回填最近30个北京时间自然日的已开奖比分
zucai fetch results --from 2026-10-01 --to 2026-10-31
zucai fetch sfc --issue 26156 --matches-out inputs/26156.csv
zucai train --model dixon-coles       # 训练并按周回测
zucai train --strict-availability    # 排除 assumed 训练，并列报告默认和严格回测
zucai features build --out data/features.parquet  # 提取球队特征，市场对比列单独隔离
pytest -q
```

数据库默认位于 `data/zucai.db`。可以设置 `ZUCAI_DB`，也可用
`zucai db init --path /path/to/football.db` 明确指定文件。
同一场比赛可以保存不同采集时间的赔率和不同模型版本的预测。

### 云端采集

在 GitHub 的 `data/football-feeds` 分支打开 [按月赛程和赛果](https://github.com/qw5563680/FOOTBALL/tree/data/football-feeds/matches)。
同分支 `state/odds/` 按采集月份保留完整赔率。数据分支保存规范化表和校验值，可在其他机器恢复。
工作流合入默认分支并设置仓库变量 `FOOTBALL_FEEDS_RUNNER` 后，每日计划北京时间 11:17、18:17 在云端运行。
此变量填写已注册、经数据源允许访问的云端 GitHub Actions runner 标签。首次通用托管机实跑被体彩站点
安全策略以 HTTP 567 拒绝；未配置可访问运行器时定时采集保持停用。Actions 中的
**Football cloud feeds → Run workflow → verify** 可在通用托管机恢复并校验数据分支。
**sync** 在配置的运行器上采集；维护者也可推送 `football-ingest-verify-*` 或 `football-ingest-run-*` 标签
验证相应路径。同步恢复上次成功提交的数据，复查最近三天、遗漏日期及逾期未结场次。
单场赛果校验失败时，合格赛果和赛前报价仍归档，批次标记为 `partial`，Actions 摘要显示待补抓数量。
拒绝历史存入同分支 `state/rejected_results.csv`，与其他数据一起校验和恢复；未解除的日期在下次同步
额外重抓，包括超出最近三天、尚未建场次或已有旧比分的情况。只有更晚被接受的同来源赛果才能解除，
某场从后续页面消失不算修复。异常历史保留，待补抓归零后批次标记为 `complete`。
网络拒绝、分页缺失、数据库或归档校验失败及推送失败仍保留原云端版本。
原始 HTTP 响应作为该次 Actions 的私有附件保留 90 天。

### 运行公开预测样例

在仓库根目录使用 Python 3.12。安装模型依赖后，一条命令会下载并校验公开的 2019/20 英超样例，
回放阿森纳对曼联，随后对 2020 年 1 月起的比赛按周回测。比赛和模型只写入临时目录。

```bash
python -m pip install -e ".[model]"
python examples/t05_public_replay.py
```

也可将同一来源的原始 `E0.csv` 传给 `python examples/t05_public_replay.py --csv E0.csv` 离线运行。
样例以 UTC 开球后 3 小时作为赛果已知时间，这是假设。主模型只接收主客队、常规时间比分和时间信息。
CSV 的 Bet365 收盘价列 `B365CH/B365CD/B365CA` 仅用于单独的回测对照，发布方未给出逐场报价时刻。
样例同时列出 90 天半衰期和每日衰减 0.001 的纯模型指标，均使用同一批留出比赛。
[英超原始 CSV](https://www.football-data.co.uk/mmz4281/1920/E0.csv) · [数据来源与使用说明](https://www.football-data.co.uk/data.php)

有赛果数据库后，用 `zucai train --model dixon-coles` 训练并保存至 `models/dixon-coles.pkl`。
`--db` 指定数据库，`--league` 和 `--source` 选择同一赛事和供应方；多个范围须分别训练。
`--since YYYY-MM-DD` 指定回测开始日，之前的赛果可用于训练。
`--as-of YYYY-MM-DD` 将所有数据截在当日 UTC 零时。可用 `--output` 另存每次的模型。
训练输出包含样本数、逐周截点、对数损失、三分类 Brier 及校准分箱。市场样本缺失时指标为 null。
`availability_basis` 为 `mixed` 时必须连同 `assumed_training_share` 和 `assumed_eval_share` 解读；
顶层与每周 fold 分别披露训练和评估样本中固定延时假设的比例。顶层训练比例按各 fold 的训练样本数
加权，评估比例按实际成功预测的比赛数计算。NULL 来源按 `captured` 处理，可知时间缺失仍排除。
`--strict-availability` 排除所有 `assumed` 行后训练并保存模型，在同一 JSON 中并列输出默认
`backtest` 与严格 `strict_backtest`；严格样本不足时返回错误，避免将不完整数据说成复核结果。
公开 CSV 演示的所有比分均标为 `assumed`，其指标为 `mixed`，训练与评估假设占比均为 100%。
只加载本项目自行训练、来源可信的模型文件。

### 批量预测与冻结报告（T06）

使用项目训练出的可信模型文件。按来源和联赛分别训练，多个模型可重复指定 `--model-file`。
新结果统一为 **P2 实验概率**，BET 禁用；仅历史比分模型，不含伤停、正式首发或天气等输入。

```bash
zucai train --league 英超 --source jc --strict-availability --output models/epl.pkl
zucai predict --match-id jc:2042428 --model-file models/epl.pkl --out reports/my-batch
zucai predict --matches matches.csv --model-file models/epl.pkl --out reports/another-batch
zucai predict --date 2026-11-02 --model-file models/epl.pkl --out reports/by-kickoff-day
zucai report --batch reports/my-batch/forecast.json --out reports/another-export
python examples/t06_public_replay.py --out reports/public-replay
```

`matches.csv` 必须有 `match_id` 列，可附 `home,away,league,source,kickoff_utc` 核对身份；有冲突的场次
列明原因并排除，不根据相似队名换场。`--date` 是北京时间**开球自然日**，竞彩编号单独展示。
无输入选择器时默认当天；历史回放默认截点的北京时间开球日。

每批输出 `forecast.json`、`report.md`、`report.csv`、`manifest.json`，目录必须为新目录。
冻结 JSON 保存原精度比分矩阵、模型文件 SHA256、训练范围/数量/可知时间依据，以及当次完整 SP 快照。
Markdown/CSV 从同一 JSON 生成，后续改价或回填比分不会改变旧报告，重新导出前核对 SHA256。
数据库的 `batch_id/mode/as_of_utc` 将预测关联到冻结报告，旧行保持未知，不自动当作赛前实录。
真实生成时间在模型计算完成时记录；写报告后和提交前再次检查开球，跨过开球则取消本批并回滚。

模型与赔率概率分列。报价须为单个时刻完整的普通胜平负三项，早于开球且不晚于预测截点；
无报价时仍可做纯模型预测，市场栏留空。EV 只是点估计，突出显示不表示通过验证或具备可购买价值。
`--market-weight w` 默认0；显式开启另存市场相关实验模型和独立版本，不覆盖纯模型行，缺完整SP则报错。

历史回放必须同时给出 `--replay --as-of 带时区的ISO时间`，保留真实生成时间并明确标为事后重建。
当前比赛表保留最新赛程/状态，不能证明历史改期或取消在当时是否已知；回放不能重新称为未见验证。
公开样例使用2019/20英超真实比分，假设赛果开球3小时后可知，`mixed/assumed=100%`；不伪填官方SP。

当前 PR 中的实际验收报告保存在 `reports/2026-11-01-t06-acceptance/`，可直接在私有 GitHub 查看。
日常命令生成的新报告需提交私有仓库后才能长期云端保存；现有 feeds 自动归档只包含赛程/报价/赛果，
不自动同步预测文件。可视化窗口和预测定时任务属于后续交付。

### 赛前球队特征（T04）

先安装可选Parquet依赖；命令只读已有数据库，不建空库、不迁移输入库。`--match-id`可重复，省略时提取全库目标。

```bash
python -m pip install -e ".[features]"
zucai features build --db data/zucai.db --out data/features.parquet
zucai features build --db data/zucai.db --match-id jc:2042428 --as-of 2026-11-01T22:00:00+08:00 --strict-availability --out data/strict-features.parquet
```

生成43个球队字段：双方近5/10场进球、失球、积分的均值及90天半衰期加权均值，主队近5主场/客队近5客场，
距最近已知完赛的开球间隔，以及最近3次交锋的主胜/平/客胜比例和进球均值。每窗记录有效n、assumed数量及所用比赛。
近况/交锋限同来源同联赛，跨赛季沿用数据；休息间隔使用同来源所有赛事，可能缺失未采集的杯赛，不能当作完整真实休息期。

每场截点是`min(as_of, 开球)`；历史比赛开球、赛果可知时刻、报价均必须严格早于截点。
迟到采集的旧比分不会被放回当时的特征。无样本保留NaN，不用0补均值；不足5/10/3场按实际n计算。
默认可使用明确标记的assumed数据并披露比例；`--strict-availability`将其排除。NULL旧标记仅在有时间时兼容为captured。
当前库不保存被覆盖前的全部赛程/比分版本，因此历史提取是重建，不等于当时冻结的未见验证。

`market_p_home/market_p_draw/market_p_away/market_line`只取最新完整且同一时刻的普通胜平负had快照。
`market_line=0`对应had；没有完整报价时为NaN，不混入hhad。`MODEL_FEATURE_COLUMNS`显式白名单排除全部market_和meta_列。
**本次不训练或升级模型，现有Dixon–Coles预测不会自动读取这些特征。**

输出同名前缀的`.parquet`、`.metadata.json`、`.manifest.json`；任何文件已存在就拒绝覆盖。
metadata含原始精度的证据、提取完成时刻、数据截点、字段名单和逐列缺失率；manifest校验两个文件。
使用`pandas.read_parquet`读取；Parquet引擎选项依据[pandas官方文档](https://pandas.pydata.org/docs/reference/api/pandas.DataFrame.to_parquet.html)。
日常新导出尚未自动上传云端，本次验收产物随私有PR保存。
[真实数据验收与缺失率](reports/2026-11-02-t04-acceptance/README.md)：866场提取成功，但841场在各自截点无可用历史；
导出成功不表示历史训练数据充足，也不表示模型已验证。

### 14场胜负彩期次分析（T07）

与竞彩足球二串一分开：按期号读取14场对阵，复用T06已冻结的**纯球队模型**概率。
先抓取并导出比赛清单，再使用相应来源/联赛的可信模型；模型可以重复指定。

```bash
zucai fetch sfc --issue 26156 --matches-out inputs/26156.csv
zucai predict --matches inputs/26156.csv --model-file models/epl.pkl --out reports/26156-model
zucai report sfc --issue 26156 --batch reports/26156-model/forecast.json --max-lines 64 --out reports/26156-sfc
```

`fetch sfc` 不指定期号时取500页面当前期；`report sfc` 仅在库中恰有一期时可以省略期号。
`--db` 指定独立数据库；CSV和报告均拒绝覆盖已有文件/目录。普通 `report --batch ... --out ...` 保持原用法。

优先用已归档的比赛对照或500当前竞彩页的比赛/主客球队ID连接已有竞彩记录，再核对联赛与开球时间；
没有可靠ID对照时才按已知队名/别名、主客、联赛和时间精确匹配。仅接受已核实的“瑞典超/瑞超”名称对照。
其余以 `c500:fixtureID` 保存，报告列出 `unmatched/ambiguous`。查找期间不创建简称球队或添加别名，
不把jc模型直接套到不同来源/联赛。ID对照页只请求公开根路径，不请求其robots禁止的历史查询路径。
补齐并核实球队别名/赛程或提供对应历史数据训练模型后，重新抓期次、重新预测，保留旧报告。

报告始终有14行；缺少任一场可用概率或身份冲突时，整期覆盖与复式留空。输入齐全时用动态规划
求不超过上限的最优**完整复式**覆盖（每场选1/2/3项，注数为各场选项数之积；不研究过滤缩水票）。
覆盖概率使用跨场独立近似，仅指14/14赛果均在覆盖范围内，不是含13场奖的中奖概率或净盈利概率。
页面赔率、平均概率、混合模型不进入计算，计算不需要蒙特卡洛模拟。所有结果仍为P2，BET禁用。

来源截止是第三方页面转述，官方资格和出票窗口未核实；历史页缺截止时保留未知。
缺完整年份时，历史短日期用期号年份解释并披露 `issue_year_assumed`；涉及1月/12月或跨度超过30天则拒绝，要求补充完整日期依据。
`historical_replay` 继续标为事后重建。期次来源采集时间和原预测生成时间分别显示；
报告新生成不代表在当时已获得完整期次或在售资格。

导出 `sfc.json/report.md/report.csv/manifest.json`。JSON内含完整期次映射、引用预测批次、模型依据
与SHA256，便于离开本机后复核；后续改库不会覆盖旧版。[真实验收报告](reports/2026-11-01-t07-acceptance/README.md)
随PR进入私有GitHub。当前feeds自动归档**不包含**期次表和这些报告，尚未启用自动同步。

## 目录结构

```
src/zucai/          Python 包
  cli.py            命令行入口（子命令：fetch / features / train / predict / report）
  db.py             SQLite 连接与建表
  models.py         SQLAlchemy 表定义
  sources/          各数据源抓取器（sporttery、c500 ...）
  features/         特征工程
  predict/          模型：dixon_coles、gbdt ...
  report/           预测结果输出（markdown / csv）
tests/              pytest，抓取器测试只用 data/fixtures 里的离线样本
data/raw/           抓取落盘的原始响应（不入库，.gitignore）
data/fixtures/      提交到仓库的小样本，供测试使用
docs/               架构、数据源说明、任务单
```

## 协作方式

- 架构与任务单由 Claude（本地）维护在 `docs/` 下。
- 具体实现由 Codex 云端按 `docs/tasks/` 中的任务单逐个完成，每个任务一个 PR。
- Codex 的工作约定见 [AGENTS.md](AGENTS.md)。
