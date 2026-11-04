# T32 · 技术数据历史回填与日常增量：覆盖率、配额、幂等（阶段 C）

## 前置依赖
T30（端点与字段实测）、T31（表、解析器、索引）。所有者对请求间隔 / job 时长 / 原件归档位置的决定。


## 所有者决定（2026-11-04 定稿）
1. **请求间隔**：技术数据端点（statistics / events / lineups / players / injuries 与索引用的 fixtures 页）最小间隔 **1.0 秒**，以响应头 `x-ratelimit-limit`（实测 300/分钟）与 `x-ratelimit-remaining` 为准，剩余 ≤ 20 时等到下一分钟；遇 429 立即停止本批。日常赛程采集与 `/status` 保持 6.1 秒不变。
2. **回填 job**：新 job `stats-backfill`，`timeout-minutes: 300`，与现有 `write-football-feed-data` 并发组互斥。
3. **原件归档**：每批原件打包为 `stats-raw-<run_id>.tar.zst`（或 zip）+ `SHA256SUMS`，上传到 GitHub Release（tag `stats-raw-YYYYMM`，每月一个），`feed_manifest.json` 记录 release tag、文件名与哈希；artifact 仍保留 90 天作为副本。
6. **规范化行表归档（2026-11-04 补充决定，方案 1）**：`match_stats` / `match_events` / `match_lineups` 三张行表**不再整表进数据分支**。
   T31 实测每行约 1.3 KB，22,075 场外推四表每次快照约 3 GB，Git 分支承受不了。改为：每次回填或日常增量把三张行表按「联赛 × 供应商赛季」
   导出为压缩 JSONL（`stats-rows-<league>-<season>.jsonl.zst`）与 `SHA256SUMS`，和原件放同一个 Release（tag `stats-raw-YYYYMM`）；
   `feed_manifest.json` 的 `technical_data` 记录每个分片的 release tag、文件名、行数与哈希。数据分支只保留 `apif_fixture_index`、
   `state/stats_coverage.json`、`state/stats_backfill_cursor.json`、`state/audit/stats_*.csv` 与分片哈希清单。
   `restore_feeds` 默认只还原索引与审计（日常预测不需要行表）；新增 `stats restore-rows --release <tag>`（或等价参数）按清单下载、校验哈希后装回三张表，供 T33 离线使用。
   `check_stats` 对行表的逐行校验只在装回后执行；日常导出只校验索引。
4. **接入顺序**：statistics（含 xG，xG 窗口自 2024 季起）→ events → lineups → players；injuries 只对有数据的 11 个联赛（英超、西甲、意甲、德甲、法甲、荷甲、丹超、瑞超、挪超、美职、澳超）。
5. 预算口径：22,075 场 × 4 端点 ≈ 88,400 次 + 索引 ≈ 40 次；日常预留 300 次；按 1 秒间隔每 run 最多 7,000 次（受日额度限制）。

## 审查者补充（T31 真实验收后，2026-11-04）
- **Actions 入口**：新增 `operation=stats-backfill` 之外，T31 的 `stats ingest-fd` 与 `stats link --fetch` 现在没有任何 Actions 入口。`stats-backfill` job 须覆盖：
  (a) 索引：7 桥接联赛 + 欧冠 2 的 `fixtures?league&season`（2022 起每季一页，共约 40 次）；(b) fd 九主联赛季文件的 `stats ingest-fd`（fd 免费、无 key，在 apis 作业周一 early 的 fd 抓取后直接入库，或在 `stats-backfill` 内重下）；
  (c) 四端点回填。真实验收顺序：索引 → fd → statistics 英超 2025/26。
- **别名前置**：所有者已在 [PR #49 评论 6627072639](https://github.com/qw5563680/FOOTBALL/pull/49#issuecomment-6627072639) 批准 7 桥接联赛 45 个 apif 球队 ID（规范名 = 库内 fd 现名）和 fd 别名 `Den Haag` → `海牙`；7 个无证据 ID 保留 `proposed`。
  实现者第一步按该评论写入 `data/team_aliases/api_football.json`（`status: approved`、`approved_by`、`approved_utc`、`approval_pr`）与 `football_data_co_uk.json`，并用离线索引页 fixtures 验证 2022、2024 季拒绝清零；`Den Haag` 两行历史合并走既有 reconcile 路径。
- **同场双行**：`matches` 里同一场同时有 `fd:` 与 `fdorg:` 两行时（T31 实测 14 场，法甲 / 葡超 / 西甲最近一轮），索引优先绑 `fd:` 行，并在 `stats_link_audit.csv` 记 `preferred_fd`；fdorg 行留给 T24 的 relink 去重，不在本任务改导入规则。
- **性能**：`check_stats` 实测 0.16 s / 5.9k 行；行表移出数据分支后日常导出只校验索引，不必再优化。
- **null 语义**：apif 返回 null 的 `red_cards` 等保留 null（missing_reason `null`），不得补 0；T33 从 events 推导牌数。

## 范围
1. **回填作业**（新 workflow job `stats-backfill`，`operation=stats-backfill`，输入 `endpoints`、`leagues`、`seasons`、`max_requests`、`resume`）：按联赛季 × 端点分批，游标写 `state/stats_backfill_cursor.json`，可暂停、续跑；每批先过 `DailyQuota`，剩余 ≤ `reserve + 日常预留(默认 300)` 即停止并写部分结果；`HTTP 200 + errors` 记失败；空响应允许最多 2 次补抓（间隔 ≥ 7 天），不支持的端点 / 联赛进 `unsupported` 名单不再重试。
2. **日常增量**（并入现有 `apis` 晚班或独立 job）：对前一日完赛且已链接的比赛请求已启用端点；对 jc 在售比赛在开球前 60–30 分钟尝试 `fixtures/lineups` 一次（记录 `captured_before_kickoff`），定时不可靠时记录缺失原因不补造。
3. **归档**：原件与三张行表分片都写 Release 附件（压缩包 + SHA256 清单，见所有者决定 3、6）；数据分支只进索引、覆盖率、游标、审计与分片哈希清单；`restore_feeds` 对无行表的归档照常通过，`stats restore-rows` 按清单装回。
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
