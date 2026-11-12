# T54 七支球队来源核对

固定数据快照：`b492f4d7c501fd8cdbd2694d760f2f13892f796c`。本调查只读归档与仓库；没有联网、改名或合并。

七条处置均记为 `pending`：这表示规范身份及新队/重复队归属待核。1153 的源英文真名 **Russia** 已由 APIF 球队 ID 4 精确证实，不是名称推测。

| ID | 快照名 | 创建记录 UTC | 来源及场次 | 结论 |
| --- | --- | --- | --- | --- |
| 1153 | apif:team:4 | 2026-11-08T22:09:22.079755Z | APIF 欧国联 team ID 4，16 场历史比赛；源名 Russia | pending |
| 1154 | 布罗姆利 | 2026-11-11T15:31:38.626037Z | JC jc:2042685 | pending |
| 1155 | 伯顿 | 2026-11-11T15:31:38.675407Z | JC jc:2042684 | pending |
| 1157 | 巴恩斯利 | 2026-11-12T03:08:30.268113Z | JC jc:2042691 | pending |
| 1158 | 奥德汉姆 | 2026-11-12T03:08:30.291079Z | JC jc:2042691 | pending |
| 1159 | 谢周三 | 2026-11-12T03:08:30.307103Z | JC jc:2042690 | pending |
| 1160 | 阿克宁顿 | 2026-11-12T03:08:30.338783Z | JC jc:2042690 | pending |

## 来源与保留意见

- 1153：`state/audit/apif_unmatched_teams.csv:420` 原文为 `欧国联,4,Russia,2026-11-08T22:09:22.079755Z`。源名已核验；正式国家队注册表没有 APIF ID 4，规范中文名、跨源归并仍待核。
- 1154、1155：`state/audit/jc_unmatched_teams.csv:464-465`；新归档记录分别是 `jc:2042685` 与 `jc:2042684` 的主队。没有显式同队映射证据。
- 1157—1160：`state/audit/jc_unmatched_teams.csv:467-470`；属于 `jc:2042691` 和 `jc:2042690`。之前的 catalog 行均 `sellStatus=2, retained=0`。
- 1157 的人工复核线索：独立 FD 队 `551/Barnsley`，46 场历史英冠比赛。1159 的线索：独立 FD 队 `562/Sheffield Weds`，138 场历史英冠比赛。没有获批别名、共同球队源 ID 或同场桥接，不能按译名合并。
- 1154、1155、1158、1160 的资料也仅能证明 JC 原名与对应比赛。未发现映射不等于证明它们是全新的足球实体。

## “同批新建”说法的修正

- 1153：首次加入 `state/teams.jsonl` 的提交 `9a0ac9e7ce7d6617024d5085e057985c3db62929`，提交时间 `2026-11-08T22:09:41Z`。
- 1154、1155：首次加入 `state/teams.jsonl` 的提交 `0f7ee8cf7de79520a4cbf7f14144cfdbc2ef4554`，提交时间 `2026-11-11T23:31:50+08:00`。
- 1157—1160：首次加入 `state/teams.jsonl` 的提交 `e24b5b4764171f3b67578aa06cbb7537c52e2eae`，提交时间 `2026-11-12T11:08:41+08:00`。

六支 JC 俱乐部首次归档的对应比赛均已 `finished` 且 `result_provider=jc`。因此不应将它们统一描述为 11 月 12 日开售采集新队。

## 可复查证据

`team-investigation.json` 包含恰好七条登记、逐条原始行/行号、创建提交、源文件哈希及未决理由。所有行号基于该固定快照；CSV 行号包括表头。

```text
9bba459fb1eb4c0f97c44e519c737f9cd6f23c4a49e226c6369029bc5eba8ca7  state/teams.jsonl
36df6178a15e22a2d301a87fd72e0b481bdb9c89ad687c32d87f96c37821395e  state/matches.jsonl
7c14aecf81586094275ce777bd8ecfd86c0fdb1b2b05cf33f496d06ce8a7e0b8  state/audit/apif_unmatched_teams.csv
5bfd85ee5a1e229185dd95f2bbaf3899722079bd338a65a5e8eaec140e80680b  state/audit/jc_unmatched_teams.csv
01ba83bd977dcdf1442315ad8566afce1b7d903abca7b78071c2a99fb9a971ed  state/audit/fd_unmatched_teams.csv
49dfeb4b8632097904b377bf93338451a502b21fc7705ed33030f1b245db2d9b  state/audit/jc_catalog/2026-11.csv
d2112dbb2b5a41cb4ca62155f33002d956070354d44e97e299c2cba75045bcc8  state/nations_league.json
```
