# T72 球队＋市场融合历史回溯（收盘价）

这个分支保存 [PR #259](https://github.com/qw5563680/FOOTBALL/pull/259) 的原始离线验收文件。

- `report.json` 是完成的回溯报告，含三段分母、排除项、指标、滚动回测截点和输入哈希。
- `weight-selection.json` 是只用训练段选定的权重及 101 项网格成绩。
- `training.jsonl`、`validation.jsonl`、`holdout.jsonl` 是逐场证据。
- `first-attempt.log` 保留首次退出记录，当时没有生成报告或权重文件。`completed-attempt.log` 是修复读取类型后完成的运行日志。[首次失败和重跑登记](https://github.com/qw5563680/FOOTBALL/pull/259#issuecomment-7194010081)

在此目录执行 `sha256sum -c SHA256SUMS` 可核对原始文件。[事前协议审查](https://github.com/qw5563680/FOOTBALL/pull/259#issuecomment-7193832203)
