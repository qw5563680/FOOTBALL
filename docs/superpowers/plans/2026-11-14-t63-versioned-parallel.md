# T63 Versioned Parallel Predictions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 按所有者修订保留七个旧规版本，同时独立训练冻结新来源版本，禁止纸面账户误用新版本。

**Architecture:** 主批次继续承载旧七联赛、已实验的八联赛和其他既有模型。七个新来源候选有独立参数、模型和同截点预测冻件，作为主批次的附属版本归档；旧冻结文件不回写。稳定定义ID与每日模型文件SHA分别保存。

**Tech Stack:** 现有Python、SQLite、SQLAlchemy、pandas、Dixon-Coles、pytest；不新增运行依赖。

**Spec:** `docs/tasks/T63-apif-results-for-existing-15-leagues.md` 在 PR #136、提交 `a9dcf0482c2010fdd77adcba139954816097a27f` 的修订版；`docs/PROMOTION-PROTOCOL.md` 同提交第7–8节。依据PR #133审查者2026-11-14 02:42:17 UTC转达的所有者定稿决定。#136在计划初稿时尚未合并；所有者已于UTC 2026-11-14 03:31合并为d6a52bbb。本实现仍不宣布T67规程已生效。

## Global Constraints

- 旧七：英超、西甲、意甲、德甲、法甲、葡超、巴甲；旧来源`fd,jc`；主参数条目完整恢复到基线`b83d2cc`。
- 新八：英冠、法乙、荷甲、日职、瑞超、挪超、芬超、美职；直接使用扩展来源，标“实验”。
- 旧七标签“可用（旧规，考核中）”，七候选标签“实验”；候选不进入虚拟单场、纸面串关、14场正式选用。
- 定义由赛事、模型族、训练来源集合、拟合方法、先验方法、校准方法、选参协议确定；每日数据、cutoff、pickle SHA、年度按既定协议重选数值不改变定义ID。
- 不改数值算法或阈值，不实现T67完整批准登记/计数，不按旧gate把候选升级为可用，不开放BET。
- 原归档e5848e5与阶段一实验/证据不改；新验收在`/workspace/work/t63-versioned-evidence/`。
- 实际体育请求、正式密钥、本地生产写入均为0；不合并任何PR。

## Review Focus

- 候选缺标、字段不一致或模型文件选错：必须拒绝进入纸面用途，不能默认成旧版。
- 候选失败、缺比赛或重试：不得替换旧版、补造概率、更改主批截止或覆写已冻结文件。
- 七个旧规条目和欧战：输入、拟合与概率须在同归档同截点保持，不能受到新身份证明政策收缩。
- 日期或年度参数变化：稳定定义不变；来源、模型方法或选参协议变化必须换定义。
- 导出恢复与发布失败：候选参数、模型、输入、预测、失败原因均有哈希，未发布内容不冒充已发布。

---

### Task 1: Stable definitions and readiness

**Files:** 新增`src/zucai/predict/model_definitions.py`；修改`predict/parameters.py`、`report/batch.py`、`report/render.py`和对应测试。

**Interfaces:** `LEGACY_LEAGUES`、`EXPERIMENTAL_LEAGUES`；`definition_metadata(model, entry)`与`entry_definition_metadata(league, entry)`返回`model_definition_id`、`model_definition`、`source_variant`。readiness另返回`paper_eligible`，保留旧冻结兼容性。

- [x] 先写并实跑失败测试：每日cutoff/文件SHA/年度半衰期数值改变不换定义；来源或方法改变会换定义。
- [x] 实现规范JSON哈希与严格字段校验，旧七/候选来源边界显式表达。
- [x] 验证候选即使具有历史良好gate也始终是“实验”、不可纸面使用；旧七显示指定人话标签。
- [x] 将元数据逐模型、逐预测冻结并测试旧格式仍可读取，模型概率不变。

### Task 2: Separate complete candidate archive

**Files:** 新增`src/zucai/source_version_archive.py`；修改`forecast_archive.py`、`forecast_publication.py`和归档测试。

**Interface:** `attach_source_versions(archive, stage, run_id, outcomes, documents, *, clock=None) -> updated_run`。
模型为`models/{run}/{league}.source-expanded.pkl`及`.source-expanded.train.json`；参数为
`models/{run}/source-expanded.params.json`；候选文件为`forecasts/{day}/{run}/source_versions.json`。
`run.source_versions={leagues,params_path,params_sha256}`；日冻结记录包含`source_versions_path`与`source_versions_attached_utc`。
attach只是本地装配，不能称已发布。`candidates/confirm_publication/publication_times`登记候选自身的path、SHA、
generated时间和post-push receipt；主批已发布不代表候选已发布。

- [x] 写失败测试：候选必须与主批七联赛目标具有相同match ID、身份、batch ID和as_of，越界/缺时标拒绝。
- [x] 复用现有原子发布与manifest校验，完整保存输入、参数、模型、预测和失败原因；候选路径独立。
- [x] 测试重复运行不覆写、损坏哈希拒绝、导出恢复一致；发布失败不能出现虚假的成功发布时间。
- [x] 保持主LATEST与原三条实验线行为不变，候选失败不阻断或替代旧版主批次。

### Task 3: Paper and SFC isolation

**Files:** `scoreboard/paper.py`、`scoreboard/score.py`（仅字段透传）、`parlay/single_double.py`、`parlay/paper.py`、`report/sfc.py`及专测。

**Interfaces:** 消费Task1定义/来源变体与`paper_eligible`；主批次旧格式走原兼容路径，新的定义或候选字段出现时要求相互一致。
新格式batch/manifest具有强制版本标志`model_definition_schema=1`；新模型与逐场预测字段必须完整并互相一致，score展开时透传格式上下文。
“某一行没有新字段”不能单独证明是旧冻件；候选sidecar的外层上下文永远不可fallback为旧版。

- [x] 实跑失败测试：同match ID的新旧并行记录不会让候选进入纸面账户、串关或替代SFC旧版。
- [x] 实现用途过滤和字段一致性检查，不改变金额、结算、下注选项或旧账。
- [x] 覆盖候选漏标、错误定义、候选伪装旧版以及完全旧冻结无新字段的兼容输入。
- [x] 验证原纸面记录与旧批次结果不变。

旧七资格仅绑定基线完整方法定义，不能仅凭`fd,jc`或历史gate授予；同来源换拟合、先验、校准、选参协议仍是新定义。
既定年度选参协议不换定义，CLI手工override属于不同选参方式。同一旧定义不因旧gate最近赛季过期自动降级，
但保留原有真实来源、非replayed、未知球队与逐场适用性等安全校验。不创建T67动态批准登记或虚构批准日期。

### Task 4: Orchestration and legacy model compatibility

**Files:** `cloud_predict.py`、`predict/backtest.py`、`predict/pooled.py`、必要训练入口、`data/model/params.json`、独立候选参数及集成测试；移除`predict/revalidate_sources.py`的gate升级入口。

**Interfaces:** 调用Task1元数据与Task2归档接口；旧七参数完整保留，七候选独立参数；显式legacy读取路径用于旧七和欧战，新八及候选使用T63真实时标/身份政策。

- [x] 写并运行失败测试：每日旧七与七候选同时训练，候选失败不回退替代旧版；其他既有模型不变。
- [x] 接通同截点候选生成和完整冻结，旧7/新8主路径选择明确；删去旧gate自动恢复资格的路径和文档。
- [x] 同e5848e5副本独立运行main与修订版：旧七各可信历史截点的拟合参数及概率逐项相同；欧战输入与预测相同。
- [x] 测试新候选仍保留实际入库、数字身份及关系时标、同源组件唯一性、导出恢复保护。

#### Final compatibility discovery

首次3579项全绿后，对未来首次retain导入做独立窄复现：既有FD或JC同场加APIF赛果时，
新ResultSourceLink有真实时标，但欧战legacy入口仅认counterpart，仍拒绝训练。
固定e5848e5没有新增该类赛果，原105场不变性不能替代这项导入检查。

- [x] 以实际离线导入入口复现，保留原FD/JC比赛与counterpart字段，不用无时标边绕过。
- [x] 仅为新增关系补严格截止的重复选择，覆盖晚到/失效关联、先APIF后高优先级及比分差异；旧可靠赛果与审计不变。
- [x] 新目录重新跑基线39组拟合与105场预测比较，旧验收原件保留。
- [x] 最终全量测试与内部窄复审通过后才提交。

### Task 5: Final evidence and draft delivery

**Files:** 更新README、ARCHITECTURE、DATA_SOURCES、T63验收README及SHA清单。

- [x] 实跑全量`pytest -q`、`ruff check src tests`、`ruff format --check src tests`与`git diff --check`。
- [x] 独立内部复审，逐条关闭必须修；不将内部复审当Claude正式批准。
- [ ] 新增副本/版本不变性证据放新数据验收位置，阶段一70场配对与源档保留；reports总量不超过200KB。
- [ ] 推送同T63分支并更新草稿PR，注明#136依据、线上权限/回填/三次运行未验收；不合并。
