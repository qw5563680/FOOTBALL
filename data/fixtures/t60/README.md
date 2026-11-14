# T60 离线训练样本

`history.jsonl` 是数据分支 `1657093073ea0e231cef476e1eac4e9ac58ebcb8`
经正常 restore 校验后的规范行裁剪，122 行、约 38 KB。保留真实比赛 ID、球队名、
状态、counterpart、UTC、90 分钟比分和可知性字段，不含赔率；原库与样本哈希见 `provenance.json`。

每联赛取最早 50 场赛果、各季首尾 APIF 赛果，并加入 2026-11-07 两场 JC 及其 APIF counterpart。
这两个 APIF counterpart 在真实归档中仍为 scheduled、没有赛果。
去重/冲突测试明确把对应 JC 的真实比分和可知时间复制到 APIF 行，模拟后来完赛的跨源重叠；
这项测试变形不是归档实测。历史每日流程测试仅在已验证恢复副本中恢复目标 scheduled 状态，
不把历史回放称为当时存在的预测。完整归档验收另见
`reports/t60-apif-history-domestic-training/README.md`。
