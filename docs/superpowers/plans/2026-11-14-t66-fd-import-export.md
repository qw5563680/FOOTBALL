# T66 FD 导入、导出与补采修复计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** 解除一致FD关联被误拒的导出故障，并使缺跑/失败后的采集可以在后续班次补回。
**Architecture:** 独立统计证据保持原值；只在校验时认可“原为空、当前为同一个已核验FD编号”。采集时机改为距完整成功采集满七个UTC日期，无成功记录则下次执行；成功状态随feeds哈希导出恢复。
**Tech Stack:** 既有 Python、SQLite/SQLAlchemy、pytest、requests离线拦截；不加依赖。
**Spec:** `docs/tasks/T66-fd-import-export-validation-failure.md`（main d6a52bbb）。

## Global Constraints

- 独立分支 `codex/T66-fd-import-export-validation-failure`，PR标题以T66开头。
- 截止北京时间2026-11-16 10:40（UTC02:40）；合并仍须Claude正式审查和CI。
- 不改模型、参数、来源范围、冻结预测、报告或账本；不向体育服务联网、不使用正式密钥。
- 所有pytest用fixtures；全量测试及ruff通过；reports只放README/SHA且小于200KB。

## Implementation decisions

任务单明确将实现方式交给实现者。选择校验承认精确一致的状态，而不改写52条旧关联证据；同步重写证据会抹掉原始关联现场，范围更大。
选择七个UTC日的成功间隔，而非只延长到周一晚班；这样整天缺跑也能恢复。失败/partial/无实际范围不更新时间，正常成功后避免每班重复下载。

## Review Focus

- 只允许None→当前关联的同一FD编号；不同FD、其他来源、旧fdorg被替换仍拒绝。
- 同一FD编号不能绕过主客反向、共享球队、窗口、别名、原件哈希或唯一性验证。
- 51条jc→fdorg为已吸收引用，逐条检查实际fdorg行是否还存在；独立FD/fdorg路径继续原冲突保护。
- 成功标记必须随档案恢复；旧快照不得残留未来成功标记，导出失败不得在下一次恢复后抑制补采。
- UTC日期边界、缺标记、部分失败、无范围、晚班补采与成功后跳过均有离线测试。

### Task 1: 精确关联兼容与真实原件

**Files:** `src/zucai/fd_stats_links.py`、`src/zucai/stats_archive.py`；新增 `tests/test_t66_fd_links.py` 和 `data/fixtures/t66/`。
**Interfaces:** 保持 `validate_fd_links(db)`、`resolve_fd_match(...)` 的公共行为；内部候选校验只接受显式给定的同一FD编号。

- [x] 先恢复e5848e5，52条校验通过；仅设置jc:2042418的同一FD编号即复现旧错误，保留日志。
- [x] 从已保存2026-11-04 N1原件裁剪真实比赛行，记录源文件/sidecar哈希、行号和真实捕获时间；11-14失败运行附件因网络代理403未下载，不能冒充重放该批次。
- [x] 添加“先有统计关联，再完整FD导入”测试，看到导出失败；并增加原fdorg不可换成FD、其他来源/不同FD/反向/共享/多候选拒绝测试。
- [x] 最小修复校验；旧T48拒绝测试原样保留并通过。
- [x] 独立副本完整导入、重复导入、导出/恢复；核查52条关联、已有可靠赛果及冻结目录未变。完整52行对应原件实跑列明实际找到/未找到，不凑数。

- [x] 内部独立复查发现精确counterpart会绕过后续观察追加和读侧原件验证，已补回双侧校验；新捕获版本保留，伪造哈希/位置/时间继续拒绝。

### Task 2: 成功日期与后续班次补采

**Files:** `src/zucai/cloud_apis.py`、`cloud_feeds.py`的可选状态处理；新调度测试及现有调度fixture。
**Interfaces:** 日常FD范围不变；可选成功状态进入feeds文件SHA，恢复旧快照时清除较新状态。

- [x] 新增缺早班/失败早班后晚班补采、七天UTC边界、成功后不重复与状态往返测试，先确认旧逻辑失败。
- [x] 实现完整成功后更新状态；partial/配置失败/无范围不更新；不影响其他来源和预测班次。
- [x] 修正旧测试的显式成功前提并保留原非FD断言，复跑相关领域与工作流测试。

- [x] 晚班补采后复用本批FD原件保存技术统计；仅扩展既有零体育请求步骤到晚班，用独立-fd批次避免与APIF附件冲突，不扩展采集接口范围。

### Task 3: 整合与交付

- [x] 更新README、ARCHITECTURE、DATA_SOURCES的真实行为与限制，附修复前后接受/拒绝矩阵。
- [x] 全量pytest、ruff check/format、diff检查；核验源码和全部证据哈希。
- [x] 完整证据追加到独立数据验收分支；代码PR中只放README/SHA，保持生产数据不变。
- [ ] 推送独立T66 PR并检查CI；等待Claude正式审查后按所有者授权处理合并，线上补采由任务指定审查者执行。
- [ ] 回到T65/T63，对照已合并#136的任务修订和最新审查，仅补剩余差异。

## 2026-11-14 北京时间13:09（UTC05:09）实跑进度

- 基线3247通过；最终3331通过，0失败/错误/跳过，ruff/format/diff通过。
- e5848e5完整副本、9份2026-11-04原件：52条关联保留，新增FD4场与训练读取新增4行一致；第二次新增0；14表导出恢复一致；2393个冻结文件不变。
- 11-14失败运行附件未下载，线上新鲜度与正式每日链路尚未验收；Claude审查、CI与合并是后续交付门。
