# T23 · 球队身份对齐：apif 桥接联赛自动核验、别名审批清单与审计整理

## 背景
T22 验收（PR #31，2026-11-03）把欧罗巴 / 欧协联补到 2021–2026 六季、九联赛补到 2025–2026 后，球队对齐率仍然很低：
欧罗巴 6.0%（13 / 218）、欧协联 5.6%（22 / 396）；韩职、丹超、中超 0%；澳超 16.7%、芬超 40%。
直接后果：2026 季竞彩在售的 89 场欧战（欧罗巴 54、欧协联 35，含 AC Milan–Benfica、Leverkusen–Celje、Anderlecht–Lyon）
因 `jc 候选身份不全` 被整体隔离到 `state/audit/apif_suspected_duplicates.csv`，没有入库；九联赛另有 1496 行疑似重复出于同一原因。

根因不在数据量，而在证据结构。`state/audit/apif_cup_aliases.csv` 显示：欧战里**唯一**自动核验成功的是北欧俱乐部
（Malmö、Häcken、Bodø/Glimt、HJK……），证据类型是 `same_provider_id`——同一个 apif 球队 ID 已在瑞超 / 挪超 / 芬超的正式别名里核验过。
英、西、意、德、法、荷、葡的豪门（Milan 489、Benfica 211、Arsenal 42、Barcelona 529……）全部是 `proposed`，
原因「缺少完整且双向唯一的主客方向比赛证据」：欧战同一时刻有 6–9 场比赛，apif 侧没有任何已知身份可做消歧，
`infer_aliases` 的双向唯一图在这种稀疏证据下无法收敛。

API-Football 对同一俱乐部在所有赛事里使用同一个球队 ID。只要在 apif 侧把这些俱乐部在**本国联赛**里的 ID 核验一次
（本国联赛每轮比赛时间分散、jc 与 fd 覆盖完整，`infer_aliases` 的图可以收敛），欧战就能通过既有的 `same_provider_id`
桥接自动对齐，不需要任何按名字猜测。付费档下这只需要每联赛每赛季 1 次请求。

## 目标
1. 2026 季所有被竞彩在售的欧罗巴 / 欧协联比赛入库并链接到 jc；欧罗巴 / 欧协联在 jc 有比赛的球队对齐率 ≥ 90%。
2. 不能自动核验的球队，走一张所有者能看懂、一句话就能批准的别名审批清单，批准结果落到正式别名文件。
3. 疑似重复审计表不再每次运行重复追加同样的行。
4. 全程不引入「名字相似即绑定」的自动规则；自动绑定只来自比赛图证据或同一来源 ID 桥接。

## 范围
1. **apif 身份桥接联赛（identity-only）**（`data/api_football/leagues.json`、`src/zucai/sources/cup_identity.py`、
   `src/zucai/cloud_probe.py` 或新模块）：
   - `leagues.json` 新增 7 个联赛条目，标记 `identity_only: true`：英超 39、西甲 140、意甲 135、德甲 78、法甲 61、荷甲 88、葡超 94
     （联赛 ID 为「需验证」，先用 `operation=probe` 的 `/leagues?id=` 元数据确认）；这些条目**不参与**每日采集过滤、不参与 `backfill` 导入。
   - 新增审查者触发的 `operation=identity`（可作为 probe 的子模式）：每个桥接联赛取 2025、2026 两季 fixtures（一季一页），
     不入库，只调用既有 `apif_verify.infer_aliases`，以数据库里该联赛的 jc 与 fd（T08 已核验中文规范名）比赛为证据，
     产出 `auto_verified` 别名（含 apif 比赛 ID、对方比赛 ID、UTC 差、原件 SHA256）写入数据分支 `state/alias_candidates/apif.json`
     与 `state/audit/apif_identity.csv`；请求数、剩余配额进摘要。
   - 实现者把 Claude 运行产出的 `auto_verified` 条目（仅此状态）复制进 `data/team_aliases/api_football.json` 对应联赛，
     保留证据字段；审查者核对证据后合入。
   - `cup_identity` 的 `same_provider_id` 桥接把 identity-only 联赛纳入来源（现在只看九个采集联赛）。
2. **别名审批清单**（新目录 `docs/aliases/`）：
   - 新增命令 `zucai aliases proposals --provider apif --leagues 3,848,292,119,169 --out docs/aliases/<日期>-apif-proposed.md`，
     从数据分支的 `apif_cup_aliases.csv` / `alias_candidates` 读出仍为 `proposed` 的球队，按联赛分组输出 Markdown 表：
     apif ID、apif 名称、所在国、近两季出场数、**同一时间窗口内的 jc 候选中文名**（仅作参考列，明确标注「未核验」）、
     建议的中文规范名（留空由所有者填或确认）。清单只读数据分支与仓库文件，不发真实请求。
   - 批准方式：所有者在 PR 里对整表说「批准」或划掉个别行；实现者据此把条目写成 `status: approved`，
     附 `approved_by`、`approved_utc`、`approval_pr` 字段。`approved` 与 `auto_verified` 同样生效，这是既有规则。
   - 第一批清单覆盖：欧罗巴 / 欧协联在 2026 季 jc 有比赛但步骤 1 之后仍未对齐的球队；韩职、丹超、澳超、芬超、中超的 `proposed` 球队。
3. **重新回填与度量**：步骤 1、2 合入后，审查者重跑 `backfill --source apif --leagues 3,848 --from 2021 --to 2026` 与九联赛 2025–2026，
   摘要给出回填前后对齐率、2026 季被隔离场数（目标 ≤ 10）、新链接到 jc 的场数。
4. **审计整理**（`src/zucai/sources/apif_ingest.py` 写审计的位置或 `cloud_api_backfill`）：
   `apif_suspected_duplicates.csv`、`apif_rejected_rows.csv` 按 `(provider_match_id, raw_sha256, reason)` 去重后追加，
   或改为 `state/audit/runs/<run_id>/` 一份一文件并保留一份汇总；两种方案选一并写进 ARCHITECTURE。fd / fdorg 的审计表不改。
5. **放宽规则评估（只评估，不默认开启）**：在步骤 1、2 完成后的数据上，离线统计「同联赛、同 3 小时窗口内 jc 与 apif 唯一配对、
   且同一球队在两场不同比赛中一致」能额外解决多少球队；结论写进 PR 描述和 DATA_SOURCES。只有当额外解决 ≥ 20 支且零冲突时，
   才在下一张任务单实现；本任务不实现。
6. **文档**：`docs/DATA_SOURCES.md` 第 6 节补 identity-only 联赛、请求数与证据链；`README.md`「云端采集」补 `operation=identity`
   与审批清单用法；`docs/ARCHITECTURE.md` 补别名状态字段（`approved_by` 等）与审计文件布局；`docs/tasks/README.md` 加 T23。
7. **测试**（离线）：identity 模式只对 identity-only 联赛生效且不触发导入；桥接把 identity-only 联赛的别名用于欧战；
   审批清单生成（含 jc 参考列只读不绑定）；`approved` 条目生效与字段校验；审计去重；现有 1085 项不回退。

8. **存量独立身份合并（2026-11-03 补充，审查者 Claude 根据首轮真实回填结果加入）**
   （`src/zucai/sources/apif_ingest.py` 旁新模块，例如 `apif_reconcile.py`；`cloud_feeds` 加 `operation=reconcile` 或作为 `identity` 的子步骤）：
   - **为什么必须做**：PR #34 合入 130 条桥接别名后，首轮真实回填 run 41615438667（欧罗巴 / 欧协联 2021–2026）
     12 个范围全部 `partial`：拒绝 1215 行、新建 jc 关联 0、2026 季隔离 39，jc 同窗对齐率只有 44% / 33%。
     原因是 T20 / T22 回填时这些俱乐部没有别名，被按临时身份 `apif:team:<id>` 入库（库内 483 个临时身份，
     其中 63 个现已由桥接联赛核验，占欧罗巴 884 场 + 欧协联 462 场）；`_Teams.resolve` 的既有规则
     「在本联赛已有独立球队身份，须先核对存量比赛」让新别名对这些存量比赛完全无效。不合并存量身份，
     任何 `auto_verified` / `approved` 别名（包括第 2 项的所有者批准）都不会生效，目标 1 不可达。
   - **处理对象**：库里每个 `apif:team:<id>` 临时身份，在它有比赛的每个联赛 L 上，用与导入器完全相同的解析
     （`_canonical` + `resolve_cup_aliases` 的 `same_provider_id` 桥接）求规范名；只处理解析结果状态为
     `auto_verified` 或 `approved` 的 (L, id)。`proposed` 一律不碰，不按名字合并。
   - **前置检查**（任一不满足就跳过该 (L, id) 并写审计，不中止运行）：规范球队在库里恰好一条（按 name / aliases
     查找，与 `_Teams.one` 一致）；规范球队在联赛 L 内没有与该临时身份任何一场开球相差 3 小时以内的比赛
     （避免合并后同场两行）；规范球队没有另一个不同的 `apif:L:team:<其他 id>` 绑定；临时身份的 aliases 为空。
   - **动作**：把该临时身份在联赛 L 的 `matches.home_team_id / away_team_id` 改指规范球队；给规范球队 `aliases`
     追加 `apif:L:team:<id>`；临时身份在所有联赛都不再有比赛时删除该 `teams` 行。`match_id`、开球时间、状态、
     `results` 不变；`results / odds / closing_odds / predictions / sfc_issue_matches` 只引用 `match_id`，不改。
   - **审计与摘要**：`state/audit/apif_identity_merges.csv`（run_id、league、team_id、provisional_team_row_id、
     canonical、matches_rekeyed、alias_status、alias_source、reason），按 `(league, team_id, canonical)` 去重追加；
     feed manifest `collection.apif` 增加 `merged_teams`、`rekeyed_matches`、`skipped_conflicts`。
   - **幂等与归档**：同一归档连续执行两次，第二次合并 0；`export_feeds` 后 `restore_feeds` + `verify_forecasts`
     通过；`counts.matches` 不变，`counts.teams` 减少量等于删除的临时身份数。导入器规则本身不改，训练输入不变。
   - **顺序**：合并 → 重跑 `backfill` 3,848 与九联赛 → 再按验收标准计 2026 季隔离与 jc 对齐率。
     审查者已核对首轮数据：63 个可解析临时身份与规范球队之间 0 处 3 小时内同联赛冲突，规范球队全部唯一存在。
   - **同时注意**：首轮 89 场 2026 季在售欧战中，桥接别名能解析双方的只有 12 场，44 场缺一方、33 场双方都缺，
     共 46 支球队（Celtic 247、Beşiktaş 549、Plzen 567、Salzburg 571……）只能靠第 2 项的审批清单；
     所以第一批审批与本项合并都在关键路径上，批准后的条目同样经本项合并才能对存量比赛生效。

## 不做
- 不按名字相似度自动绑定；不把审批清单里的参考列当作证据。
- 不导入 identity-only 联赛的比赛，不把它们加入每日采集；不改 fd / fdorg / jc 的身份规则。
- 不训练模型、不改 `params.json`（T21）。
- 不在开发环境用正式 key 发真实请求（AGENTS 第 10 条）。
- 不改 `reserve=10` 与 6.1 秒间隔。
- 第 8 项只合并 apif 临时身份；fd 的英文临时队名、`fdorg:team:` 临时身份不在本任务处理（见 T24 及后续）。

## 验收标准
- 真实运行（审查者触发）：`probe` 确认 7 个桥接联赛 ID；`identity` 两季共 ≤ 14 次请求，每个桥接联赛 `auto_verified` ≥ 18 / 20 支球队；
  候选文件与审计表写入数据分支。
- 正式别名 PR：新增 `auto_verified` 条目每条带比赛证据；审批清单第一批合入，`approved` 条目带 `approved_by` / `approved_utc`。
- 真实运行（审查者触发）：`reconcile` 合并的临时身份数 ≥ 63（以运行时生效别名为准）、重定向约 1346 场、冲突跳过 0；
  随后回填的 `apif_rejected_rows.csv` 中「已有独立球队身份」原因新增 0 行。
- 重跑回填（在第 8 项合并与第一批审批落地之后计）：欧罗巴 / 欧协联 2026 季隔离场数 ≤ 10；jc 有比赛的欧战球队对齐率 ≥ 90%；同范围第二次回填新增 0。
- 离线测试覆盖第 8 项：幂等、冲突跳过、规范名缺失或多条跳过、同一临时身份跨两个联赛一次处理、删除无比赛的临时身份、`match_id` 不变。
- 审计表在连续两次相同回填后行数不变。
- `pytest -q` 全绿、`ruff check src tests examples` 与 `ruff format --check src tests` 通过；测试不联网。
