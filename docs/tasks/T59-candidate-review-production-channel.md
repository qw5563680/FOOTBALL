# T59 竞彩待核队名的生产处置通道；附手工抓取路径降级

来源：T58 审查（PR #124）建议修 1、2；所有者 2026-11-13 确认开单。
分支 `codex/T59-candidate-review-production-channel`，PR 标题以 `T59:` 开头。

## 一、核验结论发布回正式数据的通道（主项）

### 现象

T58 的 `zucai.jc_candidate_review`（`inspect` / `apply`）只处置恢复副本的
`state/alias_candidates/jc.json`，`docs/ARCHITECTURE.md` 也明确「生产处置须另按当时最新库制定计划」。
但仓库里没有任何机制把处置后的队列发布到 `data/football-feeds`：日常链路每次从归档恢复队列，
`export_feeds` 再原样导出，于是生产日报的「竞彩待核新队名 5 个」永远不会变。
首批 5 个候选的核验结论（哥伦比亚、巴拉圭、新加坡、阿根廷 distinct；湘南海洋 pending）
目前只存在于 T58 的验收副本里。

### 范围

1. 定义一条可审计的生产处置通道，建议形态：核验计划文件提交进代码仓库（例如
   `data/jc_candidate_reviews/<日期>-<说明>.json`，含 `inventory_sha256` / `queue_sha256`），
   `football-feeds.yml` 新增 `workflow_dispatch` 操作（如 `operation=candidate-review`，
   输入计划路径），由审查者触发：恢复最新归档 → 校验计划哈希与当时库一致 → `apply` →
   走既有 `publish-football-feeds.sh` 发布队列与审计文件。哈希不一致即失败、不发布。
2. 审计文件（T58 `apply` 产出的 report）进入归档（建议 `state/audit/jc_candidate_reviews/`），
   纳入 feeds 清单与 `restore_feeds`。
3. 首批 5 个候选按新通道实际处置一次（由审查者触发，证据留 Actions）；若 `queue_sha256`
   已因新候选变化，须重新 `inspect` 并制定新计划，不得沿用旧副本的计划。
4. 不改 `jc_candidate_review` 的判定规则与红线（不自动合并、不模糊匹配、`duplicate` 仍 pending）。

### 验收

1. 离线：对固定归档副本用新操作的本地等价命令跑一遍，队列中 4 个 distinct 变 `cleared`、
   湘南海洋仍 pending；审计文件生成并被 `feed_manifest.json` 登记；`restore_feeds` 后队列与
   审计文件可用；旧截点的日报重放仍显示 5 个待核（不倒填）。
2. 哈希不一致的计划（篡改 `queue_sha256`、或队列已新增候选）→ 操作失败、归档字节不变。
3. 线上：审查者触发一次，数据分支出现处置后的队列与审计文件，之后的日报待核行从 5 变为 1。
4. 归档其余文件 SHA256 不变；全量 pytest、ruff 通过；`reports/` ≤ 200 KB；
   README 补子命令/操作用法，ARCHITECTURE 补通道说明与数据模型（如新增目录）。

## 二、手工抓取路径对未核准编号按行降级（附带）

- 现象：T58 后 `sfc_team` 遇未核准编号抛 `UnknownSfcTeam`；`src/zucai/sources/sfc.py`
  `fetch_sfc` → `ingest_issue` 默认 `mapped_only=False`，`_snapshot_rows` 对该异常
  `if not mapped_only: raise`，审查者手工触发的真实抓取会整期中止、不归档。
  日常链路（`collect_current_sfc`、`refresh_snapshot`，`mapped_only=True`）已正确按行降级。
- 范围：手工路径同样按行降级为 unmatched 并写 `national_team_name_missing` 证据，
  其余校验（编号与名称冲突仍抛错）不变。
- 验收：对含未核准编号的快照调用 `fetch_sfc` 等价离线路径，期次正常归档、该行 unmatched
  带缺表证据；新增回归测试。

## 三、可选：稳定一例偶发测试

- `tests/test_sfc_publication.py::test_t57_sfc_history_is_saved_transactionally_and_reused[shallow]`
  在审查者环境全量运行中偶发失败（单跑、整文件、第二轮全量均通过），疑为 Git 子进程时序。
  若能定位根因则修；定位不了则在 PR 写明，不得用重试或跳过掩盖。

## 不做

不改模型、参数、BET；不回写冻结预测、旧报告、旧评分、旧账本；不改候选判定规则；
不新增联网采集；除新增一个 dispatch 操作外不动 workflow 的 cron 与既有 job。
