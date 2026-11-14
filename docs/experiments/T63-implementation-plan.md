# T63 Implementation Plan

本文件保留第一阶段实现计划。所有者在 PR136 修订版本并行及标签要求后的工作，按
[后续计划](../superpowers/plans/2026-11-14-t63-versioned-parallel.md) 执行；
下述原15联赛统一失效及旧 gate 重产描述不再作为当前资格规则。

**Goal:** 15 个既有国内联赛接入 APIF，九个接入 FDORG；在真实可知截点后使用，保留既有优先级。

**Architecture:** 复用 T60 显式训练来源清单；训练和逐折评价延迟到截点内再择源。
T63 APIF 同场也保存自身比分，独立审计冲突；原比赛、原可知时间和冻结区不回写。
T61 目录/权限通道仍负责核准采集范围，本分支不自动授予未实测赛季权限。

**Tech Stack:** 现有 Python、pandas、SQLAlchemy、SQLite、pytest、ruff；不增加依赖。

**Spec:** `docs/tasks/T63-apif-results-for-existing-15-leagues.md` 与
`docs/experiments/T63-source-latency-protocol.md`。

## Global Constraints

- 根任务已指定本人在独占 worktree 执行，负责人 commit/push；本实现者不提交或发布。
- fixture 测试零体育 HTTP、零正式 key；原归档只读。
- Claude 审协议前不执行真实收益实验；统计库存和 fixture 允许。
- 来源顺序 `jc > fd > fdorg > apif`、模型数值参数、gate 与 BET 禁用不改。
- 报告只提交 README/SHA256SUMS，合计 ≤200 KB；完整证据在 `/workspace/work/t63-evidence`。

## Review Focus

- 后到的高优先级结果不得从更早截点提前排除付费行（任务 2）。
- 旧 assumed 时间不可当作真实入库，最新观察只可作为保守证据（任务 2）。
- 未核准身份、断链、错主客和多候选不能绕过重复守卫（任务 2/3）。
- 比分冲突不得改变旧训练口径，柏林联合—波鸿单列（任务 2/4）。
- 旧 P2 evidence 不可在仅 sources 改变后继续认证（任务 1/5）。

## Tasks

1. **显式来源与证据失效**
   - [x] 先添加 policy/params 回归测试，验证 15/9 集合、旧数值与 gate 不变、旧证据失效。
   - [x] 扩展 `predict/training_sources.py` 与 `data/model/apif_history_training.json`。
   - [x] 更新 `data/model/params.json` 来源与 P2 状态，保留原选参值与历史窗口。

2. **截点内选择**
   - [x] 以 `data/fixtures/t63/` 合成时序复现先全局去重造成的提前排除。
   - [x] 为 `predict/backtest.py` 保留延迟择源输入，为 `predict/pooled.py` 提供
     `select_training_history(frame, as_of)`；`DixonColes.fit` 与逐折评估共用。
   - [x] 在新来源加入范围内拒绝未知身份，assumed 使用独立实际观察界或排除。
   - [x] 验证链式 counterpart、等截点、后到替换、冲突沿旧口径、旧双源不变。

3. **独立 APIF 结果与链接**
   - [x] 用既有 T10 fixture 添加与 fd/jc 同场仍存 APIF 自身比分测试。
   - [x] 仅 T63 国内范围允许独立 provider 行并建立严格可核验链接。
   - [x] T63 新导入结果使用真实 capture；既有结果/时间原样保存。
   - [x] 验证重复导入、迟到 fd/jc、原数据不变、冲突与多候选拒绝。

4. **每日冲突审计与采集边界**
   - [x] 新模块导出 APIF 对旧源比分/开球逐场审计 CSV，加入现有归档白名单。
   - [x] `cloud_feeds` 每日采集后调用审计；再入库仍保留证据，不自动选胜方。
   - [x] 测试导出/恢复与审计列、未知身份、判罚特殊行。
   - [x] 文档列七个 import 转换与三个新增映射的目录/权限缺口，由 reviewer 补证并回填。

5. **P2 重产与验收文档**
   - [x] 给出现行 gate 的独立参数副本重产命令，不在每日训练自动沿用旧证据。
   - [x] 更新 README、ARCHITECTURE、DATA_SOURCES 与 reports 摘要。
   - [ ] 固定归档只读库存已完成；独立 main/current 拟合和规范化副本重核验由负责人运行，原归档/冻结区哈希待汇总；不提前运行真实 LL。
   - [x] 定向测试后完整 pytest：3419 passed；`ruff check src tests` 与 `ruff format --check src tests` 通过。
