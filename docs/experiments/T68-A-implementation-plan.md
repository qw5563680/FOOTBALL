# T68 A 段实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. 使用 `- [ ]` 记录步骤。本计划依据已获条件批准的方案，不再次请求已授权的A段实施许可。

**Goal:** 提供严格绑定证据与补时刻工具，修正跨日重复和采集口径，产出可复核离线证据。
**Architecture:** 沿用正式候选队列、reconcile/relink-jc、原来源保存策略；只新增离线校验/审核工具和局部保护。B段仅文档，不实现。
**Tech Stack:** Python 3.12、SQLAlchemy/SQLite、requests、现有pytest/ruff；不新增依赖。
**Spec:** `docs/experiments/T68-identity-binding-protocol.md`；PR140 review5904223500；PR139 task增补，由所有者本轮指示采用。

## Global Constraints

- B段泛称赛事关联不实现；不设新赛事独立留行/时标范围，不修改模型来源/参数。
- 正式API密钥仅Actions，开发不发体育请求；全部测试仅data/fixtures离线样本。
- 只产生待审候选，不自行核准别名，不开启daily，不改生产数据/冻结预测/旧报告/账本。
- 同联赛同向，绑定提案开球准确相等、90分钟完赛比分相等、完整窗口双向唯一；不用模糊名称或比分消歧。
- 既有18联赛原条目不改不删；新T68条目逐赛事范围限制，旧条目语义不变。
- 每批保存实际捕获/核验时间；新证据不倒填，不用后到证明改写既有冻结结果。
- 主干测试基线4023通过；最终全量pytest、ruff check、ruff format --check；reports≤200KB。

## Review Focus

- 目录中的同ID与主客/日期不符：补时刻不补造、不写比赛表。
- 多原件同fixture冲突/缺页或拒收隐藏竞争者：不推断新绑定。
- 同层互相自证、上层锚被否：阻断与撤下全部传递依赖。
- 36h重叠有多个候选/第三方已有关系：隔离不得因“不唯一”或“已有任意链接”而漏掉。
- 当前配置注入过去归档：无实际观察证据的跨日决定不影响更早cutoff。

### Task 1: JC目录时刻补充及Actions入口

Files: 新增 `src/zucai/jc_catalogue_kickoffs.py`，修改 `cloud_feeds.py`、`.github/workflows/football-feeds.yml`；测试 `tests/test_t68_jc_kickoffs.py`，样本 `data/fixtures/t68/heads/`。
Interfaces: `plan_kickoffs(archive: Path) -> dict`；`collect_kickoffs(database, run_id, *, max_requests=5, archive=None) -> dict`（cloud_feeds传入原归档；缺此参数时可用恢复证据）；计划命令 `python -m zucai.jc_catalogue_kickoffs plan --archive ... --out ...`。
- [x] 测试先写：只选新路由1083+泛称179的同ID缺时刻集；有可核验时刻不请求；主客ID/日期/HTTP/SHA错误不成功；重复0请求；未有先导成功记录不能全量。
- [x] 跑红测试，再实现复用 `_head_time`、≥1秒限速、仅self-hosted workflow_dispatch入口；前5最早历史ID先导。
- [x] 归档 `state/catalogue/<run>/jc-heads/<id>.json(.meta.json)`、`jc-kickoff-plan.json`、`jc-kickoffs.json`，加入manifest导出恢复白名单。失败记录不当成功游标。
- [x] 用fixtures验证collector、CLI、导出恢复与工作流路由；定向149通过，内部独立复查135通过，全量4103通过；ruff check/format通过。固定输入146复用、1116待取，真实先导尚未执行。

### Task 2: 严格绑定提案与正式目标范围

Files: 新增 `src/zucai/identity_binding.py`（必要时拆 `identity_binding_archive.py`），修改 `sources/apif_aliases.py`、`sources/apif_reconcile.py`、`alias_propagation.py`；测试 `tests/test_t68_identity_binding.py`，样本 `data/fixtures/t68/identity/`。
Interfaces: CLI `python -m zucai.identity_binding --archive ... --out <全新目录> [--leagues ...]`；输出 `apif.json` 使用既有按联赛/球队ID候选结构，`summary.json`、证据与未提案原因。所有新项status=proposed。可支持 `--reject <候选ID>` 撤下层级依赖。
- [ ] 正反测试：严格时间/90min/方向/唯一性；FD早年证据；女足/预备/青年/奥运亚运类型不得合并；缺完整性不得提案。
- [ ] 分层算法只用上一轮新增锚；记录layer/depends_on/原件SHA及ID；冲突或拒绝撤下全传递依赖；候选不成为生产active。
- [ ] 归档适配器从完整JC目录+匹配库内行/已核验head、T61完整原件（含未入库）、规范FD证据读入。并列真实未核实原因，不把原件名当规范身份。
- [ ] 已核准同providerID传播仅明确兼容类别；新正式项 `binding_scope` 限定target leagues（与原件身份类别一致），reconcile/propagate拒绝越界，旧无该字段项兼容。
- [ ] 固定归档已知答案：目标93条及新赛事审查基线107项，仅记录真实结果；不同逐条解释，出现相反阻止交付；不写正式别名。

### Task 3: 36h隔离与最小跨日决定

Files: `source_result_quarantine.py`、`source_results_audit.py`，必要新增 `cross_day_decisions.py`；相关history/training读取仅必要适配；测试 `tests/test_t68_cross_day.py`，fixtures `data/fixtures/t68/cross_day/`。
Interfaces: 保持 `pending_fd_identity_overlaps`/`exclude_pending_fd_overlaps` 调用兼容；新双边同ID分支不按比分选择，返回明确隔离原因与配对ID。
- [ ] 红测试覆盖12真实ID中的裁剪样本、≤36h与>36h、多候选、第三来源关系、比分不同、单边FD未知旧保护、cutoff双方可知。
- [ ] 仅训练读时隔离，完整组件是否已相连，不因某行曾有其他链接免检；旧可靠源不动。
- [ ] `data/identity_reviews/t68-cross-day.json` 最小same/different决定仅限跨日指定对；完整快照/审批/实际应用时间，导出恢复可选证明，不造超3h T63关系或泛称关系视图。无已批准真实决定时维持隔离。
- [ ] 临时身份审计按联赛汇总计数，其他时间/比分/跨日风险仍逐行；保留原始来源行。
- [ ] 实测固定归档12组只计一次；未受影响输入ID/参数/概率对照交整体验收。

### Task 4: 采集分类、候选隔离、开启门槛与整合

Files: `cloud_catalogue_backfill.py`、`cloud_api_backfill.py`等实际catch入口、`cloud_aliases.py`、`sources/apif_ingest.py`、报告渲染；必要新增 `identity_daily_gate.py`。测试 `tests/test_t68_collection_status.py`、`tests/test_t68_daily_gate.py`。
- [ ] RequestBudget专用待续原因：到上限无请求/无配额/无成功checkpoint；HTTP/校验失败仍失败，摘要数字分开。
- [ ] 未绑定导致暂缓与真疑似重复/拒收分开；未绑定单独发生不使日报异常；混合真实冲突仍异常。
- [ ] 候选坏行按可安全确定的赛事隔离；未知归属整份不作候选。旧候选差异只允许此前被别赛事坏行牵连的情况。
- [ ] 每赛事只读开启登记校验：球队/场次分母可解释、当季暂缓补齐或明确原因、30天完整覆盖、风险逐项、重复0及恢复一致；缺证据不判可开。工具不写daily开关。
- [ ] README、ARCHITECTURE、DATA_SOURCES更新；独立固定归档副本跑A段工具、二次0新增及导出恢复；正式冻结/旧账本SHA不变。
- [ ] 全量pytest/ruff、内部独立review；报告大文件放独立证据分支，原PR推送并逐项回复，不合并。线上head请求、条目核准和开启仍等Claude。
