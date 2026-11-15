# T68 新赛事存量合并、缺赛季阻断、身份创建入口与关联数口径

范围：所有者本轮四项要求；依据 `docs/reviews/2026-11-14-t68-review.md` 最后一节、
`docs/reviews/2026-11-15-t68-owner-approval.md`。从 main `ef08c66da7ce012be9e371ca9b1cb931dc69595d` 开始。
分支 `codex/T68-scoped-history-and-identity-approval`，仅草稿 PR，正式数据、每日开关和模型配置未改。

## 结论与限制

1. 获批新赛事的临时球队可以按范围合并，双方一起核对，再严格检查竞彩关联；旧 reconcile 路径未改。
   原件重放中的沙职286行、世界杯73行身份拒收复现成功，修复后该项拒收均为0。
   原359条拒收中，**349条恢复正常处理，10条转为如实待核的身份/同场冲突**；加原有2条，共剩12条。
   **这不等于所有球队已绑定、所有场次可训练，也不等于每日采集获准。**
2. `current_season` 未配置或不是有效整数时返回 `blocked/current_season_not_configured`；
   当前赛季分母是 null，不把全部历史冒充当前赛季。87条登记中75条缺赛季；现有 gate 只评估
   80条 verified/import 路线，其中69条缺赛季；另5条candidate和1条index_only缺赛季，
   原来就不在 gate 的 import 资格内，欧国联 nations 有赛季。没有修改这些资格。
3. 已提供 `identity_canonical plan/apply`，固定归档重新列出257个不同官方编号；全部保持 proposed。
   真实创建0条。审批入口只处理逐条经审查和所有者批准的决定，不把分类批准当球队创建批准。
4. **原归档单独是日职3/52、瑞超0/77。31/52、19/77需要额外的 FD 原件。**
   上一轮数字应连同辅助资料一起解释，不能当作只读8b900f4就能得到。

## 固定输入、时间与真实副本重放

原归档提交：`8b900f4cf0f9a1ca1afbafc9298e97b7cc7519f2`。
清单 SHA256：`e87b13785fdfa34deef4c449f892487f3fc9963304d97815bdd79c8652250cb6`。
原观测 UTC `2026-11-14T16:26:17.013740Z`；北京时间11月15日00:26:17。
开启检查固定截止 UTC `2026-11-14T16:30:00Z`，北京时间11月15日00:30。
实际执行时间另存 execution 记录，未倒填源文件或批准时间。

副本恢复6913球队、148190比赛、143877赛果，以及归档内的结果身份/关系和FD统计关联。
这是核心表离线副本；不声称已经恢复生产 Release 中全部技术数据或做了生产全量备份。
原始归档和生产数据只读，测试无体育HTTP请求。正式别名首批31条已在main，未增删或改写。

|2026固定原件|总行|修复前身份拒收|修复后|第二次|
|---|---:|---:|---|---|
|沙职307|306|286|279原样、12关联竞彩、3新增、12仍疑似冲突；身份拒收0|282原样、12已有竞彩关联、12仍疑似；新增/更新/比分修正0|
|世界杯1|104|73|104原样，身份拒收0|104原样；新增/更新/比分修正0|

12条关联对应12个不同竞彩ID，原有非空关系不得替换。沙职剩余12行的完整编号、双方、未获批编号和原因
在 `remaining-saudi-12.csv`，410行前后对照在 `historical-replay-410.csv`。
同一批剩余问题不能靠降低唯一性检查或拿比分挑候选绕过。3条新增只在副本，沿用显式3h历史回填规则并标assumed。

身份步骤共重定向2479个存量比赛ID（包括获批球队在范围内的早年赛季），重复合并0。
其他赛事比赛整行不变；全部原赛果、比分可知时间和非身份比赛字段不变；既有身份/关联证明表不变。
导出→恢复→再导出207个文件内容哈希一致。冻结预测和账本未写入或重算；未把“代码跑通”当作模型有效。

## 日职与瑞超的逐场差额

|赛事|只用固定归档|再加FD原件|FD额外验证|分母组成|
|---|---:|---:|---:|---|
|日职98|3/52|31/52|28|36条当前赛季已存竞彩＋16条原待核额外比赛|
|瑞超113|0/77|19/77|19|24＋53|

辅助目录66个文件（CSV及同名sidecar），不属于8b900f4的 manifest。
`coverage-additional-relations.csv` 列出47个新增验证关系的竞彩ID、FD ID、文件SHA、CSV行号、
实际捕获UTC及源URL。完整两个 gate 输出保留原始竞彩证据位置。
这些是**既有关系获得额外原件验证**，不是此次往正式库新增47场关联。

复现（先从固定提交提取归档，含清单已列的 `apif_suspected_duplicates.csv`）：

```bash
PYTHONPATH=src python -m zucai.identity_daily_gate --archive /path/to/8b900f4 \
  --cutoff 2026-11-14T16:30:00Z --codes 98,113 --out /tmp/archive-only.json
PYTHONPATH=src python -m zucai.identity_daily_gate --archive /path/to/8b900f4 \
  --cutoff 2026-11-14T16:30:00Z --codes 98,113 \
  --fd-raw-dir /path/to/evidence/auxiliary-fd --out /tmp/with-fd.json
```

## 审批与后续操作

创建计划绑定固定清单和官方原件数字证据。所有者逐条确认后，在专用决定文件补
`status=approved`、`approved_by=qw5563680`、实际 `approved_utc`、本仓库 `approval_pr` 和 `review_url`。
不直接编辑原件证据或批量把257条设为批准。类别/名称/已有编号冲突须先单独核对。
先在同一归档恢复副本上 apply：完整当前身份清单不符即拒绝；同一决定再次执行新增0。
创建前必须完整恢复回执引用的原件和sidecar，否则停止；原件、数字标识和创建回执随 feeds 导出恢复；数字别名本身没有回执不能授权。创建后仍需另外审查正式 APIF 绑定提案。

审查者复核后再决定正式副本修复、发布和每日开启，开发者本轮不执行这些操作。
若临时身份已有统计/伤停/身份快照附属引用，本入口保守阻断并记录原因，须专项核对，不默默改写附件。

## 实跑检查与可打开证据

实现版本：`16a033908233a95941c02c5e31424d289e71c579`。最终源码逐文件SHA与三份 execution 记录一致，提交前验证未改源码。

- 全量：**4597 passed**，449.37秒；Ruff check通过，414个文件format检查通过。9737条既有数值库弃用警告保留。
- 原始命令与日志：`verification/full-suite-release.log`、`release-real-replay.log`、`release-gates.log`，
  对应 `.execution.json` 保存实际UTC/北京时间、源码哈希和退出码0。
- 33个新增用例覆盖双临时身份、重复执行、范围内外共存、晚到竞彩、反向/多候选/已占用、
  附件身份引用阻断、审计失败事务回滚、混合缺赛季登记、创建审批/重名/重复编号、证据随导出恢复。
- 内部预审发现重复数字标识问题，3种攻击RED→GREEN；最终全量通过。正式Claude审查尚待PR。
- 首次全量因ENOSPC失败，完整日志保留；最终使用仅在成功teardown后清理单个tmp_path的外部插件，
  不改收集、断言或跳过用例。失败/共享临时目录不清理。

[完整证据目录](https://github.com/qw5563680/FOOTBALL/tree/3f79afd74c8e60f954f983b9a4b7607fba3f2ec0/acceptance/t68-scoped-history-20261115) · [验收压缩包](https://github.com/qw5563680/FOOTBALL/blob/3f79afd74c8e60f954f983b9a4b7607fba3f2ec0/acceptance/t68-scoped-history-20261115/t68-scoped-history-evidence.zip)

[410行重放对照](https://github.com/qw5563680/FOOTBALL/blob/3f79afd74c8e60f954f983b9a4b7607fba3f2ec0/acceptance/t68-scoped-history-20261115/historical-replay-410.csv) ·
[剩余12行](https://github.com/qw5563680/FOOTBALL/blob/3f79afd74c8e60f954f983b9a4b7607fba3f2ec0/acceptance/t68-scoped-history-20261115/remaining-saudi-12.csv) ·
[47场FD差额](https://github.com/qw5563680/FOOTBALL/blob/3f79afd74c8e60f954f983b9a4b7607fba3f2ec0/acceptance/t68-scoped-history-20261115/coverage-additional-relations.csv) ·
[257个待创建编号清单](https://github.com/qw5563680/FOOTBALL/blob/3f79afd74c8e60f954f983b9a4b7607fba3f2ec0/acceptance/t68-scoped-history-20261115/canonical-creation-queue.csv)

包SHA256：`2a91463651d367cd20dae34cc1595484b74a22eb1f847d2bc4a5db2e1d971719`，16533136字节，596项；解压后逐项SHA256SUMS.json，已检查无访问密钥。
沿用T68专用验收数据分支 `data/t68-scoped-history-acceptance-20261115`，没有向正式 `data/football-feeds` 推数据。

改动入口：`identity_daily_gate.py`、`sources/apif_scoped_reconcile.py`、`identity_canonical.py`、
`identity_binding_archive.py`，及相应测试和用法文档。只在离线副本影响所选新赛事；现有12份正式别名/模型/类别/工作流文件与main逐字节一致。
本PR不需要重跑正式模型，不回写历史预测；以后若用新数据计算，必须另存新批次。

仍未做：生产应用、真实257条批准创建、剩余12条关系处置、每日开启与连续三次线上验收；均交审查与所有者后续决定。
