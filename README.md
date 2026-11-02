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
zucai fetch results --source c500 --from 2026-11-02 --to 2026-11-02  # 仅根页近期备用
zucai fetch sfc --issue 26156 --matches-out inputs/26156.csv
zucai fetch fd --div E0 --season 2526 --season 2627 --assume-known-after 3h
zucai fetch fd --country JPN --from-season 2021 --to-season 2026 --assume-known-after 3h
# 在运行采集的环境中预先配置 FOOTBALL_DATA_ORG_TOKEN
zucai fetch fdorg --competition PL,PD,SA --days 7  # 自动拆成不超过10天的连续窗口
zucai fetch fdorg --competition PL --season 2025 --season 2026 --assume-known-after 3h
# 在运行采集的环境中预先配置 API_FOOTBALL_KEY
zucai fetch apif --league 98 --season 2024 --assume-known-after 3h --reserve 10
zucai fetch apif --date 2026-11-01  # UTC 日期；免费档当前仅最近三天可用
zucai train --league 英超 --source jc,fd,fdorg --since 2026-09-01 --output models/epl-mixed.pkl
zucai train --model dixon-coles       # 训练并按周回测
zucai tune --league 英超 --source jc,fd --db work/t15/static.db --grid 90,180,270,365,540,730
zucai train --league 英超 --source jc,fd --db work/t15/static.db --seasons 2425,2526 --fold weekly
zucai train --league 英超 --source jc,fd --predictions-out work/backtest.jsonl  # 逐场另存JSONL
zucai train --strict-availability    # 排除 assumed 训练，并列报告默认和严格回测
zucai train --league 英超 --source jc,fd --replay-known-after 3h  # 开发阶段历史回放
zucai features build --out data/features.parquet  # 提取球队特征，市场对比列单独隔离
python -m zucai.cloud_feeds apis --archive ../cloud-data --days 3 --run-id manual  # 在全新隔离目录运行
pytest -q
```

数据库默认位于 `data/zucai.db`。可以设置 `ZUCAI_DB`，也可用
`zucai db init --path /path/to/football.db` 明确指定文件。
参数文件依次查找 `ZUCAI_PARAMS`、当前目录的 `data/model/params.json`、源码相对路径（兼容可编辑安装）；
例如 `ZUCAI_PARAMS=/path/to/params.json zucai train --league 英超 --source jc,fd`。
`--params-file` 显式指定时优先使用该文件。未找到文件而回落90天时会输出包含尝试路径的 WARNING；
训练及预测的参数来源记录包含 `params_path`，便于核对实际使用的配置。
同一场比赛可以保存不同采集时间的赔率和不同模型版本的预测。

500.com 赛果源定位为**近期备用**，每批只 GET 一次 `https://trade.500.com/jczq/`，不带查询串。
2026-11-02 实读 robots.txt：禁止 `/jczq/?` 和 `/zc6/index.php?expect=*`，未禁止竞彩根路径；
14场 `/sfc/?expect=` 未被禁止。`--from/--to` 仍按北京时间开球自然日筛选，范围内每一天必须出现在
根页比赛行中，否则整批报错并提示使用 jc；不再查询前一编号日的历史页面。根页覆盖随时间变化，
示例日期也可能已移出。覆盖仅代表当前页可见场次，不保证该自然日全部比赛仍在页内。
只有精确的已结算比分选项可入库，“其他”、未结算、半场比分保持未知；有场次但无精确赛果时明确记录零场。
本次实抓根页有14场、没有精确已结算赛果，详情见 [T12验收](reports/2026-11-02-t12-acceptance/README.md)。
云端自动归档当前仅调用 jc 官方赛果，无 c500 自动备用查询。

### 云端采集

[数据分支 `data/football-feeds`](https://github.com/qw5563680/FOOTBALL/tree/data/football-feeds)
保存四来源 jc / fd / fdorg / apif 的可恢复归档。`matches/YYYY-MM.csv` 按 **UTC 开球月份**分组，
同时展示北京时间；`state/odds/` 按报价采集月保留完整快照，`state/closing_odds.jsonl` 独立保存无时间戳收盘价。
`feed_manifest.json` v2 记录表字段、全局行数、各来源行数与规范化行 SHA256，以及每个归档文件的 SHA256；
仍能读取原有 v1。恢复后可按联赛使用 `zucai train --source jc,fd,fdorg,apif --league 英超`，
样本数量、可知时间和跨来源身份校验仍按训练命令原规则执行。

工作流 **Football cloud feeds** 合入 main 后，每日 UTC **02:40 / 09:40**（北京时间 **10:40 / 17:40**）运行：

| job | 运行器 | 采集范围 |
| --- | --- | --- |
| `apis` | `ubuntu-latest`，不依赖运行器变量 | fdorg 九联赛前后各 `days` 个 UTC 日，默认 3，共 7 天一次请求；apif 固定今天、昨天、前天，通常各一次请求；fd 仅 UTC 周一早班拉已核验联赛当季，假设延时 3h |
| `sporttery` | 仓库变量 `FOOTBALL_FEEDS_RUNNER` 指定的标签 | 沿用官方在售赛程与赛果 `sync`；未配置时整个 job 显示 skipped |

两 job 依次恢复最新数据，整个工作流共用 `write-football-feed-data` concurrency，不取消正在运行的任务。
写回前执行 `git pull --rebase`；冲突则失败，不强推。fd 主联赛赛季按七月换季推导（如 2627），
已配置 country 合并文件只导入当前自然年；`leagues.json` 可用 `current_season` 明确覆盖。
未核验联赛不采集。API 解析、去重规则不变；分页必须完整，额外分页同样消耗配额。

在仓库 **Settings → Secrets and variables → Actions → Secrets** 配置
`FOOTBALL_DATA_ORG_TOKEN`、`API_FOOTBALL_KEY`。工作流只从这两个 Secrets 注入 API 凭据，
不使用仓库 Variables 或命令行参数传 key。API-Football 配额保存到 `state/api_football_quota.json`，
每次恢复并继续扣减，默认保留 10 次；损坏时拒绝重新假定完整预算。
各源拒绝、疑似重复、未对齐 CSV 保存为 `state/audit/<source>_*.csv`，保留已有历史。
竞彩旧 `state/rejected_results.csv` / `state/unmatched_teams.csv` 继续兼容。
API 单源、单窗口或单日期失败时继续其他采集并标记 `partial`；摘要列入库、链接、拒绝、请求数（fd 列为成功下载文件数）、
剩余配额和脱敏失败原因。单行拒绝/疑似重复也标记 partial。
数据库、归档校验和写回冲突仍使对应 job 失败。安全检查通过的原始响应和审计状态作为 Actions artifact 保留 90 天，
可见性继承仓库权限；工作流不会将原始响应提交到 main。

每次 API 采集后，仅对本次完整且可核验的响应离线执行 T09/T10 主客方向身份图，
以恢复后的 jc 记录为证据。fdorg 复用 `fdorg_verify` 图与证据生成函数，apif 调用 `apif_verify.infer_aliases`；
不把日期窗口当作完整赛季；同次运行的完整响应按联赛合并、按比赛 ID 去重后统一核验。
解析拒绝会留下覆盖缺口；若相关 jc 比赛的 ±180 分钟邻域没有完整覆盖，本次该联赛不产生新候选。
跨原件身份冲突或候选反向名称冲突须人工核验，不覆盖已有候选文件。新增 `auto_verified` 候选及原件 SHA256、
jc 比赛 ID、UTC 时间差追加到 `state/alias_candidates/fdorg.json` 和 `apif.json`。
候选不参与后续自动导入；审查方定期核对证据后另开 PR 合入 main 的正式别名文件。

Actions → **Run workflow** 选择代码分支后使用以下参数（`days` 默认 3，`slot` 默认 auto）：

- `operation=apis`：只采 API；`slot=early` 仍须在 UTC 周一才采 fd，late 始终跳过 fd。
  计划事件用 cron 确定早晚班，手动 auto 以 UTC 09:00 为界。
- `operation=verify`：托管机恢复并校验所有归档文件、表行数与来源哈希，不写数据分支；摘要为 `verified`。
- `operation=sync`：`apis` job 跳过，`sporttery` 在配置的自托管机采集；无变量时跳过。
  原有 `football-ingest-verify-*` / `football-ingest-run-*` 标签入口仍兼容。

自托管变量由审查方在合并后设置为单个标签 **`football-local`**，`runs-on` 直接使用该字符串；
当前 Windows 运行器已注册在 `D:\actions-runner`，通过用户登录计划任务运行 `run.cmd`。注册和停用见
[docs/RUNNER.md](docs/RUNNER.md)。竞彩官网曾对 GitHub 托管机返回 HTTP 567，故保持此门控。
竞彩同步复查最近几天、遗漏日期、逾期未结场次及仍未解决的拒绝日期；仅更晚被接受的同源观察能解除待补抓。
API 采集不会推进竞彩回填水位。两条腿均不自动训练或预测。

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
`--db` 指定数据库，`--league` 选择一个赛事，`--source jc,fd` 可显式选择多个来源；不同联赛须分别训练。
`--since YYYY-MM-DD` 指定回测开始日，之前的赛果可用于训练。
`--as-of YYYY-MM-DD` 将所有数据截在当日 UTC 零时。可用 `--output` 另存每次的模型。
训练默认stdout只含汇总、折信息及各赛季指标，不包含逐场预测数组。市场样本缺失时指标为 null。
`--predictions-out work/backtest.jsonl` 将逐场记录另存为JSON Lines（拒绝覆盖已有文件），
stdout的相应 `predictions` 字段改为 `{"path": ..., "rows": n}`。每行 `evaluation` 标明
`backtest` 或 `strict_backtest`；严格模式两组记录写在同一文件，总行数为两组评分n之和。
`evaluate()` 的完整返回值保持不变。
`availability_basis` 为 `mixed` 时必须连同 `assumed_training_share` 和 `assumed_eval_share` 解读；
顶层与每周 fold 分别披露训练和评估样本中固定延时假设的比例。顶层训练比例按各 fold 的训练样本数
加权，评估比例按实际成功预测的比赛数计算。NULL 来源按 `captured` 处理，可知时间缺失仍排除。
`--strict-availability` 排除所有 `assumed` 行后训练并保存模型，在同一 JSON 中并列输出默认
`backtest` 与严格 `strict_backtest`；严格样本不足时返回错误，避免将不完整数据说成复核结果。
公开 CSV 演示的所有比分均标为 `assumed`，其指标为 `mixed`，训练与评估假设占比均为 100%。
`--replay-known-after 3h`（也支持如 `90m`，默认关闭）只在内存中将晚于开球加延时的
`captured/published` 赛果提前到该假设时刻，标为 `replayed`；不改变 `assumed`、未知可知时间或库中数据。
它与 `--strict-availability` 互斥。训练命令只读已有数据库，不建库或迁移；旧库须先显式运行 `db init`。
回测顶层与每折披露 `replayed_training_share`、`replayed_eval_share`，分母分别沿用上述训练加权及
成功评估样本口径。只要实际使用过重放的训练或评估行，`availability_basis` 就为 `replayed`，优先于 `mixed`；
原有 `assumed` 占比仍独立保留。**带 `replayed` 的指标只能叫“开发阶段历史回放”，不能当作前瞻验证。**
模型保存实际训练重放占比及延时；模型元数据的 `replayed_eval_share` 指本次训练命令关联的周滚动回测占比，
不是最终拟合模型的留出测试。单独调用 `fit` 或旧模型没有关联回测时该字段为 null。
报告的 `uniform` 为与模型同一批成功评分比赛的等概率参照（主/平/客各 1/3），顶层和每折均有
对数损失与 Brier：非空集分别为 `ln(3) ≈ 1.098612`、`2/3 ≈ 0.666667`，空集为 null。
这是固定比较参照，不是可达到误差的数学下限，也不代表市场概率。
只加载本项目自行训练、来源可信的模型文件。

### 评估、选参与 P2 门槛（T15）

15联赛的真实数据结果、校准对比和静态库SHA256见[T15验收报告](reports/2026-11-02-t15-model-selection/README.md)。

`train --seasons 2425,2526 --fold weekly` 按欧洲联赛 UTC 七月换季；瑞超/挪超/芬超/美职/巴甲
按自然年，例如 `--seasons 2024,2025`。日职按B1分段：2025及以前按自然年，
2026年1–7月归为 `2026T`，永不计入P2完整季；自2026年8月1日UTC起按8月换季，记为 `2627`。
最近完整季查找及新选参的验证季跳过过渡季：2025季冻结的参数用于 `2627` 验证，
选择截点仍为2026年1月1日，不使用过渡期赛果选参。已发布T15日职配置的旧验证窗口标签 `2026` 仅按 `2026T` 解读，
不改参数文件，也不将它套用于 `2627` 的历史评估；新命令须显式使用分段标签。
每折累积截点前所有跨季历史，开球和赛果可知时间均须严格早于截点。
weekly 在周一冻结；跨季的一周在赛季边界再切开，monthly 在月初冻结。`--since` 可进一步缩小评估范围，
部分赛季不能通过完整赛季门槛。默认不重放 jc 的晚到可知时间。

`zucai tune` 默认用最近三个完整赛季分别选参（当前欧洲为2324/2425/2526），每季六候选在 monthly
walk-forward 上按模型 LogLoss 选择；同分选更短半衰期，候选须覆盖同一完整可评分集。
下一赛季单独验证，最终用 weekly 折重新评估；因此完整2425和2526分别使用2324和2425选出的参数，
2627使用2526选出的参数。当前季只报部分季指标，不算完整季。选择过程中不读取价格。
结果写 `data/model/params.json`，stdout 为全量 JSON（建议重定向到 `work/`）。

`train` 默认读取联赛推荐半衰期，`--half-life-days` 覆盖并在模型和 JSON 标记 `parameter_source=cli`。
`--params-file` 可指定另一份配置；无有效配置或配置选择时刻晚于训练截点则回落90天。
历史评估使用配置中的上一季选择映射；未覆盖季为明确披露的90天回落，显式 CLI 参数为固定参数评估。
T06 读取配置核对门槛；已加载模型保持实际拟合参数，要改变半衰期须重新训练，不能仅改配置。
已冻结报告保留当时 P2 状态，不随配置变化重写。

未知球队默认使用训练联赛最弱四分位攻击参数均值、最弱四分位防守参数均值，各向全联赛均值收缩50%。
较高 defence 表示较弱防守；必要时仅把该未知对阵的 rho 限制在非负比分矩阵的合法区间。
元数据披露 `unknown_team_prior`、先验方法和 rho 是否裁剪。已知球队预测保持原模型路径。
评估另列未知球队、跳过先验前的已知球队集、counterpart_id 链接的 jc 子集（含恢复的既有API链接，小样本参考），
逐折披露 warmup/fit_failed/unknown_team/invalid_grid；空集指标为 null。
10桶校准同时报告 `draw_mean`、`draw_rate` 和预测减实际的 `draw_bias`。
`tune` 默认先按未校准的选择季S选半衰期；只有该候选在S的平局偏差绝对值超过3个百分点，
才为S+1开启校准。开关和选择偏差随各窗口冻结，不能根据S+1表现回选。
校准在每个训练折内用原时间衰减权重拟合一个平局log-odds截距（限制[-6,6]），
将比分矩阵对角线乘以该截距的指数并重新归一化；非平局间相对比例不变。
训练内无效概率行单独计数，不进入校准；评估无效矩阵仍明确跳过。
`tune --draw-calibration/--no-draw-calibration` 可强制全程开/关做诊断；
`train` 同名开关覆盖配置并记录来源。强制比较属于探索性检查，不据此回改滚动验证的开关。
T16禁止强制校准诊断改写默认正式参数：`tune --draw-calibration` 或 `--no-draw-calibration`
必须显式给出独立的 `--params-file work/diagnostic-params.json`；指向默认正式文件同样拒绝。

三列对照是模型、等概率、收盘基准；Brier 为三分类平方误差之和。收盘组只取完整有限且大于1的
B365、其次PS、其次Avg，按倒数归一化去水，缺失时为空；另列相同收盘覆盖样本上的模型指标。
**收盘价是赛前最后报价，由出版方赛后发布，仅作参照上限**，不能作为当时可执行赔率。
无市场信息进入模型、先验、参数选择或校准；原有带时间戳 had 基准仍单独保留。

`p2_ready` 只看最近两个完整赛季的 weekly 验证：两季均有有效评分且无跳过或不可知赛果，评分数量须与范围内场数一致，合计 n≥500，
模型 LogLoss 比等概率低≥0.05，且相同比赛上的模型 LogLoss 不比收盘基准高超过0.05。
收盘配对集也要求 n≥500，防止用少量行情认证整个联赛；缺失证据、部分季或 monthly 结果均不通过。
门槛按两季合并场均指标计算，完整规则和理由在 params.json 中。
T06 只有与已验收来源、半衰期、先验及校准配置相符、且门槛证据在预测时已存在的模型显示“P2 可用”，
T16按A1改为策略绑定：来源集合、累计训练策略、半衰期、先验方法及校准开关须一致，
模型训练截点不早于参数选择时刻，不能使用重放模型；跨季后证据必须覆盖最新两完整季。
训练库与评估库无需SHA256相等，模型分别记录 `training_database_sha256` 与
`evaluation_database_sha256`，T06模型表并列显示，已知哈希不同时标注“评估库 ≠ 训练库”。
哈希只用于追溯语料，策略不符或证据过期仍显示“P2 实验”。历史评估不等于新增数据的前瞻验证。
**BET 始终禁用，P2 达标不授予任何执行权限。** 本任务不自动训练；该项顺延T17。

### 多赛季历史数据（T08）

`fetch fd` 只导入已核实时区的 football-data.co.uk 常规时间赛果。下载前须具有站方允许的用途和访问许可；
本次自动访问已获用户确认许可，书面依据尚未独立核阅。原始 CSV 和数据库不上传仓库。
必须显式设置结果可知时间假设，如 `--assume-known-after 3h`；这不是真实发布时间。
下载原件附 URL、HTTP 状态、读取时刻与 SHA256。`--raw-file path.csv` 可离线回放同名 `.csv.meta.json`
完整且哈希、URL、采集时刻有效的原件；主联赛回放每次只选一个赛季。
新收盘价格存入 `closing_odds`，无报价时刻。T15 仅允许回测对照读取，模型、先验、校准和实盘报价不读取。

球队只用已批准或比赛证据核对过的别名。同场官方比赛保留 jc 身份和赛果，身份未齐的可疑重复单独隔离。
未知英文名称保留英文身份并记入 `data/unmatched_teams.csv`；以后批准别名时若同一联赛已有独立身份，需要先核对合并。
仅其他联赛留有同名英文身份时，当前联赛的已核验别名可正常使用，不擅自合并其他联赛的历史。
拒绝行与可疑重复写入 `fd_rejected_rows.csv`、`fd_suspected_duplicates.csv`，可用 `--audit-dir` 指定目录。
单行失败不会丢弃其他合格场次；文件级身份、网络或结构错误仍报错。
`--leagues-file`、`--aliases-file` 可显式指定经过审查的配置，默认使用本仓库 `data/` 中的版本。

新模型的 `sources` 表示选择的来源范围，`training_source_counts` 表示截点前实际参与拟合的场数。
T06 对范围内来源可预测，冻结格式升级为 schema 2；旧单来源模型与 schema 1 报告仍能读取。
T14 已接入周一早班采集和数据分支恢复；历史原始 CSV 仍只保存在采集环境或 Actions artifact。
[时区及数据限制](docs/DATA_SOURCES.md#4-football-datacouk历史赛果与收盘赔率t08) ·
[真实数据验收与缺口](reports/2026-11-02-t08-acceptance/README.md)。

### 整个联赛的赛程与赛果（T09）

Football data provided by the [Football-Data.org API](https://www.football-data.org/).
支持 `PL ELC PD SA BL1 FL1 DED PPL BSA` 九个联赛，仅采集赛程与 90 分钟（含补时）赛果。
`--days 7` 覆盖 UTC 今天前后各 7 天（共 15 天），自动拆成 10 天和 5 天两个连续窗口。
每个窗口单独请求、落盘和提交；后续窗口失败时保留此前文件与已导入行。预定比赛只建赛程，增量完赛按采集时刻可知。
`--season` 用赛季**起始年**，必须显式指定历史可知时间假设；当季整季也遵守此规则。
历史赛季可加 `--finished-only`。本账户九联赛最早确认可读的起始年为 2023，2022 返回 403；欧洲为 2023/24，巴甲为 2023 自然年赛季。
仅把密钥设在运行采集的环境中，不写入命令、仓库或快照。

默认读取 `data/team_aliases/football_data_org.json` 的 172 条带证据 auto_verified ID 别名；
`data/football_data_org/competitions.json` 记录九联赛实测起始年和核验 UTC。
新核验结果可在训练库副本上先复核，再用 `--aliases-file` 指向替代文件。
`proposed` 不生效。相同比赛保留竞彩官方记录，其次保留 T08 的 `fd:`，单独的本源记录使用 `fdorg:`。
同一赛季或单个分窗可通过 `--raw-file` 和快照自带的校验文件离线重放，不需要凭据；
多窗口逐份重放，仍使用原 `--days` 参数；赛季回放仍需一次指定一个赛季。分窗 sidecar 的 `window_anchor_date` 保留最初 UTC 起算日，跨午夜也不平移请求范围。
例如 `zucai fetch fdorg --competition PL,PD,SA --days 7 --raw-file /path/to/window.json`。
429 最多按服务端重置时间等待 15 分钟；超过时退出并保留归档，未等待就提前重试的情况不会发生。
未对齐球队按联赛和本源 ID 写入 `data/fdorg_unmatched_teams.csv`，字段为 `league,team_id,english_name,first_seen_utc`；
重复采集不重复追加，较早原件的回放会更新最早观察时间。与拒绝/疑似重复清单一样可用 `--audit-dir` 指定目录，均不进仓库。
[来源与数据边界](docs/DATA_SOURCES.md#5-football-dataorg-v4欧洲主流联赛全量赛程与赛果t09)。

### 亚洲与北欧联赛（T10）

数据来源：[API-Football / API-Sports](https://www.api-football.com/)。
支持日职98、韩职292、澳超188、瑞超113、挪超103、芬超244，以及丹超119、美职253、中超169。
本账户实测整季只开放起始年2022、2023、2024；当季整季被拒绝。`--date` 则允许最近三天，
本轮 UTC 2026-10-31 至2026-11-02均可读取。元数据的 current 赛季不等于免费权限。

历史回填必须显式假定可知延时；日期增量只记真实 captured。比分只取90分钟 `score.fulltime`。
`--season` 可重复，每个完整赛季独立提交；先归档所有页再整体核对身份，后续失败保留已完成赛季和安全原件。
默认每日保留10次额度，配额文件须持久保存并供本机采集共享；到界限明确报错，不自动跨日等待。
离线重放用 `--raw-file <原件.json>` 加同名 sidecar；参数仍须与原件一致，多页须用完整 assembled 归档。
`--aliases-file`、`--leagues-file`、`--quota-file`、`--audit-dir` 可指定替代路径。

别名在 `data/team_aliases/api_football.json`，proposed 逐条保留待审查，尚未核验的名称不自动合并。
官方比分优先，其次 fd/fdorg；拒绝、可疑重复和未对齐清单写入审计目录，默认不提交。
本轮韩职、澳超链接率及仅jc训练样本未满足完整验收，见[实际结果](reports/2026-11-02-t10-acceptance/README.md)。
T14 已接入最近三天的定时采集；配额与规范化数据在数据分支持久化，原始 JSON 保留为 Actions artifact；模型仍自行保存。
[接口实测与权限](docs/DATA_SOURCES.md#6-api-football-v3--api-sportsio亚洲与北欧联赛t10)。

### 批量预测与冻结报告（T06）

使用项目训练出的可信模型文件。按来源和联赛分别训练，多个模型可重复指定 `--model-file`。
新结果按T15门槛显示 **P2 实验** 或 **P2 可用**，BET 禁用；仅历史比分模型，不含伤停、正式首发或天气等输入。

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

`feature_version=t04-v2`保留原有43个球队模型字段及其顺序：双方近5/10场进球、失球、积分的均值及90天半衰期加权均值，
主队近5主场/客队近5客场、双方休息天数，以及最近3次交锋的主胜/平/客胜比例和进球均值。
仅`home_rest_days/away_rest_days`改为按赛程计算，其余41列的定义与计算保持不变。
近况/交锋限同来源同联赛，跨赛季沿用数据；每个赛果窗口记录有效n、assumed数量及所用比赛。

休息天数是“距最近已知开球的间隔”：从`matches`取同来源、同球队、跨所有赛事且开球严格早于**目标开球**的最近一场，
用目标开球减去该场开球并换算为天。不要求`Result`或`available_utc`，`scheduled/finished`均可，排除`cancelled`。
该赛程的开球可晚于`as_of`，只要早于目标开球。`meta_home_rest_match_id/meta_away_rest_match_id`记录所用ID，
替代旧的休息窗口n/assumed_n；休息审计单独保存赛程证据，不伪造比分或赛果可知依据。
`meta_history_n/meta_history_basis/meta_assumed_history_share`继续描述赛果历史，纯赛程休息证据不计入其样本或假设占比分母。
库内可能缺失未采集的杯赛，间隔不能当作完整真实休息期。
Python公共函数也在同一SQLite事务快照内读取并刷新ORM缓存；调用时使用短生命周期、正常autoflush的Session，
由调用方结束事务。已有事务继续使用其快照，不会被函数提交或回滚。

每场有效截点是`min(as_of, 开球)`；依赖赛果的历史比赛开球、赛果可知时刻，以及市场报价均必须严格早于截点。
迟到采集的旧比分不会被放回当时的特征。无样本保留NaN，不用0补均值；不足5/10/3场按实际n计算。
默认可使用明确标记的assumed数据并披露比例；`--strict-availability`将其排除。NULL旧标记仅在有时间时兼容为captured。
当前库只保留当前赛程/比分版本，不能证明某一赛程及其状态、改期在历史`as_of`时已知；历史提取是重建，不等于当时冻结的未见验证。

`market_p_home/market_p_draw/market_p_away`独立取严格早于有效截点的最新完整普通胜平负`had`快照。
删除旧`market_line`，新增可空整数`market_hhad_line`及`market_hhad_p_home/market_hhad_p_draw/market_hhad_p_away`。
`hhad`独立选择严格早于有效截点、同一时刻且同一整数让球线的最新完整三项快照；不跨时刻、跨让球线或与`had`拼接。
两组概率各自按逆价比例归一化去水，缺完整快照时对应组保持缺失。
`MARKET_COLUMNS`共7列，`MODEL_FEATURE_COLUMNS`显式白名单仍排除全部market_和meta_列。
**本次不训练或升级模型，现有Dixon–Coles预测不会自动读取这些特征。**

输出同名前缀的`.parquet`、`.metadata.json`、`.manifest.json`；任何文件已存在就拒绝覆盖。
metadata含原始精度的证据、提取完成时刻、数据截点、字段名单和逐列缺失率；manifest校验两个文件。
使用`pandas.read_parquet`读取；Parquet引擎选项依据[pandas官方文档](https://pandas.pydata.org/docs/reference/api/pandas.DataFrame.to_parquet.html)。
本次只读已有数据，不新增联网采集；日常新导出尚未自动上传云端。
按[AGENTS.md第8条](AGENTS.md)，新增`reports/`验收产物只提交README与SHA256清单；全量Parquet、metadata及逐场证据
归档时放`data/football-feeds`分支或Release附件，在README注明位置和哈希。
[旧T04验收与缺失率](reports/2026-11-02-t04-acceptance/README.md)为保持原样的`t04-v1`冻结证据：866场提取成功，
但841场在各自截点无可用赛果历史，不能用其旧休息天数和4列市场统计代表`t04-v2`。导出成功不表示历史训练数据充足或模型已验证。
[T13修正验收](reports/2026-11-02-t13-acceptance/README.md)披露归档重建库的报价缺口，并在同一重建库比较v1/v2及旧T04冻结统计。

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
