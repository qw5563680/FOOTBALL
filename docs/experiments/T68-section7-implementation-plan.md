# T68 A 段第七节 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. 内部复查不替代 Claude 正式审查。

**Goal:** 完成 #141 未验收的提案读取、每日开启检查、跨日决定生效路径，在独立 PR 交付。

**Architecture:** 只读归档适配层先核实完整证据，再提供官方数字身份、类别依据与稳定输入。
提案保持 proposed；开启工具只读登记；跨日决定仅应用已核准的登记并输出回执与失效审计。

**Tech Stack:** Python、现有 JSON/JSONL 归档和 SQLite、pytest 离线 fixtures；不引入新依赖。

**Spec:** `docs/tasks/T68-new-competition-team-identity.md` 第七节，所有者本轮明确指向
PR #142 的 `660c9e73332a41a3bb1f4b824a57cf5e4ee48f50`；该版本与审查记录暂存于
`work/t68-section7/`。已审协议为 `docs/experiments/T68-identity-binding-protocol.md`。
M1已独立完成于 #141 `26666dc8`，本计划不重做 M1。

## Global Constraints

- 不用名称相似度或任何模糊匹配生成或核准绑定；不合并证据不足的球队。
- 不削弱跨来源重复保护；不改既有18个联赛的绑定；全部候选仍为 proposed。
- 不改模型来源/参数，不回写冻结预测、报告、评分或账本，不打开 daily，不合并 PR。
- 开发环境不用正式密钥发真实请求；仅复用 `8b900f4cf0f9a1ca1afbafc9298e97b7cc7519f2` 或更晚归档。
- 类别注册表的新增分类须经审查及所有者核准；离线待审分类不得伪装成已批准。
- 五段审查建议中与第七节无直接关系的采集分类/组件失败策略/分页策略不顺手修改；B泛称关联不实现。
- 提交前全量 pytest 与 Ruff 通过。`reports/` 仅 README/SHA、总量不超200KB；完整证据独立数据分支。

## Review Focus

1. 官方数字ID与项目队ID数值恰好相同不能碰撞；无数字证据不能退回名称认队（Task 1）。
2. 真实已捕获的旧快照和当前表不同，不能验A读B；改正文与sidecar但没改归档清单仍须拒绝（Task 1）。
3. 平局、预备队、一个APIF ID对应两类官方队，不得凭单场比分/无名称标记放行（Task 1）。
4. 自指重放包、空导出、漏日期或双方来源冲突不能过开启检查（Task 2）。
5. 已应用决定失效、注册表移除、证据路径越界均可见；空清单不影响已有每日链路（Task 3）。

### Task 1：官方数字身份、可审类别和可复现提案

**Files:** 修改 `src/zucai/identity_binding_archive.py`、`src/zucai/identity_binding.py`；
按责任新增 `src/zucai/identity_categories.py` 与注册表 `data/identity_categories.json`；
必要时修复 `jc_catalogue_kickoffs.py` 的共享证据读取，但保留A1线上采集语义。
测试 `tests/test_t68_identity_archive_entry.py`、`tests/test_t68_identity_binding.py`，小原件放 `data/fixtures/t68/`。

**Interfaces:** 保留 `load_archive(root, *, leagues=None, fd_raw_dir=None)` 的既有字典契约，
扩展 `teams` 与 `official` 的数字身份/类别/原件证明字段；`replay(...)` 输出新增官方逐项登记。
`teams` 的键采用显式命名空间或严格的无碰撞转换，保留真实项目ID字段供开启工具使用。

- [ ] RED：真实执行读取器的离线归档；同名异ID/异名同官方ID、无规范目标、平局单证、非平局+平局全部见证、
  reserve/Jong/B/Castilla、亚运/成年冲突、同源ID占用、清单/sidecar/表/快照互换、TBD/PST占位时刻、重复回放输出字节相同。
- [ ] 官方目录和逐场原件数字ID作为身份键；已有规范目标只据明确数字关联对上；否则输出创建规范身份提案，展示名取官方原值。
- [ ] 按赛事登记类别及可审依据，旧条目从其原联赛类别得出；未知类别和预备队单列，名称只做否决。
  待审类别和创建身份必须显式披露批准缺口，不填虚假批准人/日期，不产生生产授权。
- [ ] 至少一个非平局见证，除非已有独立数字对手锚；记录全部一致见证与实际依赖，冲突及其依赖撤回。
- [ ] 核验并使用相同归档清单约束的目录/头原件/赛果表/数据库快照，稳定路径与核验时标，不拿当前时钟重写同一提案。
  不能用两个不同快照拼出一条可信证据；取消/占位状态保持不合格。
- [ ] `known_answers` 从正式表与实跑结果计算，不把93/107等旧汇总写作产品答案。
- [ ] 在固定归档输出A范围449官方ID、564赛事球队项的逐项提案或理由；381只是审查参考，不凑数。
- [ ] GREEN：定向测试与真实读取入口各两遍，输出字节一致；全量pytest、Ruff，提交本任务。

### Task 2：每日开启检查的真实分母与非空证据

**Files:** `src/zucai/identity_daily_gate.py`，`tests/test_t68_daily_gate.py`及新增实际入口测试。
**Interfaces:** 消费Task1的 `load_archive` 实际结果；保留 `build_register(archive, cutoff, *, codes=None, evidence=None, evidence_root=None)`；
在现有 evidence 中增加按官方ID列举的 `unbound_explanations`，每条带理由与证据引用。

- [ ] RED：实际调用 `build_register` 与 `main`，不得替换加载入口；已绑定、已关联各有可靠样例且失败理由逐项可追。
  覆盖同一导出自证三次、空snapshot、空held、漏counts、伪inserted数、漏官方日期、捕获早于覆盖结束、空风险审阅、同规范队双ID。
- [ ] 以Task1的官方ID到项目队的精确关联和库中实际对应指针计算绑定/关联；无可靠关联保持缺口，不推定全覆盖。
- [ ] 正确接受逐队未绑定原因输入；未知ID、重复/空原因或无证据不能抵消缺口。
- [ ] 重新核验原件覆盖、导入前后逐ID、当季counts、恢复内容及互相独立的证据；不接受空包或把同份文件当三次独立过程。
- [ ] 固定8b900f4实际输出日职/瑞超绑定/关联非零的逐ID清单，与原件/表独立复算一致；缺30日/回填证据仍不足，不开daily。
- [ ] GREEN：真实入口、攻击反例、全量pytest、Ruff，提交本任务。

### Task 3：跨日决定生效入口与失效审计

**Files:** `src/zucai/cross_day_decisions.py`、现有每日运行调用方、`src/zucai/sources/apif_audit.py`，
必要的工作流调用声明及 `tests/test_t68_cross_day_*.py`；README/ARCHITECTURE/DATA_SOURCES同步。
**Interfaces:** 保留 `apply_decisions(database, registry=None)` 的回执契约与 `load_decisions(session)` 的有效决定结果；
新增独立返回失效原因的审计函数，供现有每日审计消费，不能静默丢弃。

- [ ] RED：实际每日入口应用已批准决定；未核准/未来/快照变化/注册表删除不生效；失效时逐ID有原因；空清单继续原样运行。
- [ ] 明确在恢复与采集完成后、模型读取前应用已核准登记；无决定时零业务写入，不触发采集或改比赛/赛果/身份关系。
- [ ] 失效快照继续隔离并写每日审计；回执真实应用时间不倒填。证据路径限制在登记目录内，绝对路径/穿越/符号链接不得导出。
- [ ] 验证回执导出恢复、失效审计恢复、已有12组隔离仍保持；全量pytest、Ruff，提交本任务。

### Task 4：独立验收与交付

**Files:** `reports/t68-a-acceptance-hardening/README.md`、SHA清单；大证据在单独数据分支。
**Interfaces:** 固定每阶段提交、归档SHA、UTC/北京时间截止与完整命令。

- [ ] 冻结代码和资料；真实提案连续两次、逐项449/564清单、日职/瑞超门槛工具、跨日入口与失效审计都实际运行。
- [ ] 对比分母、原因与381参考的差异；严格验证与待审类别/身份的批准缺口分开，不能靠减少检查凑数字。
- [ ] 旧18键、模型配置、旧归档/冻结文件哈希不变；全量与Ruff最终复验；新旧结果行为变化逐项列明。
- [ ] 每任务内部复查后一次整分支复查；修复阻断项。内部复查不是Claude正式批准。
- [ ] 推独立分支，草稿PR标题 `T68:`；若#141尚未合并则暂以其分支为base并明确依赖，合并后普通merge最新main转base。
  所有者与Claude后续审查，绝不由实现者合并。

## 计划自查

第七节1已在原PR；2–6/9归Task1，7/9归Task2，8归Task3；Task4汇总实跑与边界。
Task1与Task2共享加载契约，按顺序执行；Task3只消费既有数据库/审计接口，不改提案数据结构。
类别与新身份的审批是正式生效前置，不用离线算法通过代替；这项边界必须在候选和报告中同时可见。
