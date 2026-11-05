# 欧战身份审批清单（第五批 · 2026-11-05 晚班发现的三支临时身份球队）

> **所有者批准（2026-11-05，北京 22:15 左右，聊天中回复「同意」）**：三支球队按下表「竞彩规范名」绑定，
> 欧罗巴与欧协联两个范围各记一条，共 6 条写入 `data/team_aliases/api_football.json`，`approval_pr` 指向本 PR。

## 背景

T40 补链（run 42024621929）把 38 对 API/竞彩重复比赛关联后，剩 3 对因 API 侧球队仍为 `apif:team:<id>` 临时身份被跳过
（`state/audit/jc_relink.csv`，原因「API 球队仍为临时身份」）。它们的竞彩侧球队名 费伦茨 / 布斯巴达 / 特拉布宗 已在
`state/audit/jc_unmatched_teams.csv`（2026-11-05T10:43Z 首次出现），API 侧 651 / 628 / 998 在 `state/audit/apif_cup_aliases.csv`
为 `proposed`（「缺少完整且双向唯一的主客方向比赛证据」，即库内只有一场同窗比赛，自动核验不够）。
这 3 场出赛果后会被 `predict/apif_guard.py` 当作「临时身份与 jc 同窗重叠」再次拒绝欧战训练，故由所有者人工批准。

## 批准行

| 范围 | apif ID | apif 名称 | 竞彩规范名 | 证据（同联赛、同开球、同对手的 API / 竞彩比赛对） |
| --- | --- | --- | --- | --- |
| 欧罗巴 | 651 | Ferencvarosi TC | 费伦茨 | apif:1636208 AC米兰 VS `apif:team:651` ↔ jc:2042523 周四006 AC米兰 VS 费伦茨，2026-11-05T17:45Z |
| 欧罗巴 | 628 | Sparta Praha | 布斯巴达 | apif:1636337 `apif:team:628` VS 伯恩茅斯 ↔ jc:2042520 周四003 布斯巴达 VS 伯恩茅斯，2026-11-05T17:45Z |
| 欧协联 | 998 | Trabzonspor | 特拉布宗 | apif:1636476 `apif:team:998` VS 弗赖堡 ↔ jc:2042530 周四007 特拉布宗 VS 弗赖堡，2026-11-05T17:45Z |
| 欧协联 | 651 | Ferencvarosi TC | 费伦茨 | 同一俱乐部 ID 在欧协联范围的历史出场（第二批清单：欧协联 14 场），沿用上行规范名 |
| 欧协联 | 628 | Sparta Praha | 布斯巴达 | 同一俱乐部 ID 跨范围沿用，与 `apif_cup_aliases.csv` 两范围均列 proposed 一致 |
| 欧罗巴 | 998 | Trabzonspor | 特拉布宗 | 同一俱乐部 ID 跨范围沿用，同上 |

竞彩规范名均为库内已存在的 jc 球队行（费伦茨 id 1137、布斯巴达 id 1136、特拉布宗 id 1138），不是新造名字；
名称对应无歧义（费伦茨＝匈牙利 Ferencvárosi TC，布斯巴达＝布拉格斯巴达 Sparta Praha，特拉布宗＝Trabzonspor）。
第二批清单（`2026-11-04-apif-cups-history.md`）里 651 Ferencvarosi TC 当时建议名留空、未批，本批补上。

## 合入后步骤（审查者）

1. `operation=reconcile`：把 `apif:team:651/628/998` 三个临时球队行合并到对应竞彩球队（只重定向球队外键，比赛 ID 与原件不变）。
2. `operation=relink-jc`：合并后 3 对比赛球队 ID 一致，补上 counterpart（预期链接 3、跳过 0）。
3. 下一档 predict：`forecast_manifest.json` 中 `欧战.status == "trained"`，且这 3 场不再出现 96% / 94% / 88% 这类「不认识的球队」极端概率。
