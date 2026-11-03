非购买建议 · BET 禁用
<!-- zucai-daily:2026-11-03 -->

# 每日摘要 2026-11-03（北京时间）

本次 run ID：offline-1；摘要生成 UTC：2026-11-03T10:00:00+00:00。
竞彩同步至：2026-11-03；采集状态：jc=未记录 / fd=skipped / fdorg=partial / apif=complete。
采集归档 UTC：2026-11-03T09:40:00+00:00；记分板截点 UTC：2026-11-03T09:50:00+00:00。
缺失状态不推断为成功；以下均为已归档快照，不代表实时信息。

## 今日与明日预测

### 今日 2026-11-03

来源：[forecasts/2026-11-03/empty/forecast.json](../forecasts/2026-11-03/empty/forecast.json)；批次 empty-2026-11-03；生成 UTC：2026-11-03T09:55:00+00:00。

该批次无比赛。

### 明日 2026-11-04

来源：[forecasts/2026-11-04/empty/forecast.json](../forecasts/2026-11-04/empty/forecast.json)；批次 empty-2026-11-04；生成 UTC：2026-11-03T09:55:00+00:00。

该批次无比赛。

## 昨日结算

北京日 2026-11-02；按赛果可知时间 available_utc 归日，只列记分板 active_keys 中的原评分。
来源：[scoreboard/scored.jsonl](../scoreboard/scored.jsonl)；汇总：[scoreboard/summary.json](../scoreboard/summary.json)。

| 比赛 / 记录 ID | 冻结胜 / 平 / 负 | 实际结果 | LogLoss | Brier |
| --- | --- | --- | --- | --- |
| jc:t25-yesterday / score-one | 48.00% / 28.00% / 24.00% | 2:1 主胜 | 0.7340 | 0.4064 |

当日 LogLoss：现有归档未提供；当日 Brier：现有归档未提供（不重算日均值）。
累计样本：37；累计 LogLoss：0.9786；累计 Brier：0.6123。

- 测试联赛：前瞻样本 37 / 200；证据不足。
- 模拟盈利证据（A / 1.05）：已结 1 / 500；证据不足。样本进度不替代其余门槛，BET 保持禁用。

## 虚拟账户（1.05 档）

来源：[scoreboard/summary.json](../scoreboard/summary.json) accounts.1.05；[scoreboard/ledger.jsonl](../scoreboard/ledger.jsonl)。
资金为虚拟资金；昨日注单指昨日生成或昨日结算，注额与 SP 均照录冻结值。

| 账户 | 当前资金 | 可用现金 | 累计 ROI | 昨日注单与结果 |
| --- | --- | --- | --- | --- |
| A 单场固定额 | 10150.00 | 10150.00 | 150.00% | ticket-one（历史补记）：合成主队 VS 合成客队 主胜 SP=2.5；注额 100.00；中；返还 250.00；盈亏 150.00 |
| B 单场四分之一凯利 | 10000.00 | 10000.00 | 未提供 | 无已归档的昨日注单 |
| C 热门对照组 | 10000.00 | 10000.00 | 未提供 | 无已归档的昨日注单 |
| D 二串一 | 10000.00 | 10000.00 | 未提供 | 无已归档的昨日注单 |
| E 三串一 | 10000.00 | 10000.00 | 未提供 | 无已归档的昨日注单 |

## 异常与采集记录

来源：[feed\_manifest.json](../feed_manifest.json) collection。

- jc：未记录；疑似重复 未记录；未记录原因。
- fd：skipped；疑似重复 0；仅 UTC 周一早班采集。
- fdorg：partial；疑似重复 20；单行拒绝或疑似重复，详见 state/audit。
- apif：complete；疑似重复 0；未记录原因。
- apif 配额剩余：7420；plan：Pro；limit_day：7500。

仅转述冻结数据；缺失不填零，门槛标记不授予 BET 权限。
