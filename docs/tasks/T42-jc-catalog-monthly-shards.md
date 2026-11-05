# T42 · 竞彩完整清单与跳过审计按月分片（T39 建议修 2，小修）

## 背景
T39（PR #79）新增 `state/audit/jc_catalog.csv`（每次同步写入原响应全部编号）与 `state/audit/jc_skipped.csv`。
每天 2–3 次同步、每次数十行、每行约 150 字节，一年约 5–10 MB 且持续增长；`digest_context.audit_rows` 每次日报整文件读入。
审查时列为「建议修 2」，所有者与审查者同意合入后单独处理。

## 范围
1. 两份审计改为按**采集月份**分片：`state/audit/jc_catalog/<YYYY-MM>.csv`、`state/audit/jc_skipped/<YYYY-MM>.csv`
   （按 `captured_utc` 的 UTC 月份），字段、去重键与幂等规则不变；`sources/sporttery.save_schedule_audit` 只写当月分片。
2. `cloud_feeds.export_feeds` 的审计文件正则放宽到子目录分片并纳入 SHA256 清单；`restore_feeds` 对分片目录执行同样的「本快照以外的
   旧文件清理」；`digest_context.audit_rows` 只读本周起止涉及的分片（通常 1–2 个文件），其余不读。
3. 兼容：已归档的单文件 `jc_catalog.csv` / `jc_skipped.csv`（2026-11-05 起到合入前）按原样保留并继续被读取为 2026-11 分片的一部分；
   不迁移、不改写历史文件。
4. `docs/ARCHITECTURE.md` 数据分支文件表与 `docs/DATA_SOURCES.md` 竞彩一节同步。

## 不做
- 不改 T39 的缺号判定、措辞与清单字段；不改在售筛选；不动 `state/odds/<YYYY-MM>.jsonl` 等既有分片。

## 测试（离线）
跨月两次同步落在两个分片；导出清单含分片且恢复后字节一致；日报只读涉及分片（用监控打开路径的测试断言）；旧单文件归档照常读取。
`pytest -q`、`ruff`、`actionlint` 全绿。

## 真实验收（审查者）
合入后首个同步：分片文件出现、单文件不再增长；日报缺号核对结果与前一日一致。
