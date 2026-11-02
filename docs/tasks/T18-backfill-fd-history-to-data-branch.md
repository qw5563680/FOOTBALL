# T18 · 回填 football-data.co.uk 多赛季历史到数据分支

## 背景
T17 验收（run 41509202754）显示：云端每日训练只能用数据分支里的数据，而 fd 在数据分支中只有 T14 每周一拉取的
**当季**增量，各联赛训练场数仅 72–427 场。T15 评估与选参用的是实现者工作目录里的六赛季静态库
（15 联赛、约 2.7 万场 fd 赛果，每联赛 1000–3000 场），从未进入数据分支。于是每日模型的训练语料远小于
P2 证据所用语料，P2 标签虽按 A1 策略正确继承，但实际模型偏弱。本任务一次性把同样范围的历史回填进数据分支，
让 T17 的每日训练与 T15 的评估吃到同一份历史。

## 目标
1. 数据分支 `data/football-feeds` 包含 15 个已核验 fd 联赛的全部可得历史：
   按季文件联赛（E0、E1、SP1、I1、D1、F1、F2、N1、P1）2122–2627 六季；
   合并文件联赛（JPN、SWE、NOR、FIN、USA、BRA）2021–2026 六个自然年。
2. 回填后 `operation=predict` 的各联赛训练场数达到千场量级，与 T15 报告「数据覆盖」表的 fd 赛果数一致（允许新增比赛造成的小差异）。
3. 过程可审计：请求次数、间隔、robots 结论、原件 SHA256 与拒绝 / 可疑重复审计全部留档；可重复运行而不产生重复行。

## 范围
1. **新增 `cloud_feeds backfill` 操作**（`src/zucai/cloud_feeds.py` 或独立 `src/zucai/cloud_backfill.py`）：
   `python -m zucai.cloud_feeds backfill --archive ../cloud-data --run-id <id> [--leagues E0,JPN] [--from 2122 --to 2627]`：
   - 恢复归档到隔离工作库；按 `data/football_data_co_uk/leagues.json` 中 `status=verified` 的联赛生成范围：
     按季联赛逐季调用现有 `fetch fd --div <code> --season <ssss>`，合并文件联赛一次调用 `--country <code> --from-season 2021 --to-season 2026`；
     全部带 `--assume-known-after 3h`，与 T15 相同；
   - 复用现有限速器（每跳 ≥ 2 秒，T08 已实现）并在日志记录实际最短间隔；不改解析、去重、别名、可知时间规则；
     jc > fd 的优先级与疑似重复隔离沿用，审计 CSV 追加到 `state/audit/fd_*.csv`；
   - 单季文件失败（404 / 网络）不阻断其他季，记 `partial` 并列出失败项；
   - 结束后 `export_feeds` 写回归档，`feed_manifest.json` 正常更新；原件与审计目录上传为 artifact（90 天）。
2. **幂等**：第二次对同一范围运行，入库新增行数应为 0（或只含两次运行之间新完赛的场次），
   不产生重复 `matches` / `results` / `closing_odds`；测试用两份相同离线 CSV 证明。
3. **工作流**：`football-feeds.yml` 的 `workflow_dispatch` 增加 `operation=backfill`（只在 `apis` job 上跑，
   `ubuntu-latest`，`timeout-minutes: 60`），复用现有写回锁与 `publish-football-feeds.sh`；不加入定时计划。
   `sporttery` 与 `predict` 在 backfill 时跳过。
4. **robots 与文档**：实现前重新读取 football-data.co.uk 的 robots.txt，把日期与结论写进 `docs/DATA_SOURCES.md`；
   README「云端采集」加 `operation=backfill` 用法与「一次性，不定时」说明；`docs/ARCHITECTURE.md` 模块表补一行。
5. **测试**（离线）：范围生成（按季 / 合并两类、只取 verified、`--leagues` 与 `--from/--to` 过滤）；
   幂等（两次导入行数）；partial 标记；`export_feeds` 往返行数一致；工作流条件（backfill 时 predict / sporttery 跳过）。

## 不做
- 不回填 fdorg / apif 的历史（它们不是 T15 的评估来源，A1 来源集合也不含它们）。
- 不改 `params.json`、不重跑 tune、不改 P2 门槛与证据。
- 不改 fd 解析、去重、别名审批和可知时间假设；不把 `assumed` 改成其他标记。
- 不改每周一当季增量的现有逻辑。
- 不提交数据库、原件或全量 JSON 到 main；`reports/` 不新增内容。

## 验收标准
- 真实触发一次 `workflow_dispatch operation=backfill`：PR 描述给出 run ID、CSV 请求次数与最短间隔、
  各联赛新增 fd 赛果数、拒绝 / 可疑重复行数、数据分支提交号；`verify` 通过。
- 回填后各联赛 fd 赛果总数与 `reports/2026-11-02-t15-model-selection/README.md`「数据覆盖」表逐联赛对照，
  差异只能来自 2026-11-01 之后新完赛的场次，PR 描述列出对照表。
- 回填后再触发一次 `operation=predict`：15 联赛训练场数均 ≥ 1000（芬超、法乙等小联赛 ≥ 800），
  P2 标签仍为 7 可用 / 8 实验；PR 描述给出该 run ID 与各联赛训练场数。
- 第二次 backfill 新增行数为 0 或仅为期间新完赛场次。
- `pytest -q` 全绿、`ruff check src tests examples` 与 `ruff format --check src tests` 通过。
