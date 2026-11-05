# T41 · 身份合并遇到同场竞彩比赛时合并并关联（第五批别名卡住，小修）

## 背景
2026-11-05 所有者批准第五批欧战别名（PR #82：欧罗巴 / 欧协联 651 费伦茨、628 布斯巴达、998 特拉布宗）。随后 `operation=reconcile`
run 42044312338 合并了历史出场（欧协联 651 重定向 14 场、欧罗巴 998 重定向 12 场、欧协联 628 重定向 18 场，另 7 支早前批准球队也合并），
但这三支球队**当前正要关联的那一场**全部跳过，`state/audit/apif_identity_merges.csv` 原因均为「同联赛三小时内规范身份已有比赛」：
- 欧罗巴 651：apif:1636208 AC米兰 VS `apif:team:651` ↔ jc:2042523 周四006 AC米兰 VS 费伦茨（2026-11-05T17:45Z）
- 欧罗巴 628：apif:1636337 `apif:team:628` VS 伯恩茅斯 ↔ jc:2042520 周四003 布斯巴达 VS 伯恩茅斯
- 欧协联 998：apif:1636476 `apif:team:998` VS 弗赖堡 ↔ jc:2042530 周四007 特拉布宗 VS 弗赖堡

根因是两条安全规则互相卡死：`sources/apif_reconcile.py` 第 241–247 行在重定向前检查「规范球队在同联赛 ±3 小时内已有比赛」就拒绝，
而那场「已有比赛」正是竞彩侧的同一场；T40 的 `relink-jc` 又因为 API 侧球队仍是临时身份而跳过。于是这三对永远既合并不了也关联不了。
后果：这三场出赛果后，`predict/apif_guard.py` 会把「临时身份 + 同窗 jc」判为重叠，欧战联合模型再次训练失败（下一轮杯赛 11-25 前须修好）。

## 范围
1. `apif_reconcile`（fdorg 同理）的三小时冲突检查改为：对每一场与规范球队冲突的既有比赛 `m`，若 **`m.source == "jc"`、同联赛、
   把临时球队换成规范球队后主客 ID 完全同向相同、开球相差 ≤3 小时、`m.counterpart_id` 为空且未被其他比赛声明、API 行自身无 counterpart、
   且这样的 jc 比赛恰好一场**，则视为同一场：照常重定向球队外键，并在同一事务里设置 `m.counterpart_id = <API match_id>`
   （与 T40 `linked_late_jc` 同一语义，jc 为主，API 行 Result 不覆盖 jc 赛果）。审计行 `reason` 记 `merged_and_linked`，
   另在 `state/audit/jc_relink.csv` 追加一行 `outcome=linked`、`reason=reconcile`。
   其余任何冲突（非 jc 来源、方向不同、多场、任一侧已有链接或声明）保持现有拒绝与原因不变。
2. 复用 `sources/jc_counterpart.late_jc_candidate` 的判定（可把「用规范球队替换临时球队后再判定」作为参数），不再写第二套规则。
3. 文档：`docs/ARCHITECTURE.md` reconcile 一节与 `docs/DATA_SOURCES.md` 第五批记录各补一段；README 不变。

## 不做
- 不放宽 reconcile 的其他冲突规则；不按名称推断；不删行；不改 `apif_guard` / `reject_cross_source_overlaps`；不改模型与 BET/P2/T28。
- 不处理 T40 未覆盖的 fd/fdorg 域（本单只在 reconcile 内部补这一条路径，fdorg 临时身份同样适用同一函数即可）。

## 测试（离线）
- 临时球队 + 规范球队的同场 jc 比赛（同向）→ 重定向 + counterpart 设置 + 两份审计；重复运行幂等、数据库字节不变。
- 主客反向 / 两场同窗 jc / jc 已有 counterpart / API 行已有 counterpart / 冲突比赛来源为 fd → 保持拒绝，原因不变，不改库。
- 用本单背景的三对构造最小样本，reconcile 后跑欧战守卫 `reject_cross_source_overlaps`（经 `linked_representatives` 去重）通过。
- `pytest -q`、`ruff check src tests`、`ruff format --check`、`actionlint` 全绿；只用 `data/fixtures/` 离线样本。

## 真实验收（审查者）
1. 合入后触发 `operation=reconcile`：审计中上述三对 `merged_and_linked`，`state/matches.jsonl` 里三场 apif 行球队 ID 变为 1137 / 1136 / 1138，
   对应 jc 行 `counterpart_id` 指向 apif ID；`apif:team:651/628/998` 三行不再被任何比赛引用。
2. 下一档 predict：`forecast_manifest.json` 中 `欧战.status == "trained"`。

## 验收标准
- 三对实际合并并关联；其他冲突拒绝行为与数量不变；文档同步；测试离线。
