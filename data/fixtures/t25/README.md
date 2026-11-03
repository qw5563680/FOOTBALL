# T25 离线摘要样本

全部为合成数据，不含真实采集、正式 key 或真实 Issue。`forecast.json` 由 T19 合成样本裁剪并改日期，
保留 H/D/A=0.48/0.28/0.24，比分矩阵与前六项人工设置为一致的离线样本；摘要只展示原前五。
`summary.json` / `summary_gates.json` 是页面测试用的聚合快照，分别模拟 37/200 前瞻样本和 1/500 已结注单；
`scored.jsonl` / `ledger.jsonl` 仅保留一条明细，不声称它们可重新算出完整聚合数据。
`collection.json` 模拟 partial、跳过、未知来源及套餐配额；`github.json` 模拟 Issue 和机器人评论。

三个 `expected_*.md` 独立按上述原值写出，分别覆盖 partial/无模型、门槛达成、最新批次为空。
测试将样本包装为现有 feed/forecast/scoreboard 格式并生成合法清单哈希；不训练模型，不执行记分板计算。
模型文件只在测试临时目录写入不可执行占位字节，用于验证归档，不反序列化。
HTTP 全部由 responses 拦截。真实手机通知与置顶权限需 Actions 验收。
