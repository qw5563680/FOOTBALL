# T29 · 欧战常客球队的历史身份合并：第二批别名审批清单与同源 ID 跨联赛生效

## 背景
T21 合入后首个真实预测（run 41699931511 / 41710899834）暴露了欧战模型当前最大的质量瓶颈：**同一俱乐部的历史被拆成两个身份**。
竞彩在售的欧冠球队以 jc 中文规范名入库（费内巴切、斯拉维亚、LASK林茨、布拉迪斯、萨巴赫、雅典AEK……），
但它们 2021–2026 的欧罗巴 / 欧协联历史仍挂在 API-Football 的临时身份 `apif:team:<id>` 下，模型把它们当成只有 3–9 场记录的近乎未知球队：

| jc 规范名 | 库内比赛数 | apif 临时身份 | 临时身份下的欧战比赛 |
| --- | ---: | --- | ---: |
| 费内巴切 | 9 | `apif:team:611` Fenerbahçe | 60（欧罗巴 42、欧协联 18） |
| 斯拉维亚 | 17 | `apif:team:560` Slavia Praha | 46 |
| 布拉迪斯 | 17 | `apif:team:656` Slovan Bratislava | 40 |
| LASK林茨 | 9 | `apif:team:1026` Lask Linz | 28 |
| 雅典AEK | 7 | `apif:team:575` AEK Athens FC | 28 |
| 加拉塔萨 | 27 | `apif:team:645` Galatasaray | 24 |
| 布鲁日 | 31 | `apif:team:569` Club Brugge KV | 18 |
| 萨巴赫 | 9 | `apif:team:13976` Sabah FA | 14 |

直接后果（2026-11-03 数据分支 `e05c7b0`）：费内巴切–利物浦 模型 2 / 5 / 94 对 jc 去水 16 / 20 / 64；LASK–布拉迪斯 95 / 3 / 2 对 73 / 15 / 12。
15 场在售欧冠里 5 场与市场总变差 ≥ 0.20，全部涉及这类球队。

基线（数据分支 `e05c7b0`，2025-07 起 jc 在售的 72 支欧战球队）：库内比赛 < 20 场的 **8 支**，20–59 场的 **15 支**；
其中 **15 支**没有任何 apif 别名。库内 apif 临时身份 378 个（≥ 10 场的 213 个，共 6764 次出场），fdorg 临时身份 17 个（欧冠 175 次出场）。

T23 第一批审批清单（164 行）里欧战球队只批了少数，原因是「jc 候选中文名」一列把同一三小时窗口内的 20 多场比赛全部列出，
所有者无法据此确认。已批准的（凯尔特人、萨尔茨堡、贝西克塔、安德莱……）经 `reconcile` 后临时身份已清零，证明「批准 → 合并」链路可用。
另外 `apif:team:400` FC Copenhagen 在欧战已 `auto_verified`，但同一 ID 在丹超的 55 场仍是临时身份：同源 ID 证据没有跨联赛生效。

## 目标
1. 上表 8 支及其他「现为 jc 规范球队、历史仍在临时身份」的欧战常客，历史合并进规范球队。
2. 审批清单改成所有者 1–2 分钟能批完的形式：每行一个候选、带可核对的参考信息、不再列二十多场同窗比赛。
3. 同一 apif ID 在任一联赛被 `auto_verified` / `approved`，`reconcile` 对该 ID 在所有联赛的临时身份都生效（同源 ID 是既有允许的证据类型）。
4. 全程不引入「名字相似即绑定」的自动规则：自动合并只来自同源 ID；跨源的中文名绑定一律由所有者逐行批准。

## 范围
1. **第二批审批清单（`zucai aliases proposals` 新模式 `--scope cups-history`）**，输出 `docs/aliases/<日期>-apif-cups-history.md`：
   - 行集合：数据分支中仍为临时身份、在欧罗巴 / 欧协联出场 ≥ 10 的 apif ID，按出场数降序；名称取 `state/audit/apif_cup_aliases.csv` 的 apif 原名。
   - 列：apif ID、apif 名称、出场赛季范围、欧罗巴 / 欧协联出场数、**已绑定对手示例**（该 ID 近两季对阵过的、已有规范名的球队，最多 3 个，仅供核对）、建议中文规范名（留空）。
   - 清单末尾附「参考：当前 jc 在售欧战球队中没有 apif 别名的球队」一节：中文名、库内比赛数、已有 fdorg 别名；明确标注仅供所有者对照，不是绑定证据。
   - 不再输出同窗 jc 比赛列表；不自动填任何中文名；只读归档与仓库文件，不联网，不覆盖已有文件。
2. **审批落地**：所有者在 PR 评论里按「apif ID → 中文规范名」逐行批准（可只批需要的行）。实现者把批准行写入 `data/team_aliases/api_football.json`
   对应欧战联赛键，`status=approved`，附 `approved_by / approved_utc / approval_pr`（规则同 T23）；未批准、无中文名的行保持 `proposed`。
3. **同源 ID 跨联赛生效（`src/zucai/sources/apif_reconcile.py`）**：对库内每个 `apif:team:<id>` 临时身份，若该 ID 在**任一**联赛的正式别名文件里为
   `auto_verified` / `approved`（含桥接联赛与欧战联赛），则在它有比赛的每个联赛按 T23 第 8 项的前置检查与动作合并，`alias_source` 记录来源联赛与 `same_provider_id`。
   前置检查（规范球队唯一、3 小时内无同联赛冲突、无另一 apif 绑定、临时身份 aliases 为空）全部沿用，任一不满足仍跳过并写审计。
4. **fdorg 临时身份**：`reconcile` 同样处理 `fdorg:team:<id>`（17 个），依据 `data/team_aliases/football_data_org.json` 的 `auto_verified` / `approved`；
   没有别名的保持不动。如需所有者批准，在同一清单附「fdorg」一节（名称取 `fdorg_unmatched_teams.csv`）。
5. **度量**（`reconcile` 摘要与 `aliases proposals` 输出都要给出）：jc 在售欧战球队中库内比赛 < 20 / < 60 场的数量；apif / fdorg 临时身份数与欧战出场数；本次合并的身份数、重定向场数、冲突跳过数。
6. **测试**（离线，只用 `data/fixtures/`）：清单生成（行集合、排序、对手示例只取已有规范名、参考节、不写中文名、不覆盖）；
   跨联赛生效（欧战 approved 的 ID 合并其丹超临时身份；仅 `proposed` 不合并；冲突跳过）；fdorg 临时身份合并与跳过；幂等（第二次合并 0、`match_id` 不变、`counts.matches` 不变）。
7. **文档**：`docs/aliases/README.md` 补第二批流程；`docs/ARCHITECTURE.md` 身份一节补「同源 ID 跨联赛生效」与 fdorg 合并；`README.md` 补 `--scope cups-history` 用法；`docs/tasks/README.md` 加 T29。

## 不做
- 不按名字相似度自动绑定；清单的「已绑定对手示例」和参考节只供人读，不进入任何判定。
- 不新增 apif 欧冠（ID 2）采集；T20 已决定欧冠只用 fdorg，如要讨论另开任务单。
- 不改导入器的隔离规则、不改 `_Teams.resolve`、不改训练与模型代码、不改 `params.json`。
- 不在开发环境用正式 key 发真实请求；`reconcile` 本身不联网。
- 不处理 fd（football-data.co.uk）的英文临时队名。

## 验收标准
- 清单 PR：`docs/aliases/<日期>-apif-cups-history.md` 生成自数据分支快照并写明 manifest SHA256；上表 8 支球队的 apif ID 全部在列且各只占一行；没有同窗比赛列表。
- 审批：所有者在 PR 评论逐行批准后，正式别名文件新增 `approved` 条目三项审批字段齐全；`proposed` 行不改。
- 真实运行（审查者触发）`operation=reconcile`：合并的临时身份数 ≥ 批准行数（含跨联赛生效的丹超等历史）、冲突跳过 0；重复运行合并 0；
  `restore_feeds` + `verify_forecasts` 通过，`counts.matches` 不变。
- 合并后度量：jc 在售欧战球队库内比赛 < 20 场的从 8 支降到 **0**，< 60 场的从 15 支降到 **≤ 6**；费内巴切、斯拉维亚、布拉迪斯、LASK林茨、雅典AEK 各 ≥ 30 场。
- 下一次定时预测中，上述球队的 `home_training_leagues / away_training_leagues` 含欧罗巴或欧协联场数；训练总场数不减少。
- `pytest -q` 全绿、`ruff check src tests examples` 与 `ruff format --check src tests` 通过；测试不联网；`reports/` 不新增。
