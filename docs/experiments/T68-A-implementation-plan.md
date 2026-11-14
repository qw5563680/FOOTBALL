# T68 A 段实施计划


**Goal:** 提供严格绑定证据与补时刻工具，修正跨日重复和采集口径，产出可复核离线证据。
**Architecture:** 沿用正式候选队列、reconcile/relink-jc、原来源保存策略；只新增离线校验/审核工具和局部保护。B段仅文档，不实现。
**Tech Stack:** Python 3.12、SQLAlchemy/SQLite、requests、现有pytest/ruff；不新增依赖。
**Spec:** `docs/experiments/T68-identity-binding-protocol.md`；PR140 review5904223500；PR139 task增补，由所有者本轮指示采用。

按 [复审要求](https://github.com/qw5563680/FOOTBALL/pull/140#pullrequestreview-5904285942)，PR #140 只交付方案与 Task 1。Task 2–4 在 `codex/T68-A-identity-and-quarantine` 后续 PR 实施；本计划保留全段依赖关系。B 段设计及候选赛事集合已获批准，另行实施前先提交全部 counterpart_id 读取位置的盘点与测试，不并入 A 段。

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
- [x] 用fixtures验证collector、CLI、导出恢复与工作流路由；第二轮定向156通过，内部独立复查156通过，全量4110通过；ruff check/format通过。A1交付时固定输入146复用、1116待取；所有者合并后，审查者已在main完成1116次取数（归档afdf86d），最新资料另作补充回放。全量三连败停止、manifest摘要与历史状态清除、首份固定DB缓存已按第二轮复审补测。

### Task 2: 严格绑定提案与正式目标范围

Files: 新增 `src/zucai/identity_binding.py`（必要时拆 `identity_binding_archive.py`），修改 `sources/apif_aliases.py`、`sources/apif_reconcile.py`、`alias_propagation.py`；测试 `tests/test_t68_identity_binding.py`，样本 `data/fixtures/t68/identity/`。
Interfaces: CLI `python -m zucai.identity_binding --archive ... --out <全新目录> [--leagues ...]`；输出 `apif.json` 使用既有按联赛/球队ID候选结构，`summary.json`、证据与未提案原因。所有新项status=proposed。可支持 `--reject <候选ID>` 撤下层级依赖。
- [x] 正反测试：严格时间/90min/方向/唯一性；FD早年证据；女足/预备/青年/奥运亚运类型不得合并；缺完整性不得提案。
- [x] 分层算法只用上一轮新增锚；记录layer/depends_on/原件SHA及ID；冲突或拒绝撤下全传递依赖；候选不成为生产active。
- [x] 归档适配器从完整JC目录+匹配库内行/已核验head、T61完整原件（含未入库）、规范FD证据读入。并列真实未核实原因，不把原件名当规范身份。
- [x] 已核准同providerID传播仅明确兼容类别；新正式项 `binding_scope` 限定target leagues（与原件身份类别一致），reconcile/propagate拒绝越界，旧无该字段项兼容。
- [x] 固定归档已知答案实跑93条、相反0；新赛事诊断116，对参考107净差9，缺参考逐项名单，不能伪称已定位这9项。严格提案0、旧队列138原样、第二次新增0；88项缺口逐项说明，不写正式别名。定向368与内部复验280通过，见后续A段验收报告。

### Task 3: 36h隔离与最小跨日决定

Files: `source_result_quarantine.py`、`source_results_audit.py`，必要新增 `cross_day_decisions.py`；相关history/training读取仅必要适配；测试 `tests/test_t68_cross_day.py`，fixtures `data/fixtures/t68/cross_day/`。
Interfaces: 保持 `pending_fd_identity_overlaps`/`exclude_pending_fd_overlaps` 调用兼容；新双边同ID分支不按比分选择，返回明确隔离原因与配对ID。
- [x] 红测试覆盖12真实ID中的裁剪样本、≤36h与>36h、多候选、第三来源关系、比分不同、单边FD未知旧保护、cutoff双方可知。
- [x] 仅训练读时隔离，完整组件是否已相连，不因某行曾有其他链接免检；旧可靠源不动。
- [x] `data/identity_reviews/t68-cross-day.json` 最小same/different决定仅限跨日指定对；完整快照/审批/实际应用时间，导出恢复可选证明，不造超3h T63关系或泛称关系视图。无已批准真实决定时维持隔离。
- [x] 临时身份审计按联赛汇总计数，其他时间/比分/跨日风险仍逐行；保留原始来源行。
- [x] 固定归档26套实际拟合：仅美职4751→4739精确隔离12条；其余25套输入、参数、概率及比分矩阵逐项相同，七个旧规联赛分别核对。恢复拟合和最终全量另列整体验收。

阶段实跑652定向、独立27反例通过；修后26套实际训练输入仅美职精确减少指定12，其余25逐行一致。最终参数/概率与恢复拟合仍由整体验收记录。

### Task 4: 采集分类、候选隔离、开启门槛与整合

Files: `cloud_catalogue_backfill.py`、`cloud_api_backfill.py`等实际catch入口、`cloud_aliases.py`、`sources/apif_ingest.py`、报告渲染；必要新增 `identity_daily_gate.py`。测试 `tests/test_t68_collection_status.py`、`tests/test_t68_daily_gate.py`。
- [x] RequestBudget专用待续原因：到上限无请求/无配额/无成功checkpoint；HTTP/校验失败仍失败，摘要数字分开。
- [x] 未绑定导致暂缓与真疑似重复/拒收分开；未绑定单独发生不使日报异常；混合真实冲突仍异常。
- [x] 候选坏行按可安全确定的赛事隔离；未知归属整份不作候选。旧候选差异只允许此前被别赛事坏行牵连的情况。
- [x] 每赛事只读开启登记校验：球队/场次分母可解释、当季暂缓补齐或明确原因、30天完整覆盖、风险逐项、重复0及恢复一致；缺证据不判可开。工具不写daily开关。
- [x] README、ARCHITECTURE、DATA_SOURCES更新；旧e370与补head的afdf各两轮提案回放新增0；核心副本两次导出恢复207文件SHA一致，固定原件/冻结对象未变。
- [x] 最终全量4356/0fail/error/skip与Ruff通过，内部审查问题关闭；完整证据另存独立验收分支，后续A段草稿PR交付不合并。head已由审查者完成；具体条目核准、应用和每日三次验收仍待Claude/所有者。
