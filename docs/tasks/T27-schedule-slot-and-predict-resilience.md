# T27 · 定时任务迟到 / 漏跑时的班次判定与预测补跑（小修）

## 背景
2026-11-03 的两档定时任务都出了问题：`40 2 * * *`（北京 10:40 早班）迟到 7 小时 22 分，于 UTC 10:02 才启动
（run 41615859011）；`40 9 * * *`（北京 17:40 晚班）当天根本没有触发。工作流用 **cron 字符串** 判定班次：
`FEED_SLOT = github.event.schedule == '40 2 * * *' ? early : late`，`predict` job 的条件是
`github.event.schedule == '40 9 * * *'`。于是迟到的早班 run 虽然在晚班时间段执行，仍按早班只采集不预测，
`predict` 显示 skipped；晚班又没来，当天没有任何新预测，直到审查者手动补跑 `operation=predict`（run 41631941968）。
GitHub 官方说明 schedule 事件在高负载时可能延迟甚至跳过，这不是偶发。审查者侧已有「保险检查」会在 UTC 03:23 / 10:23
核对并补跑，但工作流自身也应当按**实际启动时间**判断班次，不依赖哪条 cron 把它叫起来。

## 目标
1. schedule 触发的 run 按实际 UTC 启动时间判定早 / 晚班（以 09:00 UTC 为界，与手动 `slot=auto` 同一规则）。
2. 只要 schedule run 在晚班时间段启动，就执行 `predict`（含 scoreboard），不管是哪条 cron 触发的。
3. 手动 `operation=apis` 的行为不变（不触发 predict）；手动 `operation=predict` 不变。

## 范围
1. **班次判定**（`.github/workflows/football-feeds.yml` apis job）：schedule 事件改传 `--slot auto`，由 `zucai.cloud_feeds`
   现有的 auto 规则（UTC 09:00 为界）判定；apis job 增加输出 `slot`（early / late），用 `date -u +%H` 或 Python 计算，写入 `$GITHUB_OUTPUT`。
   `fd` 只在 UTC 周一 early 采集的规则不变。
2. **predict 条件**（predict job `if`）：改为 `(github.event_name == 'schedule' && needs.apis.outputs.slot == 'late')
   || (github.event_name == 'workflow_dispatch' && inputs.operation == 'predict')`；保留 `!cancelled() && needs.apis.result != 'failure'`。
   sporttery / scoreboard 的条件不改。
3. **同日两次预测**：若迟到的早班和正常晚班都在晚班时间段执行，会产生两批预测；确认 `forecast_archive` 与记分板
   「同场取开球前最后一批」的既有规则能正确处理，不需要去重；若需要，只在 ARCHITECTURE 说明，不改评分逻辑。
4. **测试**（`tests/test_cloud_predict_workflow.py`）：把 schedule 的两条 cron 分别在 UTC 09:00 前 / 后启动的四种组合
   加进参数化用例，断言 predict 是否运行只取决于启动时间；手动 apis / predict / 其他 operation 的既有断言不回退。
5. **文档**：`README.md`「云端采集」说明班次按实际时间判定；`docs/ARCHITECTURE.md` T17 一节补一句。

## 不做
- 不改 cron 时间、不增加第三条 cron；不改 `zucai.cloud_feeds` 的 auto 判定规则本身。
- 不改模型、预测冻结格式、记分板评分；不碰数据分支内容。
- 审查者侧的保险检查（Claude 定时任务）已于 2026-11-03 修订为同时核对 predict 是否执行，不在本任务范围。

## 验收标准
- 离线：`pytest -q` 全绿（含新增四种组合）；`ruff check src tests examples` 与 `ruff format --check src tests` 通过；actionlint 通过。
- 真实（审查者在 Actions 观察，不额外触发）：合入后首个在 UTC 09:00 之后启动的 schedule run，`predict` job 不再 skipped，
  数据分支 `forecast_manifest.json` 出现该 run 的批次；UTC 09:00 之前启动的 schedule run 仍只采集。
- 手动 `operation=apis slot=auto` 在任何时间都不触发 predict。
