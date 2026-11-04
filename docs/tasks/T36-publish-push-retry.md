# T36 · 数据分支发布推送的失败重试（小修）

## 背景
`scripts/publish-football-feeds.sh` 被 apis / sporttery / predict / scoreboard / digest 五个 job 共用，最后一步
`git pull --rebase` + `git push` 失败就直接让作业失败。2026-11-04 GitHub 事故（Git 操作 / Actions 降级）期间，
17:40 档定时任务采集成功但推送被 `remote: Internal Server Error` 拒绝，predict 被跳过；之后三次手工补跑的 predict
都把预测算好了（附件 forecasts-41814316968-1、forecasts-41814865083-1 等），却在同一步被拒，Issue #39 当晚没更新。
短暂抽风时丢一档预测的后果是：开球前最后一档若恰好失败，那场比赛没有冻结记录，记分板缺行。

## 范围
1. `publish-football-feeds.sh` 的 `git pull --rebase` + `git push` 改为最多 **3 次**尝试，间隔 **30 秒**（第 2 次起可用 30/60 秒递增）：
   - 只对 push 的远端 5xx / `Internal Server Error` / 网络超时重试；`rejected (fetch first)`、`non-fast-forward` 走原有的 rebase 后再推，
     rebase 冲突仍立即失败（保持「冲突不碰远端」）。
   - 每次失败把 `git push` 的 stderr 原文和尝试次数写入 `$GITHUB_STEP_SUMMARY`。
   - 三次都失败：退出码非 0（保持现有 artifact 兜底步骤能触发），摘要注明「推送失败，产物在 artifact」。
2. predict / scoreboard / digest 的 `if:` 条件不变；不改任何 Python 代码、模型、数据格式。
3. README「云端采集」一节加一句重试说明；ARCHITECTURE 发布流程处补一行。

## 不做
- 不加空提交、不自动重跑整个 job、不改并发组；不改 workflow_dispatch 输入；不动数据分支历史。
- 不处理持续一两小时的大故障（靠下一档定时任务或审查者手工补跑）。

## 测试（离线）
用本地裸仓库作远端，`pre-receive` 钩子前两次返回失败、第三次放行，断言：脚本最终成功且远端只多一个提交；
三次都失败时退出码非 0、远端未变；`non-fast-forward` 场景走 rebase 后一次成功；rebase 冲突立即失败且远端未变。
shell 测试放 `tests/test_publish_script.py`（subprocess 调 bash），不联网；actionlint 通过。

## 真实验收（审查者）
合并后观察连续两档定时任务摘要中出现「推送 1/3 成功」字样；人为不制造故障。

## 完成标准
pytest / ruff / actionlint 通过；README、ARCHITECTURE 同步。
