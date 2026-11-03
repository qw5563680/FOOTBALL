# T20 · 欧战数据：欧冠 / 欧罗巴 / 欧协联的历史回填与每日增量

## 背景
竞彩常年在售欧冠、欧罗巴、欧协联，但数据分支里这三项赛事只有竞彩顺带抓到的本季 85 场（欧冠 49、欧罗巴 32、欧协联 4），
没有历史、没有模型，T17 每日报告把它们列为「无模型，未预测」。
现有三个外部数据源里：football-data.co.uk 不提供欧战；football-data.org 免费档**含欧冠**（`CL`），本账户实测最早可读
2023/24 季（T09，`docs/DATA_SOURCES.md` 第 5 节），但项目目前只映射九个国内联赛、「欧冠不入库」；
API-Football 免费档整季只开放 2022–2024（T10），最近三天的日增量可用，欧罗巴 / 欧协联的联赛 ID 候选为 **3 / 848**（需验证）。
本任务只做数据：把三项欧战的历史与每日增量接进现有采集、去重、身份核验和数据分支归档；模型留给 T21。

## 目标
1. 数据分支具备：欧冠 2023/24 至当季（fdorg），欧罗巴与欧协联 2022/23 至 2024/25 整季（apif），三项赛事当季每日增量（fdorg + apif）。
2. 欧战球队与竞彩中文队名对齐率可核验；未对齐的身份进入审计清单，不猜名。
3. 不增加 API-Football 每日请求次数（日端点本来就返回全部赛事，只改过滤配置）；fdorg 日窗口把 `CL` 并入同一次请求。

## 范围
1. **fdorg 欧冠**（`data/football_data_org/competitions.json`、`src/zucai/sources/fdorg_*`、`src/zucai/cloud_apis.py`）：
   - `competitions.json` 新增 `CL`：`league` 为 `欧冠`，`earliest_season` 以真实请求确认（预期 2023），记录 `verified_utc`；
   - 历史回填：`fetch fdorg --competition CL --season 2023 ... 2026 --assume-known-after 3h`，每季一次请求；
     淘汰赛加时 / 点球按既有 `regularTime` 规则取常规时间，两回合次回合不做任何合并；
   - 每日增量：云端 `apis` 的 fdorg 请求把 `CL` 加进同一个 `competitions=` 列表（代码里的 `COMPETITION_CODES`），请求次数不变；
   - 赛季边界：欧冠按 7 月换季（现有跨年默认规则），不进 `CALENDAR_LEAGUES`。
2. **apif 欧罗巴 / 欧协联**（`data/api_football/leagues.json`、`src/zucai/sources/apif_*`）：
   - 实现前用真实请求确认联赛 ID（候选 3 = UEFA Europa League、848 = UEFA Europa Conference League）与可读赛季
     （预期 2022–2024，当季整季预期被 `errors.plan` 拒绝），结论与原件 SHA256 写进 `leagues.json` 的 `season_probes`
     和 `docs/DATA_SOURCES.md`；
   - `leagues.json` 新增两项：`league` 分别为 `欧罗巴`、`欧协联`；
   - 历史回填：`fetch apif --league 3 --season 2022/2023/2024`、`--league 848` 同样三季，共 6 次整季请求，走现有配额守卫
     （`--reserve 10`）与分页规则；
   - 每日增量：现有 `--date` 日端点已返回全部赛事，只需把两项加入过滤配置，请求数为 0 增量；
   - 不从 apif 回填欧冠（避免与 fdorg 重复身份；fdorg 已覆盖 2023 起的欧冠）。
3. **身份对齐**：复用 T09 / T10 的主客方向比赛图自动核验，证据来源为数据分支里的 jc 欧战比赛（本季已有 85 场）与
   已核验的国内联赛别名桥接（英超、西甲、德甲、意甲、法甲、荷甲、葡超球队大多已有 fdorg 别名）；
   其余国家的球队（如布鲁日、萨尔茨堡、维京）若无 jc 证据，保留 `fdorg:team:<id>` / `apif:team:<id>` 身份并写入
   `fdorg_unmatched_teams.csv` / apif 未对齐清单，PR 描述列出对齐率与未对齐球队数；**不按名字相似度猜测**。
4. **去重与优先级**：沿用 jc > fd > fdorg > apif；欧战比赛只填空的 jc `counterpart`，不覆盖 jc 赛果；
   同一场在 fdorg 与 apif 都出现时按既有规则隔离疑似重复，不新增规则。
5. **回填入口**：把 T18 的 `cloud_feeds backfill` 扩展为 `--source fd|fdorg|apif`（默认 fd，保持 T18 行为不变）：
   `--source fdorg --leagues CL --from 2023 --to 2026`、`--source apif --leagues 3,848 --from 2022 --to 2024`；
   工作流 `operation=backfill` 新增输入 `source`（默认 `fd`）与 `leagues`（可空），API key 仍只来自 Secrets。
   幂等要求与 T18 相同：第二次运行新增 0。
6. **归档**：`export_feeds` 已按来源导出 fdorg / apif 行，无需改格式；确认 `feed_manifest.json` 的来源行数包含欧战，
   `verify` 通过。
7. **测试**（离线，新 fixture 裁剪到几十 KB）：fdorg `CL` 一季响应（含一场 `EXTRA_TIME` 淘汰赛）解析为常规时间；
   apif 3 / 848 一季响应与一条 `errors.plan` 拒绝；日增量过滤配置包含三项赛事；backfill `--source` 三种取值的范围生成与
   幂等；身份核验在有 jc 证据 / 无证据两种情况下的行为。
8. **文档**：`docs/DATA_SOURCES.md` 第 5、6 节补欧战覆盖、ID 与赛季实测、未对齐规则；README「云端采集」补 backfill 的
   `source` 参数与三项赛事的每日增量说明；`docs/ARCHITECTURE.md` 数据来源表补三项赛事。

## 不做
- 不训练欧战模型、不改 `params.json`、不改 P2 规则（T21）。
- 不从 apif 回填欧冠；不回填欧洲杯 / 世界杯（`EC` / `WC`）。
- 不改 fd 解析、去重、别名审批规则；不手工编写别名。
- 不增加 API-Football 每日请求次数；整季回填只在 `workflow_dispatch` 手动触发一次。
- 不提交数据库、原件或全量 JSON 到 main；`reports/` 不新增内容。

## 验收标准
- 真实回填：fdorg `CL` 2023–2026 四次请求、apif 3 / 848 各三季六次请求，PR 描述给出各赛事各赛季入库场数、
  拒绝 / 疑似重复行数、球队对齐率、未对齐球队数、数据分支提交号；`verify` 通过。
- 回填后触发一次 `operation=apis`：fdorg 请求数与回填前相同（`CL` 并入同一窗口），apif 请求数相同，摘要中出现三项赛事的入库行。
- 第二次相同范围 backfill 新增 0。
- 欧冠入库场数与公开赛程一致（2023/24 125 场，2024/25 起 189 场联赛阶段 + 淘汰赛，允许未完赛差异）。
- `pytest -q` 全绿、`ruff check src tests examples` 与 `ruff format --check src tests` 通过。
