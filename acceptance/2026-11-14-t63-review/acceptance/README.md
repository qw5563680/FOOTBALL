**T63 独立离线验收：通过。此为 Codex 内部验收，不是 Claude 审批。**

基线是 T66 最终修复副本与 e5848e5 原始归档。未改业务文件、未提交、未使用 key、HTTP/socket 均阻断。身份核验 `2026-11-14T06:29:00Z` 和训练截止 `2026-11-14T06:30:00Z` 都是明确的合成回归时钟，不代表历史上当时已经获得证明。

- `b821ae6-euro-red.log` 用 immutable `git archive b821ae6 src data` 和只读 T66 DB，复现 SWE `apif:1494327` / `fd:SWE:20261103:sirius:halmstad` 守卫失败。
- 新源码 reconcile 新增 403 条身份观察、165 条带时间关联；重复运行不新增。瑞超部分指针原样保留，未伪造双边身份或带时间关联。
- 原 T66 全部 14 张表不变，含 37,712 条 Results；其中 e584 原始 37,617 条 Results 逐字段保持。2,615 个冻结产物及 T66 原 DB、19 个原模型哈希保持。
- 最终 **19/19 实际 train 成功，0 失败**；欧战 20,608 行成功。与 T66 相比只新增日职 180 行、美职 17 行 APIF 可训练记录，其他 17 个模型的训练 ID 相同。
- 美职首轮真实语料失败完整保留。修复后仅暂缓 `apif:1490585`、`apif:1490596` 两行，并保留对应 FD；`source_identity_exclusions` 明确记录。两行没有生成 pointer/dated link，不称已关联。
- 旧七模型 404 个 engine 拟合参数，以及先验、校准、半衰期和训练字段精确同值。4,506 个真实历史有向球队对使用合成未来时刻，450,600 个矩阵值及 13,518 个 1X2 值逐值同值，最大差异 0。可适用真实未来目标为 **0**；另外两场 FDORG 目标不符合旧七来源限制，未修改来源。此为数值回归，不是历史预测或绩效实验。
- 用最新 live `football-t63/src` 完成最后 reconcile/check/export/restore；16 张表全行内容哈希一致。最新 restore 顺序改变了工作 DB 的物理 SHA，从训练时 `10b904c4c506ae8559c2475e4459d28ab9f6a0e5021f47e50178eab4ad49485c` 变为 `82c0e9d4eddd0bc4fc2549c506e0c3ea19d11f9649f2ebfd0570979a481322b3`。再次用 live 源码读全 19 个语料，训练 ID、来源计数、排除项与实际训练时全部一致。两个 SHA 不混称相同。
- 真实 e584 `jc:2042615` 小裁片逐字段校验后，以明确手构的 API1556134 输入分别执行 T66/T63：T66 不保留 API 独立行；T63 保留 API Match+Result；两者技术索引均为原 JC2042615，重复索引 unchanged，原 JC 和全部赛果不变。API 输入不是真实响应或权限/覆盖证据。

主要证据为 `SUMMARY.json`、`training-summary.json`、`old-seven-comparison.json`、`final-source-validation.json`、`final-restore-corpus-comparison.json`、`final-source-checks.json`、`swe-partial-proof.json`、`stats-t66.json`、`stats-t63.json`。最终兼容修复后：实际训练导入的 25 个模块中 24 个与当前 live 字节相同，唯一差异为 `source_result_quarantine.py` 的两处 `datetime.fromisoformat` 改成既有 `instant`，支持 session 中已是 datetime 的输入。严格 source diff 证明仅这两处调用与 imports 改动；原 T66 DB 和最新副本各 38,377 条真实 kickoff 的 datetime 值、ISO 和 UTC offset 全部相同，datetime 对象输入也等值。最新 live 再读 19 个训练 ID/来源/排除项全部与实际 fits 相同，因此保留原 19 个模型，不重复等价拟合。最终归档 38、最新语料 26、统计索引 41 个实际模块与 live 字节相同；参数文件一致。见 `final-normalization-compatibility.json`、`final-normalization-corpus-comparison.json` 和 `final-normalization-source.diff`。

模型及逐联赛 train JSON 在 `training/`；数值签名与压缩矩阵在 `signatures-t66/`、`signatures-t63/`。模型输入采用训练时物理 SHA，最终 DB 的表内容等值证据单独保留。所有本轮 runner、日志、JSON、模型和矩阵在 `SHA256SUMS` 中列出；快照源码哈希在 `inputs.json` / `training-source-snapshot.json`，工作 DB 原件校验在相应摘要中。

`acceptance.py`、`stats_acceptance.py`、`baseline_read_red.py` 是本轮独立脚本。`code-pre-mls-fix` 保留初始失败源码（早期 traceback 中的 `code-training` 目录后来更名）；`code-training` 保存实际 19 fits 的源码。`pre-latest-archive-*` 是最终归档验证器修复前的单独证据；不冒充最新验证。

`before-normalization-*` 保留该兼容修复之前的已完成检查记录。最终结论不声称实际拟合的 25 个模块全部与最终源码字节相同；一个 helper 的差异由逐值真实输入和 19 语料等价证据覆盖。
