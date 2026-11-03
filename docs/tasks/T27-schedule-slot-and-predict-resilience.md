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
2. **每一次 schedule run 都执行 `predict`**（含 scoreboard），不管是哪条 cron、什么时候触发的：晚班迟到不再漏预测；
   早班（北京 10:40）也出一批冻结预测，让下午至傍晚开球的亚洲场次（亚冠精英、韩职、日职、澳超等）在开球前有预测——
   2026-11-03 实测：晚班 17:40 预测「今天」时，当天在售的 11 场里 10 场已开赛，整批为空（所有者 2026-11-03 决定加入）。
3. 手动 `operation=apis` 的行为不变（不触发 predict）；手动 `operation=predict` 不变。

## 范围
1. **班次判定**（`.github/workflows/football-feeds.yml` apis job）：schedule 事件改传 `--slot auto`，由 `zucai.cloud_feeds`
   现有的 auto 规则（UTC 09:00 为界）判定；apis job 增加输出 `slot`（early / late），用 `date -u +%H` 或 Python 计算，写入 `$GITHUB_OUTPUT`。
   `fd` 只在 UTC 周一 early 采集的规则不变。
2. **predict 条件**（predict job `if`）：改为 `github.event_name == 'schedule' || (github.event_name == 'workflow_dispatch' && inputs.operation == 'predict')`；
   保留 `!cancelled() && needs.apis.result != 'failure'`，且仍 `needs: [apis, sporttery]`（早班也先等竞彩同步完成再预测）。
   `slot` 输出仍保留给 fd 周一规则与摘要；sporttery / scoreboard 的条件不改。手动 `operation=apis` 仍不触发 predict。
3. **同日两次预测成为常态**（早班 + 晚班，迟到时可能更多）：确认 `forecast_archive` 与记分板
   「同场取开球前最后一批」的既有规则能正确处理，不需要去重；若需要，只在 ARCHITECTURE 说明，不改评分逻辑。
4. **测试**（`tests/test_cloud_predict_workflow.py`）：两条 cron 分别在 UTC 09:00 前 / 后启动的四种组合都断言 predict 运行、
   fd 采集只在 UTC 周一 early；手动 apis 不预测、手动 predict 预测、其他 operation 的既有断言不回退。
   同时更新该文件里「predict 只在晚班 schedule 运行」的旧断言与 README / ARCHITECTURE 的对应描述。
5. **文档**：`README.md`「云端采集」说明班次按实际时间判定；`docs/ARCHITECTURE.md` T17 一节补一句。

## 不做
- 不改 cron 时间、不增加第三条 cron；不改 `zucai.cloud_feeds` 的 auto 判定规则本身。
- 不改预测模型、训练参数或「冻结两个北京日」的范围；早班预测不新增 API 请求。
- 不改模型、预测冻结格式、记分板评分；不碰数据分支内容。
- 审查者侧的保险检查（Claude 定时任务）已于 2026-11-03 修订为同时核对 predict 是否执行，不在本任务范围。

## 验收标准
- 离线：`pytest -q` 全绿（含新增四种组合）；`ruff check src tests examples` 与 `ruff format --check src tests` 通过；actionlint 通过。
- 真实（审查者在 Actions 观察，不额外触发）：合入后首个早班与首个晚班 schedule run 的 `predict` job 都不再 skipped，
  数据分支 `forecast_manifest.json` 出现两个 run 的批次；早班批次里当天下午开球的亚洲场次带概率（有模型的联赛）；
  记分板对同一场只计开球前最后一批，`verify` 通过。
- 手动 `operation=apis slot=auto` 在任何时间都不触发 predict。
