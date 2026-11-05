# T43 · 可手动触发的「整条链」操作，摆脱 GitHub 定时器迟到（小修）

## 背景
`football-feeds.yml` 的两条 cron 实际触发时间（UTC）核对如下：`40 9 * * *`（北京 17:40）每天 09:49–10:02 触发，正常；
`40 2 * * *`（北京 10:40）连续四天在 **17:18–19:45** 才触发（11-02 run 15、11-03 run 63、11-04 run 86、11-05 run 98），
迟到约 15 小时，落在北京时间次日 01:18–01:58，于是出现「凌晨 1 点多发出标着次日日期的日报」。11-05 新增的 `40 7 * * *`
（北京 15:40，T39 胜负彩提前班）尚无触发记录，可能同样迟到。GitHub 对低活跃仓库的 schedule 事件会排队延迟，仓库无法控制。

现有 `workflow_dispatch` 没有一个操作能复现 schedule 事件的整条链：`apis` 只跑采集，`sync` 只跑竞彩，`predict` 跑预测→记分板→日报。
审查者的保险检查只能分三次触发、串行等待，约 20 分钟，且 schedule-gate 的期次判定对手动触发不生效。

## 范围
1. **新增 `operation=daily`**：手动触发时与 `schedule` 事件行为完全一致——apis（`slot=auto`）→ sporttery → predict → scoreboard → digest，
   各 job 的 `if:` 把 `inputs.operation == 'daily'` 与 `github.event_name == 'schedule'` 同等处理（含 predict 的 `needs.apis.result != 'failure'`、
   digest 的 `needs.sporttery` 传递、`cloud_predict` / `daily_digest` 的参数）。
2. **新增输入 `gate`（boolean，默认 false）**：`daily` 且 `gate=true` 时先运行 `schedule-gate`（读取 `state/sfc/` 判定当日或次日是否有停售期次），
   `run_early=false` 则整条链跳过，与 `40 7 * * *` 班一致；`gate=false` 不运行 gate。
3. **cron 保留不变**（`40 2`、`40 7`、`40 9`），作为第二道保险；并发组已保证同一时刻只有一条链在跑。
   `daily_digest` 的「本日第 k 次更新」按既有规则计数，不因多一次运行而出错。
4. **Actions 摘要**在 daily 运行时首行写明「手动整条链（gate=…）」，与 schedule 运行可区分；`forecast_manifest` / `feed_manifest` 的 run 记录
   保留 `event_name` 与 `operation`，便于事后区分哪次是定时、哪次是补跑。
5. 文档：README「云端采集 / 定时」一节说明 cron 迟到现象与 `daily` 的用途；ARCHITECTURE 工作流表加 `daily`；`docs/RUNNER.md` 不改。

## 不做
- 不改任何采集、预测、记分板、日报的业务逻辑；不改模型、params、P2、BET、T28。
- 不删 cron；不引入外部调度服务（外部触发由审查者的调度器调用 `workflow_dispatch` 完成，不在仓库内实现）。
- 不改 `sync` / `apis` / `predict` 等既有手动操作的语义。

## 测试（离线）
沿用 `tests/test_early_schedule_workflow.py` 的 YAML 条件测试方式：`daily` 事件下五个 job 的 `if:` 与 schedule 等价；`daily` + `gate=true` 且
gate 输出 `run_early=false` 时四个下游 job 跳过；`gate=false` 时 gate job 不运行；其他 `operation`（apis / sync / predict / digest …）条件不变；
`actionlint` 通过。

## 真实验收（审查者）
1. 合入后手动触发 `{"operation":"daily"}`：五个 job 全部运行成功，Issue #39 当日评论更新且头部「本日第 k 次更新」递增。
2. 手动触发 `{"operation":"daily","gate":true}`：有期次日跑整条链；无期次日只跑 gate 并在摘要写「跳过提前班次」。
3. 审查者随后把自己的调度器改为：02:41 / 07:41 / 09:41 UTC 检查最近 15 分钟是否已有 schedule run，没有则触发 `daily`（07:41 带 `gate=true`）；
   连续三天观察 Issue #39 早班（北京 10:40–11:10）与 15:40 班是否准时出现。

## 验收标准
- `daily` 与 schedule 行为一致且可区分；`gate` 输入生效；既有操作不变；文档同步；测试离线。
