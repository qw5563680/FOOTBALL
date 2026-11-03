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
# 以下 API 采集命令由审查者在 Actions 执行，正式凭据只取 Secrets；开发测试用离线 fixtures
# Actions 配置 FOOTBALL_DATA_ORG_TOKEN
zucai fetch fdorg --competition PL,PD,SA --days 7  # 自动拆成不超过10天的连续窗口
zucai fetch fdorg --competition PL --season 2025 --season 2026 --assume-known-after 3h
# Actions 配置 API_FOOTBALL_KEY
zucai fetch apif --league 98 --season 2024 --assume-known-after 3h --reserve 10
zucai fetch apif --date 2026-11-01  # UTC 日期；日常采集固定最近三天
zucai train --league 英超 --source jc,fd,fdorg --since 2026-09-01 --output models/epl-mixed.pkl
zucai train --model dixon-coles       # 训练并按周回测
zucai tune --league 英超 --source jc,fd --db work/t15/static.db --grid 90,180,270,365,540,730
zucai train --league 英超 --source jc,fd --db work/t15/static.db --seasons 2425,2526 --fold weekly
zucai train --league 英超 --source jc,fd --predictions-out work/backtest.jsonl  # 逐场另存JSONL
zucai train --strict-availability    # 排除 assumed 训练，并列报告默认和严格回测
zucai train --league 英超 --source jc,fd --replay-known-after 3h  # 开发阶段历史回放
zucai features build --out data/features.parquet  # 提取球队特征，市场对比列单独隔离
python -m zucai.cloud_feeds apis --archive ../cloud-data --days 3 --run-id manual  # 在全新隔离目录运行
python -m zucai.cloud_predict run --archive ../cloud-data --params data/model/params.json --run-id manual-1
python -m zucai.cloud_feeds backfill --archive ../cloud-data --run-id history-1  # 全新隔离目录，一次性回填
python -m zucai.cloud_feeds backfill --archive ../cloud-data --run-id cups-cl-1 --source fdorg --leagues CL --from 2023 --to 2026
python -m zucai.cloud_feeds backfill --archive ../cloud-data --run-id cups-el-ecl-1 --source apif --leagues 3,848 --from 2025 --to 2026
python -m zucai.cloud_feeds backfill --relink-fdorg --archive ../cloud-data --run-id fd-links-1  # 仅审查者在 Actions 手动补链，不联网
python -m zucai.cloud_feeds reconcile --archive ../cloud-data --run-id identity-merge-1  # 审查者在 Actions 合并已核验的 APIF 存量身份，不联网
zucai aliases proposals --provider apif --leagues 3,848,292,119,188,244,169 --archive ../cloud-data --out docs/aliases/2026-11-03-apif-proposed.md
zucai aliases evaluate --provider apif --leagues 3,848 --archive ../cloud-data --out work/identity-evaluation.json
python -m zucai.scoreboard run --archive ../cloud-data  # 只读已有预测和赛果，更新记分板与虚拟账户
python -m zucai.daily_digest --archive ../cloud-data --out ../cloud-data/daily  # 只读冻结数字，生成每日摘要
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

快速开始：由审查者在 Actions 手动运行 `operation=probe source=apif leagues=3,848 seasons=2021-2026`，查看权限审计表。

[数据分支 `data/football-feeds`](https://github.com/qw5563680/FOOTBALL/tree/data/football-feeds)
保存四来源 jc / fd / fdorg / apif 的可恢复归档。`matches/YYYY-MM.csv` 按 **UTC 开球月份**分组，
同时展示北京时间；`state/odds/` 按报价采集月保留完整快照，`state/closing_odds.jsonl` 独立保存无时间戳收盘价。
`feed_manifest.json` v2 记录表字段、全局行数、各来源行数与规范化行 SHA256，以及每个归档文件的 SHA256；
仍能读取原有 v1。恢复后可按联赛使用 `zucai train --source jc,fd,fdorg,apif --league 英超`，
样本数量、可知时间和跨来源身份校验仍按训练命令原规则执行。

工作流 **Football cloud feeds** 合入 main 后，每日 UTC **02:40 / 09:40**（北京时间 **10:40 / 17:40**）运行：

| job | 运行器 | 时间 / 范围 |
| --- | --- | --- |
| `apis` | `ubuntu-latest`，不依赖运行器变量 | fdorg 九个国内联赛及欧冠共用前后各 `days` 个 UTC 日窗口，默认 3，共 7 天一次请求；apif 固定今天、昨天、前天，过滤配置含欧罗巴、欧协联，通常各一次请求；fd 仅 UTC 周一早班拉已核验联赛当季，假设延时 3h |
| `sporttery` | 仓库变量 `FOOTBALL_FEEDS_RUNNER` 指定的标签 | 沿用官方在售赛程与赛果 `sync`；未配置时整个 job 显示 skipped |
| `predict` | `ubuntu-latest`，45 分钟上限 | 每次定时采集后（含迟到班次）或手动 `operation=predict`；15 联赛训练、北京今天剩余与明天的竞彩开球场次，两批冻结报告 |
| `scoreboard` | `ubuntu-latest`，30 分钟上限 | 早晚班均更新赛后评分和虚拟账户；等待 `predict` 结束，预测失败仍可结算旧批次；另支持手动 `operation=scoreboard` |
| `digest` | `ubuntu-latest`，15 分钟上限 | 等待 `scoreboard`，把冻结数字写成每日摘要并推送到当月 Issue；另支持手动 `operation=digest` |

五个 job 依赖顺序为 `apis → sporttery → predict → scoreboard → digest`，各自恢复最新数据，整个工作流共用 `write-football-feed-data` concurrency，不取消正在运行的任务。
写回前执行 `git pull --rebase`；冲突则失败，不强推。fd 主联赛赛季按七月换季推导（如 2627），
已配置 country 合并文件只导入当前自然年；`leagues.json` 可用 `current_season` 明确覆盖。
未核验联赛不采集。API 解析、去重规则不变；分页必须完整，额外分页同样消耗配额。

在仓库 **Settings → Secrets and variables → Actions → Secrets** 配置
`FOOTBALL_DATA_ORG_TOKEN`、`API_FOOTBALL_KEY`。工作流只从这两个 Secrets 注入 API 凭据，
不使用仓库 Variables 或命令行参数传 key。API-Football 配额保存到 `state/api_football_quota.json`，
每次恢复并继续扣减；每日上限跟随 `x-ratelimit-requests-limit`，也可由 `/status` 提供，
两者尚未提供时回退 100，摘要明确标注「回退」。默认保留 10 次，每次请求间隔仍为 6.1 秒；
本地日上限默认等于套餐上限，可用仓库 Actions Variable `ZUCAI_APIF_DAILY_CAP` 下调（空值表示默认）。
该值取与套餐上限的较小值，不能扩大服务器额度；损坏配额文件拒绝重新假定完整预算。
正式 key 只供 Actions 使用，任何开发环境均不得拿正式 key 联网。
各源拒绝、疑似重复、未对齐 CSV 保存为 `state/audit/<source>_*.csv`，保留已有历史。
竞彩旧 `state/rejected_results.csv` / `state/unmatched_teams.csv` 继续兼容。
API 单源、单窗口或单日期失败时继续其他采集并标记 `partial`；摘要列入库、链接、拒绝、请求数（fd 列为成功下载文件数）、
剩余配额、`plan`、`limit_day` 和失败原因。`errors.plan` / `errors.access` 在通过安全归档检查后保留原文，仍标记 partial；其他上游错误不直接输出正文。单行拒绝/疑似重复也标记 partial。
数据库、归档校验和写回冲突仍使对应 job 失败。安全检查通过的原始响应和审计状态作为 Actions artifact 保留 90 天，
可见性继承仓库权限；工作流不会将原始响应提交到 main。

每次 API 采集后，仅对本次完整且可核验的响应离线执行 T09/T10 主客方向身份图，
以恢复后的 jc 记录为证据。fdorg 复用 `fdorg_verify` 图与证据生成函数，apif 调用 `apif_verify.infer_aliases`；
不把日期窗口当作完整赛季；同次运行的完整响应按联赛合并、按比赛 ID 去重后统一核验。
解析拒绝会留下覆盖缺口；若相关 jc 比赛的 ±180 分钟邻域没有完整覆盖，本次该联赛不产生新候选。
跨原件身份冲突或候选反向名称冲突须人工核验，不覆盖已有候选文件。新增 `auto_verified` 候选及原件 SHA256、
jc 比赛 ID、UTC 时间差追加到 `state/alias_candidates/fdorg.json` 和 `apif.json`。
候选不参与后续自动导入；审查方定期核对证据后另开 PR 合入 main 的正式别名文件。

T22 权限探测由 Claude 在 **Actions → Football cloud feeds → Run workflow** 选择 PR 分支执行：
`operation=probe source=apif leagues=3,848 seasons=2021-2026`。
九个既有联赛另跑 `leagues=98,292,188,113,103,244,119,253,169 seasons=2025,2026`。
每个联赛 × 赛季只取第一页，不入库；HTTP、错误、结果数、分页和原件 SHA256 写入
`state/audit/apif_season_probes.csv`，原件及 sidecar 留在本次 artifact。
任务结束沿用统一写锁、数据分支导出及恢复校验，不推进竞彩赛果同步日期。

probe 先连续查询两次 `/status`，将套餐、日计数和每日响应头的变化记录到
`state/audit/apif_status_probes.csv`。相同 UTC 日、套餐及上限一致，且两次正文/响应头
一致显示计数不变时才记为不计费；计数增加一次记为计费，其他情况保留「未知」。
仅已记录不计费时，后续 `apis` 和 apif `backfill` 开头自动刷新一次 `/status`，另列请求数，三次日期采集不变。
本地硬上限仍保守计入所有实际请求，包括 `/status`。Claude 已在 Actions 验证 Pro / 7500 次，
连续两次 status 计数保持 9→9（后一次 probe 为 21→21），记录为不计费；详情见 [DATA_SOURCES §6](docs/DATA_SOURCES.md)。
`apif_status_probes.csv` 同时记录成功和失败尝试；失败列出 HTTP、安全原件 SHA256 或失败原因。
旧八列审计表自动补列，原有记录和哈希保留。

T23 的七个 **identity-only** 联赛只用于核验球队 ID，不进入每日采集或任何比赛导入：
英超 39、西甲 140、意甲 135、德甲 78、法甲 61、荷甲 88、葡超 94，已由 Claude 的
[probe 41612517821](https://github.com/qw5563680/FOOTBALL/actions/runs/41612517821) 确认。
[identity 41612781709](https://github.com/qw5563680/FOOTBALL/actions/runs/41612781709) 核验出 130 条别名，
已保留完整证据纳入正式文件；剩余 20 支待审批，真实回填验收仍待 Claude。
核验流程为先运行 `operation=probe probe_kind=leagues`（leagues 留空选择这七个），
每个 ID 一次 `/leagues?id=` 请求，核对名称、国家和联赛类型；这不是赛季权限证明。
有成功审计后再运行 `operation=identity`，默认七联赛、`seasons=2025,2026`，最多 14 次 fixtures 请求。
identity 不额外查询 status，不补拉分页；不完整响应不作为核验依据。它只用已有 jc 和 T08 已核验的 fd
比赛图生成候选证据，写入 `state/alias_candidates/apif.json` 和 `state/audit/apif_identity.csv`，
不改变比赛、球队、赛果、冻结预测或竞彩水位。请求间隔 6.1 秒、reserve=10 保持原样。

候选里的 `auto_verified` 须由实现者保留完整证据复制进正式别名文件，再经 Claude 审查；
之后欧战复用已有 `same_provider_id` 桥接。未确认的球队使用快速开始里的 `aliases proposals` 生成
只读审批清单，jc 参考名不绑定，建议规范名留给所有者填写。具体批准格式及顺序见
[别名审批说明](docs/aliases/README.md)。`aliases evaluate` 只输出离线评估，不放宽生产规则。

T20 的三项欧战在导入前另做运行期核验：复用同源已生效的国内联赛数字球队 ID，
再用完整响应与 jc 的双向唯一主客方向图补充身份；不读取待审候选作为生效别名。
历史回填（包括直接 fetch 重复指定 `--season`）先下载所选全部赛季，再统一核验，
避免先入库的未知身份挡住较新赛季的证据。后续下载失败仍保留此前完整范围的导入结果。
依据写入 `state/audit/{fdorg,apif}_cup_aliases.csv`，已入库的来源 ID 绑定随数据库归档保存。
无法核验的保留 `fdorg:team:<id>` / `apif:team:<id>`，写入各源未对齐 CSV，不猜中文名。
导入器仍拒绝直接重绑已有独立身份。T23 另提供审查者手动触发的 `operation=reconcile`：
仅按已生效的正式别名与欧战同源 ID 桥接，把 `apif:team:<id>` 在对应联赛的比赛重指到唯一规范球队。
规范名缺失/不唯一、三小时内已有比赛、其他来源 ID 绑定、临时身份带 aliases 等情况跳过并记审计。
比赛编号、赛果、赔率、预测不改，临时行没有任何比赛引用后才删除；不发请求、不注入 API key。
Actions 摘要显示合并球队、重定向比赛、删除空临时行和冲突数。先 reconcile，再 backfill；
第二次 reconcile 合并 0。它不接受 source/leagues/seasons，检查全库 APIF 临时身份；未批准的 proposed 不处理。

Actions → **Run workflow** 选择代码分支后使用以下参数（`days` 默认 3，`slot` 默认 auto）：
`source` 选项统一为 fd / fdorg / apif，用于 backfill；probe 固定 apif，避免页面默认 fd 造成误用。
`leagues` 输入默认留空，由步骤脚本决定：probe 用 `3,848`，backfill 用该来源默认范围。
`seasons` 供赛季 probe（留空用 `2021-2026`）及 identity（留空用 `2025,2026`）；
`probe_kind=leagues` 只查元数据，不接收年份；`from_season` / `to_season` 仅供 backfill，含首尾。

- `operation=apis`：只采 API；`slot=early` 仍须在 UTC 周一才采 fd，late 始终跳过 fd。
  计划事件一律传 auto，按实际启动时刻以 UTC 09:00 为界判定早晚班，并输出 apis.slot；手动 auto 同样处理。
  每次定时运行均预测，手动 apis 仍不触发预测；fd 仍只在 UTC 周一 early 采集。
- `operation=relink-fdorg`：仅手动 Actions 补存量 fd/fdorg 链接，对应 `backfill --relink-fdorg`；
  不采集、不注入 key、不训练、不运行记分板。摘要列补链/跳过数、原因和逐对清单；只改 counterpart，保留赛果和预测原件。
- `operation=reconcile`：仅手动运行 APIF 存量身份合并，不触发采集、预测或记分板；
  复用写回锁，保持竞彩水位，归档恢复及冻结预测哈希校验后发布。
- `operation=backfill`：**一次性历史回填，不定时**。只在 `apis` 托管机运行（60 分钟上限），
  `sporttery` 与 `predict` 跳过；沿用写回锁和发布脚本。
  `source=fd`（默认）保持国内历史回填；`source=fdorg` 默认欧冠 CL 2023 至当季；
  `source=apif` 默认欧罗巴 3、欧协联 848 各 2021–2026 六季；显式 `leagues` 也可选择原有九联赛。
  `leagues` 可空，或填该来源的逗号分隔代码；API 回填凭据仍只取 Actions Secrets。
  例如 `source=apif leagues=3,848 from_season=2025 to_season=2026`；2021 可单独再跑起止均为 2021。
  九联赛用 `leagues=98,292,188,113,103,244,119,253,169 from_season=2025 to_season=2026`。
  允许年份始终取 `leagues.json` 的实测 `seasons_available`，不按套餐名推测权限。
- `operation=verify`：托管机恢复并校验归档文件、表行数、来源哈希与独立的 `forecast_manifest.json`；不写数据分支，摘要为 `verified`，没有预测的旧归档仍兼容。
- `operation=identity`：只在手动 Actions 核验七个桥接联赛，默认 2025/2026；必须先通过元数据 probe，
  只发布候选和审计，不触发竞彩同步、训练预测或记分板。source 固定 apif，leagues 留空选择七联赛。
- `operation=predict`：跳过采集，在托管机使用现有归档训练并预测；`days` / `slot` 不影响预测。
  `sporttery` 跳过、失败或归档水位不是今日时，摘要注明「竞彩数据非今日」。
- `operation=scoreboard`：只用现有冻结预测和归档赛果计算记分板，不采集、不训练；`days` / `slot` 不影响此操作。
  早晚定时运行及手动 predict 后也会运行记分板；预测失败仍可结算旧批次，整个工作流取消时不运行。
- `operation=digest`：只读取现有冻结预测和记分板，重新生成并推送北京当日摘要；采集、训练和记分板计算均跳过。
  `days` / `slot` 不影响此操作；早晚定时以及手动 predict / scoreboard 后自动推送。
- `operation=sync`：`apis` job 跳过，`sporttery` 在配置的自托管机采集；无变量时跳过。
  原有 `football-ingest-verify-*` / `football-ingest-run-*` 标签入口仍兼容。

T18 默认回填 15 个已核验联赛：E0/E1/SP1/I1/D1/F1/F2/N1/P1 的 2122–2627 六季，
JPN/SWE/NOR/FIN/USA/BRA 的 2021–2026 六个起始自然年；共 60 份 CSV（不含重定向跳数）。
合并文件每联赛只下载一次，来源中的 `2026/2027` 标签保留，按起始年 2026 筛选。
本地可筛选范围，例如在全新的隔离目录运行：

```bash
python -m zucai.cloud_feeds backfill --archive ../cloud-data --run-id history-subset --leagues E0,JPN --from 2425 --to 2526
```

fd 的 `--from/--to` 含首尾，接受赛季代码 `2425` 或起始自然年 `2024`；上述两联赛示例分别对应
E0 的 2425/2526 与 JPN 的 2024–2025。省略时默认 2021–2026；这些筛选参数仅用于 backfill。
回填先恢复现有归档，沿用 jc > fd、既有别名/疑似重复规则和开球后 3h 的 `assumed` 假设；
重复相同文件不新增比赛或赛果，比分更正仍按原导入器处理，不改官方比分和可知时间。
单文件 404、网络或解析失败记为 partial 后继续其他文件；有拒绝/疑似重复也明确标记 partial。
完成后恢复一次新归档并校验（包含已有预测清单），不推进竞彩补抓水位、不重新训练。

运行摘要列每联赛回填前/新增/回填后 fd 赛果数、失败项、拒绝/疑似重复、CSV 请求次数与真实最短间隔。
每次运行重新检查 robots，读取失败或规则禁止时停止；原有限速器保留，回填发送入口另留 2.1 秒间隔余量。
逐跳请求记录追加到数据分支 `state/audit/fd_backfill_requests.csv`；CSV/robots 原件及 SHA256 校验文件、
完整运行记录、审计作为 `apis-source-<run-id>-<attempt>` artifact 保留 90 天，不提交到 main。
回填成功后手动运行 `operation=predict` 使用新增历史；每周一当季增量逻辑保持原样。

欧战回填使用快速开始里的 `--source fdorg|apif` 命令，API 的 `--from/--to` 只接受四位起始年。
fdorg 每季一次请求（CL 2023–2026 共 4 次），apif 两项赛事六季共 12 次整季请求，另计已验证不计费的 status；
若有分页继续沿用原分页和 `reserve=10` 配额守卫。源的 6 秒 / 6.1 秒限速保持原样。
2026-11-03 付费后实测两项欧战 2021–2026、九联赛 2025/2026 可读；历史免费档拒绝已记录在 DATA_SOURCES，失败仍不当作空赛季成功。
当季日增量仍可由现有日期请求采集，不增加每日请求次数；摘要单列三项欧战库内比赛/赛果与本次新增行数。
回填摘要另列每赛事每季解析、入库、链接竞彩、拒绝、疑似重复、不同球队 ID 对齐率（apif 显示回填前→后）及未对齐数，
来源行数包含欧战并经过恢复校验；不覆盖竞彩比分，历史赛果沿用开球后 3h 的 assumed。
T23 的 apif 摘要另列本次新建立的 jc 关联、2026 季本次仍隔离场数，以及 jc 三小时窗口内候选球队的对齐率；
这个候选分母包含尚未确认身份的同时开球球队，不能当作已确认的在售球队名单。
APIF 拒绝/疑似重复审计按比赛 ID、原件 SHA256、原因去重，首次运行也整理旧重复行；不同原件和原因都保留。
欧冠 2026 当前只公布 144 场，后续淘汰赛尚未全部排出；apif 历史包含资格赛，
详见 [数据来源 §5、§6](docs/DATA_SOURCES.md)。本任务只接数据，欧战模型另由 T21 处理。

自托管变量由审查方在合并后设置为单个标签 **`football-local`**，`runs-on` 直接使用该字符串；
当前 Windows 运行器已注册在 `D:\actions-runner`，通过用户登录计划任务运行 `run.cmd`。注册和停用见
[docs/RUNNER.md](docs/RUNNER.md)。竞彩官网曾对 GitHub 托管机返回 HTTP 567，故保持此门控。
竞彩同步复查最近几天、遗漏日期、逾期未结场次及仍未解决的拒绝日期；仅更晚被接受的同源观察能解除待补抓。
API 采集不会推进竞彩回填水位。早班只采集；晚班 `predict` 等待两条采集腿结束，`apis` 失败或工作流取消时不预测。

T17 用 `params.json` 的各联赛来源（当前均为 `jc,fd`）、半衰期和校准开关训练；
恢复库只读，截点前 56 天的回测仅作冒烟检查，不更新 P2 证据。训练依赖现有 `[model]` extra，
工作流通过绝对路径 `ZUCAI_PARAMS` 定位代码分支内参数，不复制或改写配置。
本地也可用 `ZUCAI_PARAMS=/path/to/params.json python -m zucai.cloud_predict run --archive /path/to/cloud-data --run-id manual-2`；
`--params` 显式参数优先，运行 ID 必须唯一。单联赛失败记为 `partial`，其他联赛继续；
预测仅使用本次成功模型，缺模型、已开球场次按 T06 规则列入排除。空比赛日仍生成四件套。

数据分支新增以下路径，main 不保存模型或预测产物：

| 路径 | 内容 |
| --- | --- |
| `models/<run-id>/` | 各联赛 `.pkl` 与 `.train.json`；失败联赛仅有原因 JSON |
| `models/latest.json` | 各联赛最新成功模型路径、SHA256、训练截点、训练库哈希及 P2 标签；失败不替换上次成功项 |
| `forecasts/<北京开球日>/<run-id>/` | `forecast.json`、`report.md`、`report.csv`、`manifest.json`；同日重跑另存目录 |
| `forecast_manifest.json` | 每次运行、两日批次、模型/P2/训练库证据及所有模型、训练 JSON、报告和最新索引的 SHA256 |

Actions 摘要第一行为「非购买建议 · BET 禁用 · P2 标签含义见 README」，随后列各联赛训练结果和
两日实际 T06 比赛表（模型、市场去水概率与 EV 分列），紧跟原报告的「比分前五与快照」；空批次省略比分节。
摘要只复制已有报告内容，不重算或改写冻结报告。报告、模型及索引另以 artifact 保存 90 天；
数据分支长期保留每次冻结目录。模型校验不执行 pickle；预测只载入本次训练的可信模型。
归档写回为新路径设置 Git `-text`，避免 Windows 换行转换破坏冻结哈希。

### 前瞻记分板与模拟账户（T19）

**非购买建议 · BET 禁用 · 虚拟资金模拟 · 竞彩含抽水**

记分板读取数据分支中经过哈希校验的 `forecast.json` 和恢复出的赛果库，不抓新数据、不重新预测。
同场只给开球前最后一批 `prematch` / `predicted` 预测打分；生成时刻严格早于开球，赛果可知时刻
严格晚于生成且不晚于记分板截点，身份和常规时间比分必须完整。事后生成的 `historical_replay`、
假设可知时间 `assumed`、缺时间或身份变动的结果不算真实前瞻证据。
三列指标分别为纯模型、等概率与预测时 SP 去水后的市场参照；不读取后来的赔率或收盘价。
缺 SP 仍计模型评分，市场 n 单列；相同市场覆盖样本上的模型指标也写入 JSON。

每档门槛各有下列五个账户，分别从 **10000 虚拟币**开始，共 15 份独立统计。
计算 `ratio = 模型概率 × 冻结 SP`，要求 **ratio 严格大于** `1.00 / 1.05 / 1.10`；
等价于 `edge = ratio − 1` 严格大于 `0.00 / 0.05 / 0.10`。默认 1.05 档指预期收益超过 5%。
例如模型概率 48%、冻结 SP 2.5 时，ratio = 1.20，入选 1.05 档虚拟账户；ratio 恰好 1.05 时不入选。
无候选时如实记零注。

| 账户 | 固定模拟规则 |
| --- | --- |
| A 单场固定额 | 每场取 ratio 最大的一个选项，ratio 严格超过门槛才生成 100 虚拟币注单 |
| B 单场四分之一凯利 | 相同选项；注额为可用资金 × `(p×SP−1)/(SP−1)` × 1/4，上限 2%，下限 10 |
| C 热门对照组 | 每场选冻结 SP 最低的选项，固定 100；不看模型、不检查 ratio 门槛，各档仍独立记账 |
| D 二串一 | 同一北京开球日候选按 edge 排序取两场，固定 100；全部命中才按冻结 SP 乘积计算回报 |
| E 三串一 | 同上取三场；候选不足记录原因 |

同分按主/平/客顺序、再按比赛 ID 稳定排序。按批次生成时间处理；A/B/C 每个比赛每档每账户最多一注，
D/E 每个北京开球日每档每账户最多一注。后续批次不替换已有注单，因此纸面账户与「最后一批预测评分」口径不同。
注单保存原批次、选项、SP、注额、规则及冻结指纹；结算只填同一条的结算字段。
注额按分向下取整，未结本金占用可用资金，凯利只用该决定时刻已知的资金；上限已小于 10 或固定额超过余额时跳过。
回报率分母只计已结投入，当前资金为起始资金加已结盈亏，可用资金再扣未结本金；不借款，不把待结当作亏损。
取消、缺赛果或缺可信观察时间的注单保持待结，不猜测退款规则。已存评分或已结注单遭遇比分更正会提示复核，
原记录不静默覆盖；迟到的旧批次不倒填已建立的虚拟资金历史。

**单关偏差**：模拟假设均可单关，竞彩部分场次实际不开单关；也未模拟奖金封顶等其他现实规则。
竞彩赔率含抽水，历史预测准确不代表有盈利证据。首次补记已开球的历史批次时，注单明确标记 `replayed=true`，
只使用真实冻结的概率和 SP；这不等于当时已经生成了注单，不会把事后重建预测混入前瞻评分。

| 文件（均在数据分支） | 内容 |
| --- | --- |
| `scoreboard/scored.jsonl` | 逐场评分追加日志；同一比赛/批次不重复，汇总按比赛取最后一批，旧记录保留 |
| `scoreboard/ledger.jsonl` | 三档五账户的冻结注单、跳过原因、批次处理记录及结算；不改原注额和 SP |
| `scoreboard/summary.json` | 联赛 × 模型版本、按月及总体指标，10 桶校准/平局偏差、账户统计、门槛和文件 SHA256 |
| `scoreboard/scoreboard.md` | 每日覆盖的文本记分板，默认 1.05、另两档附表、每周资金点；历史由 Git 保留 |
| `daily/YYYY-MM-DD.md` | 北京当日摘要；同日第二班更新，历史版本由 Git 保留 |
| `daily/latest.md` | 与最近一次当日摘要完全相同的正文，便于手机打开 |

每天直接看[最新摘要](https://github.com/qw5563680/FOOTBALL/blob/data/football-feeds/daily/latest.md)，
或者订阅置顶的「每日报告 YYYY-MM」Issue。工作流先发布数据分支，再把同一正文发为 Issue 评论；
同一天晚班编辑已有评论，每月首次运行新建并置顶当月 Issue、取消上月置顶并关闭上月 Issue。
如果 1 日漏跑，下一次运行补做换月。只新增 `GITHUB_TOKEN` 的 `issues: write` 权限，不新增 Secret。
GitHub App / 邮件是否提醒取决于账号的订阅与通知设置；编辑评论不保证再次提醒，真实通知由 Actions 验收。

摘要依次列日期与数据水位、今明预测及冻结比分前五、昨日结算、1.05 档五个虚拟账户、采集异常。
首行固定「非购买建议 · BET 禁用」。无模型与缺失状态原样标出，不以零填空；门槛未达显示「证据不足」。
昨日评分按赛果可知时间的北京日归类，昨日注单按生成或结算北京日归类。现有记分板没有保存每日平均
LogLoss / Brier，所以摘要标「现有归档未提供」，只照录逐场与累计值，不另算平均、不改记分板。
竞彩同步可能覆盖四来源状态记录；未记录的来源明确显示未知，配额可读取已有配额快照并标日期，均非实时查询。
命令的 `--out` 是输出目录；写入归档内部时只允许 `daily/`。本地生成不推送，推送命令仅允许在 Actions 中执行。

记分板 job 将前 60 行贴入 Actions 摘要，四文件另存 artifact 90 天；使用原有写回锁与发布脚本。
仅更新这四个文件，feed/forecast 清单、数据库、模型和旧预测不变；先校验旧状态，失败回滚本次写入。
可在归档副本中运行 `python -m zucai.scoreboard run --archive ../cloud-data --replay-days 7`
补记最近七天已经冻结的批次；`--as-of` 可指定带时区的历史截点，但不能倒退已有记分板。
没有符合条件的完赛场次时保留 n=0 和未知指标，不制造旧预测或旧赛果。

两条门槛彼此独立：

- **前瞻已验证**：单个联赛 n ≥ 200 且模型 LL ≤ 等概率 LL。JSON 的联赛字段单独判定，
  顶层 `gates.prospective_verified` 仅表示至少有一个联赛通过。新 T06 报告在 P2 标签旁标明
  「仅历史证据」或「前瞻已验证」，证据在生成时冻结；云端使用预测之前已有的记分板，未提供证据的手工批次保守标为仅历史证据。
  原 P2 状态和门槛不变，旧报告不补写新标签。
- **模拟盈利证据**：仅账户 A / 1.05，已结注数 ≥ 500、回报率 > 0、删去总回报最高的 ceil(5%) 注后仍 > 0，
  且固定种子 20261103、bootstrap 1000 次的回报率 95% 区间下限 > −2%。达成只标
  「达到模拟门槛，待所有者决策」，**BET 仍禁用，不改任何默认值**。小样本和空集不宣称已验证。

最大回撤按逐注结算后的资金峰值计算，最长连亏按结算顺序计算；周表按北京时间周一分组。
连续四个完整且均有样本的北京周，平局偏差绝对值均 > 5 个百分点，或某联赛最近 100 场 LL 高于等概率时，
记分板标黄提示复核和重跑 tune；不会自动调参。逐注 bootstrap 未处理串关之间的相关性，仅作模拟稳健性描述。

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
必须显式给出独立的 `--params-file work/diagnostic-params.json`；指向任一默认候选路径同样拒绝，
包括 `ZUCAI_PARAMS` 指定但尚不存在的文件。

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
评估库哈希优先取模型训练时保存的配置快照；模型未保存该值时回落到当前配置。
后续更新配置不会改写模型已保存的哈希，P2判定仍使用预测时配置中的策略与证据。
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
免费档历史实测只开放 2022、2023、2024；2026-11-03 Claude 的付费档 probe 已确认九联赛 2025、2026，
可用年份现为 2022–2026。日职元数据 current=2027 尚未在付费档验证，仍标未确认。
`--date` 继续采最近三天；元数据 current 不代表实际读取权限。

历史回填必须显式假定可知延时；日期增量只记真实 captured。比分只取90分钟 `score.fulltime`。
`--season` 可重复，每个完整赛季独立提交；先归档所有页再整体核对身份，后续失败保留已完成赛季和安全原件。
默认每日保留10次额度，配额文件须持久保存并供本机采集共享；到界限明确报错，不自动跨日等待。
离线重放用 `--raw-file <原件.json>` 加同名 sidecar；参数仍须与原件一致，多页须用完整 assembled 归档。
`--aliases-file`、`--leagues-file`、`--quota-file`、`--audit-dir` 可指定替代路径。

别名在 `data/team_aliases/api_football.json`，proposed 逐条保留待审查，尚未核验的名称不自动合并。
官方比分优先，其次 fd/fdorg；拒绝、可疑重复和未对齐清单写入审计目录，默认不提交。
本轮韩职、澳超链接率及仅jc训练样本未满足完整验收，见[实际结果](reports/2026-11-02-t10-acceptance/README.md)。
T14 已接入最近三天的定时采集；配额与规范化数据在数据分支持久化，原始 JSON 保留为 Actions artifact；T17 定时模型只使用已配置的 jc/fd 来源。
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
T17 晚班与手动 `operation=predict` 自动训练并将两日冻结报告保存到数据分支 `forecasts/`，
详见上方「云端采集」。手工运行 `zucai predict` 的本地报告仍需自行归档；可视化窗口属于后续交付。

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
