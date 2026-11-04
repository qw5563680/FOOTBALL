# T32 · 技术数据历史回填与日常增量：覆盖率、配额、幂等（阶段 C）

## 前置依赖
T30（端点与字段实测）、T31（表、解析器、索引）。所有者对请求间隔 / job 时长 / 原件归档位置的决定。


## 所有者决定（2026-11-04 定稿）
1. **请求间隔**：技术数据端点（statistics / events / lineups / players / injuries 与索引用的 fixtures 页）最小间隔 **1.0 秒**，以响应头 `x-ratelimit-limit`（实测 300/分钟）与 `x-ratelimit-remaining` 为准，剩余 ≤ 20 时等到下一分钟；遇 429 立即停止本批。日常赛程采集与 `/status` 保持 6.1 秒不变。
2. **回填 job**：新 job `stats-backfill`，`timeout-minutes: 300`，与现有 `write-football-feed-data` 并发组互斥。
3. **原件归档**：每批原件打包为 `stats-raw-<run_id>.tar.zst`（或 zip）+ `SHA256SUMS`，上传到 GitHub Release（tag `stats-raw-YYYYMM`，每月一个），`feed_manifest.json` 记录 release tag、文件名与哈希；artifact 仍保留 90 天作为副本。规范化表进数据分支。
4. **接入顺序**：statistics（含 xG，xG 窗口自 2024 季起）→ events → lineups → players；injuries 只对有数据的 11 个联赛（英超、西甲、意甲、德甲、法甲、荷甲、丹超、瑞超、挪超、美职、澳超）。
5. 预算口径：22,075 场 × 4 端点 ≈ 88,400 次 + 索引 ≈ 40 次；日常预留 300 次；按 1 秒间隔每 run 最多 7,000 次（受日额度限制）。

## 范围
1. **回填作业**（新 workflow job `stats-backfill`，`operation=stats-backfill`，输入 `endpoints`、`leagues`、`seasons`、`max_requests`、`resume`）：按联赛季 × 端点分批，游标写 `state/stats_backfill_cursor.json`，可暂停、续跑；每批先过 `DailyQuota`，剩余 ≤ `reserve + 日常预留(默认 300)` 即停止并写部分结果；`HTTP 200 + errors` 记失败；空响应允许最多 2 次补抓（间隔 ≥ 7 天），不支持的端点 / 联赛进 `unsupported` 名单不再重试。
2. **日常增量**（并入现有 `apis` 晚班或独立 job）：对前一日完赛且已链接的比赛请求已启用端点；对 jc 在售比赛在开球前 60–30 分钟尝试 `fixtures/lineups` 一次（记录 `captured_before_kickoff`），定时不可靠时记录缺失原因不补造。
3. **原件归档**：按所有者决定写 Release 附件（压缩包 + SHA256 清单）或 artifact；规范化表进数据分支并进 manifest。
4. **覆盖率报告**：按赛事 × 赛季 × 端点列应取 / 已取 / 有效 / 空值 / 失败 / 未支持；写 `state/stats_coverage.json` 并进摘要与每日报告异常节。
5. **文档**：README 云端采集补作业与预算；ARCHITECTURE 补游标、归档与失败分类；DATA_SOURCES 补请求次数与覆盖率表。

## 不做
- 不改模型 / 特征；不改既有采集请求数；不购买更高套餐；不改赛果可知时间、冻结预测、虚拟账户。

## 失败处理
限流 429 立即停止本批；权限错误整联赛季标 `unsupported` 待人工复核；网络失败计入配额且不重试本轮；归档失败则该批不入库。

## 测试（离线）
游标续跑、配额触底停止、日常预留、空响应补抓计数上限、unsupported 不重试、errors 判失败、覆盖率统计、原件归档失败回滚、幂等（重复批次 0 新增）、赛前阵容捕获时刻判定。

## 真实验收（审查者触发）
首批：英超 2025/26 statistics 全季（约 380 次）→ 覆盖率 ≥ 95%，剩余配额 ≥ 日常预留；中断后 `resume` 续跑新增 0 重复；第二次同范围运行请求 0；日常增量连续两天摘要出现统计覆盖行。

## 完成标准
pytest / ruff / actionlint 通过；总体覆盖率表写入 DATA_SOURCES；大文件不进 main。
