# T54 离线验收资料

代码版本 [8593aa6bac41a36cb3864880ae0bfe860d715dcb](https://github.com/qw5563680/FOOTBALL/commit/8593aa6bac41a36cb3864880ae0bfe860d715dcb)。

资料目录：[acceptance/2026-11-12-t54](acceptance/2026-11-12-t54/summary.json)。

- 26163 固定批次13/14→14/14；仅重新计算修复的第3场，独立报告原样保留其余13场。
- 同环境修复前后13场模型结果逐字节一致；跨环境全量重算的两场浮点末位差异另有诊断，不混同两种比较。
- 全部2369个源档案SHA不变；7队登记完整；2728项全量测试通过。
- `checks/*red*` 为修复前预期失败记录，最终结果见 `checks/final-pytest.xml` 和绿色检查日志。
- `SHA256SUMS` 与代码分支 reports/t54-jc-short-name/SHA256SUMS 相同；提交版本和源文件哈希见 `implementation-version.json`。

这不是正式数据发布。没有数据库、密钥或生产改动，完整原件引用固定数据提交。
