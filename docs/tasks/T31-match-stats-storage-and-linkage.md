# T31 · 技术数据解析、存储、身份关联、归档与离线测试（阶段 B）

## 背景
见 `docs/research/2026-11-04-match-stats-program.md` 第 4、6、7 节。字段字典以 T30 实测结果定稿；fd CSV 的统计列不依赖 apif，可先做。

## 前置依赖
T30 真实探测结果回填（apif 部分）；T29 合入（身份合并与别名传播）。fd 统计解析不依赖 T30。

## 范围
1. **数据模型**（`src/zucai/models.py`、`docs/ARCHITECTURE.md` 数据模型节）：新增 `match_stats`、`match_events`、`match_lineups`、`apif_fixture_index` 四张附属表，字段按研究文档第 7 节；不改既有表；迁移脚本只建新表；`export_feeds / restore_feeds / verify` 覆盖新表（JSONL 分文件进 manifest）。
2. **fd 统计解析**（`sources/football_data.py`）：9 个主联赛季文件解析 HS/AS、HST/AST、HC/AC、HF/AF、HO/AO、HY/AY、HR/AR（`HFKC/AFKC` 替代犯规时单独标 `stat_key=free_kicks_conceded`）；缺列或空串写 null + `missing_reason=column_absent/empty`；写入 `match_stats`（provider=fd，附着到已有 fd `match_id`，`availability_basis=backfilled`，`available_utc=null`）。国别合并文件不含统计，不解析。
3. **apif 解析器**（新 `sources/apif_stats.py`）：statistics（type → 规范 `stat_key` 映射表，`Ball Possession "55%"` → 55.0 单位 %，`Passes %` 同；`expected_goals` 字符串 → float，缺失 null；`period_basis` 由 fixture 状态 FT/AET/PEN 判定，AET/PEN 标 `extra_time_included=true` 且禁止进 90 分钟特征）、events（type/detail/time.elapsed+extra/team/player/assist）、lineups（startXI/substitutes/coach/formation，`captured_before_kickoff = captured_utc < kickoff_utc`）、players（minutes、rating 等原值）。全部保留 `raw_value` 原文与 `value` 规范值。
4. **关联索引**：`apif_fixture_index` 建立规则——同联赛季、两队 apif ID 经正式别名解析为与库内一致的规范球队、开球 ±3 小时、一对一；否则写 `state/audit/stats_link_audit.csv`（reason：候选不全 / 多对一 / 主客反向 / 身份临时 / 同窗冲突），不新建比赛或球队。索引请求 `/fixtures?league&season` 仅限 7 个桥接联赛（欧冠待所有者批准范围变更后另开）。
5. **幂等与版本**：同 (provider_fixture_id, endpoint, raw_sha256) 不重复入库；新响应空值不覆盖已有有效值；数值变化写新 `version` 并保留旧行；全部在同一事务，失败回滚且审计一并回滚。
6. **文档**：ARCHITECTURE 数据模型与归档布局；DATA_SOURCES 字段字典（第 4 节定稿版）；README 新子命令（`zucai stats ingest-fd`、`zucai stats link`）。

## 不做
- 不联网回填（T32）；不改模型、特征、params（T33）；不改既有表与冻结文件；不处理赔率；不按名字相似度绑定。

## 失败处理
解析异常逐行拒绝写审计；索引冲突进待核验；事务失败整体回滚；原件缺失时不入库。

## 测试（离线）
字段解析与单位（百分比、字符串数值）、null 与 0 区分、FT/AET 时段标记、半场列独立、一对多与反向身份冲突、重复采集不重复入库、空值不覆盖、更正版本、空响应、errors 响应、导出 / 恢复 / verify 含新表、fd 列缺失、`captured_before_kickoff` 判定。

## 真实验收（审查者触发）
对数据分支快照只读运行 `stats ingest-fd`（从已归档或重新下载的 fd CSV，fd 为免费源）：9 联赛 2023/24–2026/27 的统计行数、null 比例、拒绝行；`stats link` 对 7 个桥接联赛 4 季建索引：链接数 / 待核验数；`restore_feeds + verify` 通过；重复运行字节不变。

## 完成标准
pytest / ruff / actionlint 通过；ARCHITECTURE、DATA_SOURCES、README 同步；`reports/` 只提交 README + SHA256SUMS（若有）。
