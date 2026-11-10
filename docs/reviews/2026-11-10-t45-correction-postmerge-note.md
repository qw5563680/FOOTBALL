# T45 纸面选法纠偏（PR #93）审查记录补档

审查结论已于 2026-11-06 18:27 UTC 以 GitHub review（review_id 5804997321）发在 PR #93 上，
当时审查会话中断，未同步归档到 docs/reviews；本文件为补档，内容以该 review 为准。

- 对象：PR #93（`codex/T45-paper-selection-correction`，head 53d74ad，+583/−118，15 文件），
  已由所有者于 2026-11-06 20:09 UTC 合并（merge commit d10f37f）。
- 审查方式：完整 diff 阅读 + 独立 worktree 复跑 pytest -q 与 ruff + 对照修订后的 T45 任务单。
  任务单修订系所有者经 ChatGPT 草拟后下达，审查记录按「所有者决定」对待并在 review 中请所有者
  确认了与原交接口径的差异（方向不明/伤停未知候选从「只进诊断」改为「不再默认淘汰」）。
- 红线核查通过：SP 赔率仅作 1.20 下限展示过滤，不进模型概率；`research_invalidation`
  要求证据 `observed_utc` 严格早于截点；测试全部离线 fixtures；新旧规则分版本统计，
  v1 已冻结批次字节不变（跨版本 batch 锁）。
- 分级结论：必须修 0；建议修 2（legacy_filter_diagnostic 对 gap=0 行的归类口径、
  tests 内 `from test_parlay_single_double import generate` 依赖 rootdir 导入方式）；
  仅说明 3（`publish_document` 中 `previous_version_sha256`/`replaced_groups` 成为死代码、
  REASONS 保留 v1 文案、合并后首个真实 run 需复核 v2 冻结与 F 账户分层）。
- 合并后跟进：2026-11-07 起每日 run 已按 v2 规则冻结（run 111 起），上述「仅说明」项
  未发现实际故障；死代码项留给后续小修任务处理。
