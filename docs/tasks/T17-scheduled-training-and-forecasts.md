# T17 · 定时自动训练与每日冻结预测

## 背景
T14 让四个数据源每天自动增长并写回 `data/football-feeds` 分支；T15/T16 给出每个联赛的推荐半衰期、未知球队先验、
校准开关和 P2 门槛（A1 策略绑定）。但「用最新数据训练模型 → 对今天在售比赛出预测 → 冻结报告」这最后一步仍要人工
在本机跑 `zucai train` 与 `zucai predict`，README 的 T06 一节也写着「预测定时任务属于后续交付」。
本任务把这一步接到现有工作流后面，让每天的预测报告自动产生、自动归档，并且每一份都带 P2 标签和模型证据。

## 目标
1. 每天在竞彩采集完成后，自动用当天恢复出的数据库为 15 个已配置联赛各训练一个 Dixon-Coles 模型。
2. 自动对北京时间「今天剩余 + 明天」的在售竞彩比赛生成纯模型预测，冻结为 forecast.json / report.md / report.csv。
3. 模型与预测归档到数据分支，Actions 摘要给出当批汇总；main 不进任何模型或预测文件。
4. P2 可用 / 实验标签由 T16 的策略绑定自动判定；BET 始终禁用，报告不包含任何购买建议。

## 范围
1. **新增模块 `src/zucai/cloud_predict.py`**，入口 `python -m zucai.cloud_predict run --archive ../cloud-data --params ../app/data/model/params.json --run-id <id>`：
   - 调 `cloud_feeds.restore_feeds` 把归档恢复成一份只读工作库，记录库 SHA256；
   - 读取 `params.json` 中全部联赛条目，对每个联赛执行与 `zucai train` 等价的训练：`--source` 取该条目的 `sources`
     （当前为 `jc,fd`，不得擅自加入 fdorg/apif，否则 A1 的来源集合检查不通过）、半衰期 / 校准开关由配置解析、
     不加 `--strict-availability` 与 `--replay-known-after`、`--since` 取截点前 56 天只作冒烟回测（不是 P2 证据），
     `--predictions-out` 不启用；模型写到 `models/<league>.pkl`，训练 JSON 写 `models/<league>.train.json`；
   - 单个联赛训练失败（历史不足、拟合失败）不阻断其他联赛，记入摘要并标记 `partial`；
   - 用全部成功的模型调 `predict_batch`，比赛选择为北京时间当天与次日两个开球自然日（两批、两个输出目录），
     已开球场次按 T06 现有规则排除；库里没有对应联赛模型的场次照常列为 excluded 并写原因；
   - 输出目录为归档内 `forecasts/<YYYY-MM-DD 北京日>/<run-id>/`，内容沿用 T06 的四个文件；同日多次运行不覆盖，各自成目录；
   - 把本次模型目录按 `models/<run-id>/` 归档，并维护 `models/latest.json`（各联赛最新模型路径、SHA256、训练截点、
     训练库 SHA256、P2 标签）。
2. **数据分支归档与清单**：`cloud_feeds.export_feeds` / `archive_info` 不变；新增 `forecast_manifest.json`
   （每个 forecast 目录与模型文件的 SHA256、生成时刻、批次 ID、库 SHA256、各联赛 P2 标签），
   `scripts/publish-football-feeds.sh` 的 `git add` 列表加入 `models forecasts forecast_manifest.json`。
   旧归档没有这些目录时 `restore_feeds` 与 `verify` 照常通过。
3. **工作流**：`.github/workflows/football-feeds.yml` 新增 job `predict`：`needs: [apis, sporttery]`、
   `if: ${{ !cancelled() && needs.apis.result != 'failure' }}`（sporttery 被跳过时仍运行，但摘要注明「竞彩数据非今日」）、
   `runs-on: ubuntu-latest`、`timeout-minutes: 45`，只在 `schedule` 的晚班（UTC 09:40，北京 17:40）和
   `workflow_dispatch` 新增的 `operation=predict` 时运行；早班不出预测。
   步骤：checkout 代码到 `app`、checkout 数据分支到 `cloud-data`、`pip install -e ./app`、运行 `cloud_predict run`、
   `publish-football-feeds.sh` 写回、上传 `forecasts` 与 `models` 为 artifact（90 天）。
   `ZUCAI_PARAMS` 指向 `app/data/model/params.json`（T16 新增的定位方式），不复制配置到工作目录。
   共用现有 `concurrency: write-football-feed-data`。
4. **Actions 摘要**（`GITHUB_STEP_SUMMARY`）：每联赛一行（训练场数、训练截点、半衰期来源、P2 标签、本批预测场数），
   随后是当批 report.md 的比赛表（模型概率三列 + 市场去水概率三列，EV 列照 T06 规则），顶部固定一行
   「非购买建议 · BET 禁用 · P2 标签含义见 README」。
5. **测试**（全部离线）：用 `data/fixtures/t16/train_history.csv` 之类合成语料 + 一份含两场在售比赛与 had 快照的
   fixture 走通 restore → 多联赛训练（其中一个联赛故意历史不足）→ 两日预测 → 归档目录与 `forecast_manifest.json`；
   断言：模型文件 SHA256 与 manifest 一致；失败联赛被标记 partial 且不影响其他联赛；重复运行不覆盖旧目录；
   P2 标签与 `model_readiness` 一致；`publish` 的 `git add` 列表覆盖新目录（可用 shell 脚本文本断言）。
6. **文档**：README「云端采集」一节加 `predict` job 的时间表、归档位置与 `operation=predict` 用法；
   T06 一节删去「预测定时任务属于后续交付」；`docs/ARCHITECTURE.md` 模块表加 `cloud_predict`，
   「数据分支」说明加 `models/`、`forecasts/`、`forecast_manifest.json`。

## 不做
- 不重跑 `tune`、不改 `params.json`、不改 P2 门槛与 A1 规则；P2 标签只读配置。
- 不把 fdorg / apif 加进训练来源（留给下一次 T15 式的重新评估）。
- 不做 14 场胜负彩（T07）的定时报告，不做网页展示，不发任何通知（邮件 / IM）。
- 不改采集、去重、可知时间规则，不改数据库 schema。
- 不启用 BET，不输出任何形式的投注建议或资金分配。

## 验收标准
- 真实触发一次 `workflow_dispatch` 的 `operation=predict`：数据分支出现 `models/<run-id>/`、`forecasts/<日期>/<run-id>/`
  与 `forecast_manifest.json` 的新提交；Actions 摘要含 15 个联赛的训练行和当批比赛表。PR 描述写明 run ID、
  成功 / 失败训练的联赛数、两日预测场数、excluded 场数及原因分布、数据分支提交号。
- 7 个 P2 可用联赛的模型在摘要中显示「P2 可用」，其余 8 个显示「P2 实验」；任意一场的 `model_probabilities` 之和为 1。
- `verify` 模式在托管机恢复新归档并通过校验（含新 manifest）。
- `pytest -q` 全绿、`ruff check src tests examples` 与 `ruff format --check src tests` 通过；`reports/` 不新增内容。
