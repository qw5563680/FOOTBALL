# T38 · 伤停数据：入库、赛前可知标记、特征与离线实验

## 背景
所有者 2026-11-05 指出：现行 Dixon-Coles 只用历史赛果估计球队「平均实力」，历史赛果里已经混进了当时的伤停影响，
而对今天这场比赛却默认「全员可用」，没有用任何伤停信息。这个批评成立。模型只能通过近期比赛权重更高间接反映长期缺阵，
对突发的主力缺阵完全不知情。
T32 上线后（2026-11-03 起）`cloud_stats daily` 已每天归档 API-Football `injuries?league&season` 原件，覆盖
英超、西甲、意甲、德甲、法甲、荷甲、丹超、瑞超、挪超、美职、澳超 11 个联赛（日职、韩职、中超、葡超、欧战三项接口为空，
见 `docs/DATA_SOURCES.md`），但**只有原件和覆盖率，没有入库、没有特征、没有进模型**。本任务把这条链补齐，并用离线实验回答
「加入伤停后预测是否更准」。

## 前置依赖
T32（原件与游标）、T33（特征层与实验框架）、T34a（可知时间 `captured` 口径；本任务的 strict 特征依赖它，但数据表与回填可先做）。

## 目标
1. 伤停原件解析入库，逐条带抓取时间与「是否赛前已知」标记；历史季页回填只能标 `backfilled`，不冒充赛前已知。
2. 特征层给出每队每场赛前可知的缺阵 / 存疑人数与「上一场首发中的缺阵数」，缺失留 null。
3. 复用 T33 / T34b 评估框架做一轮离线实验，允许「无改善」。不改正式模型。

## 范围
1. **数据表 `match_injuries`**（新表，更新 `docs/ARCHITECTURE.md` 数据模型）：`provider`、`league`、`provider_season`、
   `provider_fixture_id`、`match_id`（经 `apif_fixture_index` 关联，关不上留 NULL 并进审计）、`team_id`、`provider_team_id`、
   `player_id`、`player_name`、`injury_type`（Missing Fixture / Questionable 原值）、`reason`、`fixture_kickoff_utc`、
   `captured_utc`、`available_utc`、`availability_basis`（captured / backfilled）、`captured_before_kickoff`、`raw_sha256`、
   `raw_location`、`parser_version`、`version`。按 PR #72 所有者决定：新增 last_captured_utc / record_key / content_sha256，首次 captured_utc 保留；唯一键 `(provider, provider_fixture_id, record_key, version)`；
   未变只延长末次捕获时间，完整回执压缩随 Release 发布，git CSV 每页仅计数与哈希；
   同一条目在后续抓取中变化（如 Questionable → Missing Fixture）作为新版本保留，不覆盖。
2. **解析**（`sources/apif_injuries.py`，T31 的 `Source` 约定；测试只用裁剪到几十 KB 的离线样本）：
   - 每日原件：条目的 `fixture.date` 晚于 `captured_utc` 的记为 `captured`、`available_utc=captured_utc`、
     `captured_before_kickoff=true`；`fixture.date` 早于抓取时间的条目（已踢完的比赛）记 `backfilled`、`available_utc` NULL。
   - 历史季页回填（`stats-backfill endpoints=injuries`，11 联赛 × 2022–2025，约 44 次请求，**所有者已批准，合入后由 Claude 执行**）：全部 `backfilled`。
   - 球员 ID 不做名字相似绑定；球队用 `apif_fixture_index` 既有身份；日职 / 韩职 / 中超 / 葡超 / 欧战接口为空的事实写进覆盖率，不虚构。
3. **特征层**（`features/injuries.py`，与 `features/stats.py` 同一套截点规则 `cutoff=min(as_of,kickoff)`）：每队每场
   - `missing_n`、`questionable_n`：截点前最新一次抓取中该队对本场的 Missing Fixture / Questionable 人数；
   - `missing_last_starters_n`：其中出现在该队上一场首发名单（`match_lineups`，若有）的人数，无阵容数据则 null；
   - 每项带 `availability_basis`、`captured_utc`、`n` 与缺失原因。本轮所有者明确沿用 T34a：strict 中回填行以真实
     `captured_utc` 作为 `effective_available_utc`，严格早于截点才可用，原 backfilled 标记保留；captured 行仍检查真实可知时刻。
     训练用 `backfill_replay`；目标分别输出 strict_capture_time 与 backfill_replay 独立表，后者以「最近归档名单在开球前已知」为**假设**，不得混算/互填。
4. **离线实验**（`predict/experiments/`，协议先冻结）：在 T34b 校准层框架上加伤停特征差（主候选仅 `missing_n` 差，`missing_last_starters_n` 差在有阵容子集另列附加分析），
   消融 = 不含伤停的同一校准层；时间切分、自助、六列对照、报告格式沿用 T33 / T34b；全量产物上 Release 资产。
   若 T34b 尚未合入，则以 T33 第一轮的 `dc-sot-v1` 为宿主做同样的加特征 / 消融比较，PR 里说明选择。
5. **摘要**：「技术数据覆盖率」节加伤停行（captured / backfilled 条数、关联成功率）；每日预测报告**暂不**展示伤停（等实验结论）。
6. **文档**：README 快速开始加子命令；ARCHITECTURE 数据模型与特征说明；DATA_SOURCES 伤停字段与空接口联赛。

## 不做
- 不改正式模型、不改 P2 / BET / T28；不把伤停特征直接接入每日预测（有改善另开任务单并经 T34a 并行机制前瞻验证）。
- 不抓球员个人统计、不估球员身价或重要度（第一轮只用人数与上一场首发交集）；不用名字相似度绑定球员或球队。
- 不用历史季页冒充赛前已知；不填 0 代替缺失。

## 测试（离线）
解析样本的 captured / backfilled 判定、版本保留、唯一键幂等；特征截点（本场抓取等于或晚于截点不进入）、strict 按真实捕获时刻；
缺阵名单与上一场首发交集；缺失 → null；实验协议切分不重叠；Release 上传走现有校验。

## 真实验收（审查者）
1. 历史回填 44 次请求后 `match_injuries` 行数、关联成功率、各联赛季条数与 DATA_SOURCES 探测记录量级一致。
2. 连续 7 天日常抓取后，`captured_before_kickoff=true` 的条目覆盖当周在售比赛的比例。
3. 在同一数据分支快照上独立复算实验，配对差与区间一致。

## 完成标准
pytest / ruff 通过；PR 描述给出全部指标表与结论（含「无改善」）；审查记录归档。
