# T66 审查修订验收（2026-11-14）

结论：两条必须修和两条建议已处理，离线验收通过；正式合并与线上发布、每日链路验收仍由审查者和所有者执行。

代码：`bf7c603` 实现修订，`4c924b806b3dd080210ef1d554f3bbacf6f5b9b6` 普通合入 main `6be4104`；后续文档提交不改受测源码。
[完整本轮证据](https://github.com/qw5563680/FOOTBALL/tree/data/t66-fd-import-export-acceptance-20261114/acceptance/2026-11-14-t66-review)；本目录 SHA256SUMS 相对该证据根目录。
固定数据 `e5848e5d7de83a0cd024fa3e03e08cf6db6c7ee4`，原件来自[失败运行43543648051](https://github.com/qw5563680/FOOTBALL/actions/runs/43543648051)。
附件ZIP SHA256：`ab410c4abdacfffc914d8833419a31c1d2ebbb22c4836ba79cbf61ae84da37e5`。
通过GitHub连接取得附件，只重放15份非空HTTP200原件；没有重新请求体育接口或使用正式密钥。
回归时钟固定UTC 2026-11-14 06:30（北京时间14:30），是离线回归设定，不是当时已发布预测。

## 逐条回应 PR #138

1. **必须修：晚到免费数据与APIF重复。** 导入只给唯一同场记录补APIF→FD指针，原球队、赛果、半场比分、采集/可知时间保留。两方规范ID相同，或APIF恰一方占位而另一方已绑定且同向ID相同，才可能通过；同时核验三小时窗口、双向唯一、全局供应商绑定和其他声明。未完赛竞争记录也参与检查。导入、导出恢复、训练读取共用这些验证。优先级仍为jc>fd>fdorg>apif；比分不作为身份依据，未新增比分取舍政策。
2. **必须修：行级partial导致重复采集、滚动七天漂移。** 全部已核验联赛文件完成即记成功，行级拒收/疑似重复保留partial警告但不阻止回执。整联赛/配置/空范围失败继续重试。按UTC周一开始的自然周采一次，周六成功不推迟下周一；回执版本及真实开始时刻保持。
3. **建议：工具计划文件。** 已删除 `docs/superpowers/plans/2026-11-14-t66-fd-import-export.md`。
4. **建议：Release增长。** 已核对[GitHub官方限制](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases#storage-and-bandwidth-quotas)：每Release最多1000附件，单文件小于2GiB。正常成功后的同周班次不再下载/复用FD原件，失败重试仍可能追加附件；没有声称解决APIF、伤停等其他附件的增长。

## 真实原件实跑

| 检查 | 结果 | 证据（相对完整证据目录） |
| --- | --- | --- |
| 原代码复现 | 同15文件新增95/关联610/拒收6/疑似141；仅欧战读取失败，首个冲突FIN apif:1638116 | real-acceptance/baseline/summary.json |
| 修复后首轮 | 新增95场，既有关联610，新增APIF→FD关系40条，原6拒收/141疑似不消失 | real-acceptance/fixed/summary.json |
| 重复导入 | 新增0，全部表逻辑哈希不变 | tables-repeat.json（fixed目录） |
| 导出恢复 | 导入版本与最终合并代码各自通过；所有表哈希相同 | real-acceptance/fixed/final-source-validation.json |
| 实际训练 | 最终合并代码19/19训练成功，参数/时窗调用与cloud_predict一致，DB哈希不变 | real-acceptance/fixed/training-summary.json、training/ |
| 原记录保护 | 37617条旧赛果、52条证明、51条既有fdorg指针、2615份冻结文件均保持 | old-result-changes.json、fd-proof-pointer-comparison.json、frozen-before/after.json（fixed目录） |
| 最新免费场次 | UTC2026-11-01 20:30推进到2026-11-09 18:00，仅验收副本 | real-acceptance/fixed/summary.json |
| 全量测试 | 合入main后3554项通过，0失败/错误/跳过；ruff check/format/diff通过 | verification/full-tests.xml、full-tests-summary.json、ruff-*.log |

40条关系包含34条双边ID一致（原审查所列），另6条符合单边占位规则：瑞超1、美职5。美职另2条API规范队与FD未映射原名不一致，保持未关联，不新增全局队名映射；7条FD未映射来源行均维持原导入行为。美职不属于当前欧战池；逐条ID/原球队/时刻在 `real-acceptance/fixed/linked-apif-list.csv` 与 `usa-partial-identity-evidence.json`。
旧Match行的正常导入变化另列明：40条APIF、43条FDORG、2条JC只补counterpart；1742条FD仅将最近检查时间更新为本批原件真实捕获时间，旧赛果可知时间不改。详见 `old-match-change-fields.json`。
15国内模型训练新增ID与95条新FD对应；完整逐联赛增减在 `training-id-deltas.json`。Euro改用已证明同场的高优先级代表，未证明的身份重叠仍拒绝。

导入使用单独冻结的修订源码；最终合并代码重新验证FD15完整配置、模型参数文件及19个模型的全部历史/截点内ID完全一致，再重新导出恢复并实际拟合。两阶段源码SHA分别留在 `inputs.json` 和 `final-source-validation.json`，未把旧快照冒充合并后的代码。

## 独立内部检查与限制

独立内部复核443项通过；修正并验证实际落库的10个边界探针在旧代码7失败/3通过、修复后全过。早期opaque探针存在ORM临时对象未持久化的问题，日志保留并标明，使用更正后的复现作证。详见 `internal-review/final-review.json`。这是Codex内部复核，不替代Claude正式审批。
初版9份11-04原件、52条统计关联验收仍保存在[原证据提交](https://github.com/qw5563680/FOOTBALL/tree/07585d0e1b811f4196ca56176502089f64c6cc92/acceptance/2026-11-14-t66-fd-import-export)，原备份未删除。本报告的周调度和15原件结论取代初版相应限制。

未修改正式数据、旧预测、日报、账本、模型参数、来源范围或投注开关；无需重跑旧预测。下一步由Claude复审，所有者合并；审查者执行正式FD补采发布及随后每日链路验收。离线19个模型跑通不表示预测有效。合并期限仍为UTC 2026-11-16 02:40（北京时间10:40）。
