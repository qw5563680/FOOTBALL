# T24 · fd 与 fdorg 同场互链、审计表去重（小修）

## 背景
T20 验收（PR #30，run 41543199767）发现 fdorg 每日采集的「疑似重复」从 3 跳到 27，之后每次运行都重复出现同一批行。
定位（见 `docs/reviews/2026-11-02-t20-review.md`）：fdorg 以赛程形式提前入库（窗口到今天 +4 天），football-data.co.uk 的赛果
晚几天才到；`fd_ingest` 导入时只会链接 `source=jc` 的比赛，不认识已有的 fdorg 行，于是同一场比赛在库里有 fd、fdorg 两行；
下一次 fdorg 采集在 `fdorg_ingest._link_target` 走到 `existing and matches` 分支，把「已有 fdorg 行 + 新出现的同场 fd 行」判成
`fd 候选身份不全、主客冲突或存在重复比赛`，写入 `state/audit/fdorg_suspected_duplicates.csv`，并且**每次运行都再写一遍**。
受影响的是竞彩没有开售的国内联赛场次（英冠、荷甲、葡超居多）。训练只用 jc + fd，模型不受影响；但两行长期不互链，
fdorg 的赛果和状态更新对这些场次失效，审计表也被噪音淹没（三天内从 11 行涨到 44 行）。

## 目标
1. 同一场比赛在 fd 与 fdorg 都出现时，两行互链一次并保持 jc > fd > fdorg 的优先级，不再被判为疑似重复。
2. fdorg / fd 的审计 CSV 在连续两次相同采集后行数不变。
3. 不改训练输入、不改比分可知时间规则、不改 jc 的任何处理。

## 范围
1. **fd 导入时认识 fdorg**（`src/zucai/sources/fd_ingest.py`）：导入 fd 行时，除 jc 外也在同联赛 ±3 小时窗口内查找 `source=fdorg`、
   主客球队 ID 完全一致、尚无 fd 侧 counterpart 的比赛；命中唯一候选时把 fdorg 行的 `counterpart_id` 指向该 fd `match_id`（fd 为高优先级），
   fd 行照常插入；多个候选或主客相反仍走现有疑似重复规则。赛果以 fd 为准，不覆盖 jc。
2. **fdorg 重采时识别已链接的 fd**（`src/zucai/sources/fdorg_ingest.py` `_link_target`）：`existing and matches` 的分支改为——
   若 `existing.counterpart_id` 已指向该 fd 候选，或该 fd 候选是唯一精确匹配且 `existing` 没有其他链接，则建立/确认链接并返回
   `linked_fd`，只在真正冲突（多个候选、主客相反、已有不同链接）时才判疑似。fdorg 不得改写 fd 已有比分。
3. **审计去重**（`fdorg_ingest` / `fd_ingest` 写审计处或 `cloud_apis`）：`fdorg_suspected_duplicates.csv`、`fd_suspected_duplicates.csv`、
   两个 `*_rejected_rows.csv` 按 `(provider_match_id 或 fd 行键, raw_sha256, reason)` 去重后再追加；保留既有历史行。
   apif 的审计表由 T23 处理，本任务不碰。
4. **一次性清理**：提供 `cloud_feeds` 子命令或 backfill 选项 `--relink-fdorg`，对库里已有的「同联赛 ±3 小时、主客一致、互不链接」的 fd/fdorg 对
   补上链接并输出清单；只允许在 Actions 手动触发，摘要列补链数、跳过数与原因。
5. **文档与测试**：`docs/ARCHITECTURE.md` 的去重/优先级一节补「fd 后到时与 fdorg 互链」的规则；
   离线测试覆盖：fd 后到互链、fdorg 重采不再判疑似、多候选仍判疑似、审计去重、一次性清理幂等；现有测试不回退。

## 不做
- 不改 jc 的身份、链接或赛果规则；不改比分可知时间（`available_utc`）算法；不改训练数据选择。
- 不处理 apif 的审计表与身份（T23）。
- 不按名字相似度匹配；只用已核验的球队 ID。

## 验收标准
- 真实运行（审查者触发）：`--relink-fdorg` 一次性清理给出补链数（预期约等于当前重复出现的 27 行对应的场次）；
  之后两次 `operation=apis` 的 fdorg 疑似重复回到个位数且两次审计 CSV 行数相同。
- 数据分支 `matches` 行数不变（只补 counterpart，不删行）；`verify` 通过；冻结预测字节不变。
- `pytest -q` 全绿、`ruff check src tests examples` 与 `ruff format --check src tests` 通过；测试不联网。
