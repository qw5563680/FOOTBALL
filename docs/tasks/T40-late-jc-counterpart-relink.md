# T40 · 竞彩后到场次与已入库 API 赛程的关联补链（欧战模型失败，小修）

## 背景
2026-11-05 晚班 run 42005610679 的欧战联合模型（欧冠 + 欧罗巴 + 欧协联）训练失败，
`forecast_manifest.json` 记录原因为 `同联赛三小时内存在跨来源重复或待核对的身份重叠：apif:1636324 / jc:2042508`
（周三003 雷恩 VS 奥林匹亚科斯）。当晚欧冠、欧罗巴、欧协联全部「未预测：缺少同来源同联赛的模型」。
按数据分支 `state/matches.jsonl` 统计，三项杯赛里同联赛、同主客球队 ID、开球相差 ≤3 小时、来源不同且**互无 counterpart**
的比赛对共 **25 对**：欧罗巴 9 对（apif ↔ jc，周三003、周四002/004/005/008–012），欧冠 16 对（fdorg ↔ jc，周二004–011、周三004–011）。
2026-07 以来 113 场 jc 杯赛比赛中 85 场有 counterpart，28 场没有。

根因：杯赛场次在竞彩开售前，API-Football / football-data.org 的赛程已先以独立行入库。之后 jc 场次到达时，
`apif_ingest._link_target` 的 `existing and matches` 与 `fdorg_ingest._link_target` 的 `existing and matches and source == "jc"`
一律抛 `_Suspected("jc 候选身份不全、主客冲突或存在重复比赛")`，不再把已存在的 API 行和后到的 jc 行关联，于是两行永久并存。
昨日（11-04）同一模型训练成功，是因为这些 jc 行当时还没有赛果、不进训练集；11-05 晚班竞彩同步补回周二、周三赛果后，
重复对进入训练集，`predict/base.py::reject_cross_source_overlaps` 与 `predict/apif_guard.py` 的守卫按设计拒绝训练。
守卫本身是对的，不动；要修的是关联缺口。不修则每周新的杯赛场次结束后欧战模型持续失败，重复对持续累积。

## 范围
1. **入库时补链（防止再发生）。** `apif_ingest._link_target` 与 `fdorg_ingest._link_target`：当 `existing` 是该 API 来源自己的独立行
   （`existing.source` 为 apif / fdorg，`existing.counterpart_id` 为空，且没有任何比赛把它作为 counterpart），并且恰有 **1** 个精确 jc 候选
   （同联赛、同向同主客球队 ID、开球相差 ≤3 小时、`jc.counterpart_id` 为空、未被其他比赛声明）时，视为同一场：
   `jc.counterpart_id = key`，结果写法与现有「jc 先到、API 后到」的 `linked_existing` 路径一致，outcome 记为 `linked_late_jc`。
   任何其他情况（多候选、主客相反、任一侧已有不同链接、球队为 `apif:team:<id>` / `fdorg:team:<id>` 临时身份）保持现有 `_Suspected`，不猜。
2. **存量一次性补链。** 参照 `sources/fd_counterpart.relink_fdorg` 增加离线命令 `backfill --relink-jc`（不联网、不注入 key、只改 `counterpart_id`），
   遍历 `source in (apif, fdorg)` 且无 counterpart 的比赛行，按第 1 条同样的精确规则找唯一 jc 候选并链接；
   每对写一行到报告与 `state/audit/jc_relink.csv`（API ID、jc ID、联赛、开球、结果 linked / already_linked / 跳过原因与候选列表）。
   工作流新增 `operation=relink-jc`（复用 `relink-fdorg` 那一步的结构与 `if:` 门控），步骤摘要列出链接数、跳过数与原因分布。
3. **链接后的数据语义。** jc 行为主：赛果以 jc 官方赛果为准，API 行自己的 `Result` 不覆盖 jc；技术数据、伤停仍按 provider fixture id 存，
   特征按 T34a 既有方式经 counterpart 取用。训练：`predict/pooled.linked_representatives` 已按来源优先级排除低优先级一侧，
   验证欧战训练在补链后通过、未补链的重复对仍被守卫拒绝；如需调整守卫与去重的调用顺序，只能让「已链接对」通过，不能放宽对未链接对的拒绝。
4. 更新 `docs/ARCHITECTURE.md`（身份/关联一节加 `linked_late_jc` 与 `relink-jc`）、`docs/DATA_SOURCES.md`（记录本次 25 对与根因）、
   `README.md` 快速开始加 `relink-jc` 一行。

## 不做
- 不按球队名称、拼音或相似度推断关联；不动 `apif_cup_aliases.csv`、`reconcile`、别名审批流程。
- 不改 `reject_cross_source_overlaps` / `apif_guard` 对未链接重复对的拒绝语义；不改模型、params、P2、BET、T28。
- 不删除任何比赛行、赛果或技术数据；不改数据分支历史；不处理 28 − 25 = 3 场无 API 对应行的 jc 杯赛比赛。
- 不碰 T39 范围（日报、15:40 预测、`jc_skipped.csv`）。

## 测试（离线，只用 data/fixtures）
- 构造 SQLite：jc 行 + 同场独立 apif 行（无 counterpart）→ `relink-jc` 链接，报告 linked=1；再跑一次 already_linked，数据库字节不变。
- 主客相反、两个 jc 候选、jc 已链接到其他 apif ID、apif 行已被其他比赛声明、球队为 `apif:team:<id>` 临时身份 → 各自跳过并给出原因，不改库。
- fdorg ↔ jc 同上一组。
- 入库时：apif 游戏到达时「独立 apif 行 + 唯一 jc 候选」→ `linked_late_jc`，`jc.counterpart_id` 被设置；多候选仍 `_Suspected`。
- 欧战训练：用含一对重复的最小样本，补链前训练抛守卫错误，补链后训练通过且 `linked_rows_excluded` 含 API 行。
- `pytest -q`、`ruff check src tests`、`ruff format --check`、`actionlint` 全绿。

## 真实验收（审查者）
1. 合入后由 Claude 触发 `operation=relink-jc`，核对步骤摘要：链接数应为 25 左右（以当时数据为准），跳过项逐条有原因。
2. 下一档定时 predict：`forecast_manifest.json` 中 `欧战.status == "trained"`，Issue #39 的欧冠 / 欧罗巴 / 欧协联恢复预测。
3. 之后一周内新结束的杯赛场次不再出现未链接重复对（用本任务单「背景」里的统计方法复查）。

## 验收标准
- 25 对存量重复在不删行、不猜名的前提下完成关联；欧战模型恢复训练。
- 入库路径对「API 先到、jc 后到」给出 `linked_late_jc`，对一切歧义情形仍 `_Suspected`。
- 文档三处同步；测试只用离线样本。
