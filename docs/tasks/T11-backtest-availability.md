# T11 · 回测用的赛果可知时间重放

## 目标
让 T05 的周滚动回测在"赛果采集晚于比赛很久"的库上仍能得到有意义的折内训练数据，同时**不改动数据库里任何
`available_utc`**，并把重放假设在报告里完整披露。

## 背景
T08 验收暴露的问题：竞彩 833 场赛果全部在 2026-11-01 才 captured，按路线 B 它们在之前任何周截点都不可知，
于是 9 月到 10 月的每一折都只能用 8 月底之前的数据训练，当季比赛对模型毫无贡献，回测成绩甚至劣于等概率参照。
这是回测方法与采集历史的矛盾，不是导入错误。路线 B 第 3、6 条禁止把 `captured` 改写成 `assumed`，所以解法只能在
训练/回测的内存层做重放，而不是改库。

## 范围
1. `zucai train` 新增 `--replay-known-after 3h`（延时可调，默认关闭）：
   - 读取历史后，对 `available_source in ('captured','published')` 且 `available_utc - kickoff_utc > 延时` 的行，
     在内存中把可知时间改为 `kickoff_utc + 延时`，并把该行的 `available_source` 标为 `replayed`；
   - 数据库不写任何东西；`read_history` 的返回值加 `attrs["replay_delay"]`。
2. 报告披露：`backtest` 顶层与每折新增 `replayed_training_share`、`replayed_eval_share`；
   `availability_basis` 取值增加 `replayed`（只要任一已用行被重放）；模型文件元数据同样记录。
   README 与 ARCHITECTURE 的评估一节写明：带 `replayed` 的指标只能叫"开发阶段历史回放"，不能当前瞻验证。
3. `--strict-availability` 与 `--replay-known-after` 互斥，同时给出直接报错。
4. 回测报告新增同一评估集的**等概率参照**（主/平/客各 1/3）的对数损失和 Brier，作为固定下限；T08 验收里手工算过一次，
   这次写进程序。
5. 测试：重放只影响超过延时的行；库文件字节不变；披露字段正确；互斥报错；等概率参照数值。

## 不做
- 不改 `results` 表，不加列。
- 不改 T06 预测路径：真实预测仍只用库里的可知时间。
- 不调参、不改半衰期。

## 验收标准
- 在 T08 验收同一份英超副本上，`--source jc,fd --replay-known-after 3h` 的每折训练场数随时间增长（不再停在 8 月底），
  PR 贴出各折 `training_games`、两种占比、对数损失、Brier 与等概率参照的并列表。
- 不带开关时结果与 T08 验收逐字节一致。
