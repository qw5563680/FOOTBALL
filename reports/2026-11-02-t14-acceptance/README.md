# T14 四来源云端采集验收

代码从 main `0c207c9` 创建 `codex/T14-cloud-collection`，随后同步到任务单更新 `699c57a`；PR base 为 main。
本目录只提交本 README 与 `SHA256SUMS`，无逐场证据、原始响应、数据库或模型文件。

## 已完成的本地验证

- 全量离线 `pytest -q -o addopts=''`：**636 passed**。778 条现有 penaltyblog / NumPy 弃用警告；基线也有相同警告。
- `ruff check src tests examples`、`ruff format --check src tests`、actionlint 1.7.7 与 `git diff --check` 均通过。
- `responses` 合成 fixture 完成恢复 → fd/fdorg/apif 导入 → 导出 → 写回目录 → 再恢复，五表逐行一致。
  覆盖 v1 升级、UTC 月界、closing_odds、每来源计数/哈希、损坏归档拒绝、候选证据持续保留且不激活、
  配额跨运行/保留额度/损坏状态、周一早晚班和 UTC 时区、单源与后续窗口失败、partial、训练历史读取。
- 本地裸 Git remote 运行工作流实际写回脚本：不同文件变更先 rebase 再 push；同文件冲突非零退出，远端提交不变。
- 模拟缺少 Unix `fcntl` 的环境可导入 sporttery/verify 入口；Windows 真机 Actions 仍待审查方验证。
- 所有测试进程移除真实 API 凭据；HTTP 由 `responses` 拦截。提交文件扫描未发现环境 key 的值。

## 现有数据分支回归

只读取得 `data/football-feeds` 提交 `2b7b76eb3141ff9309daba5cdd1d027deb9d10fd` 的本地副本，
使用离线 fixture 验证三源导入。为避免合成 fdorg 身份与真实 jc 的同时间比赛触发合法隔离，
该副本测试将合成 fdorg 赛程设为 2026-11-04 18:00 UTC 的 TIMED 场次，仍在请求窗口内。
没有改动远端数据分支，没有调用线上采集。

| 表 | 旧归档 | 本地导入后 |
| --- | ---: | ---: |
| teams | 456 | 462 |
| matches | 866 | 871 |
| results | 833 | 837 |
| odds | 2,319 | 2,319 |
| closing_odds | v1 无此表 | 1 |

最终 matches 来源 jc=866、fd=1、fdorg=1、apif=3；五表再次恢复后逐行相同。
本地副本与测试日志仅作工作区临时文件，不是发布的数据或验收附件。

## 一次在线冒烟

北京时间 2026-11-02 04:27:48–04:27:50，严格只对 fdorg 与 apif **各发一个带凭据 GET**；
无重试、无重定向、无数据库导入。响应字节只在内存解析，未写文件、fixture 或仓库。
实现前的三个匿名 robots.txt 读取结果见 `docs/DATA_SOURCES.md`。

| 来源 | HTTP | 返回行 | 已配置联赛解析行 | 拒绝行 | 响应配额 |
| --- | ---: | ---: | ---: | ---: | --- |
| fdorg | 200 | 104 | 104 | 0 | 分钟剩余 9 |
| apif | 200 | 1,187 | 29 | 0 | 当日剩余 60 |

响应原件未保存，以下仅为本次内存字节 SHA256，不能当作可下载附件：

- fdorg：`41c3cbf69f4c01ee24ddfc5447372ecdfad120e887d747680401f85e8b7095b8`
- apif：`dd6198d131ec5ae26c12c083978d089b22fe34671c7dd6598e1509c3823decbd`

## 待审查方在 PR 分支运行的 Actions 验收

本沙箱**没有触发 workflow_dispatch**。以下真实 run ID、Actions 入库计数、数据分支新提交号均待审查方填写，
不能用本地 Git 测试或在线 GET 替代。

1. 在 `codex/T14-cloud-collection` 分支触发 Football cloud feeds，`operation=apis`、`days=3`、`slot=late`。
   预期 apis 在 ubuntu-latest 运行；fd skipped；fdorg 一次七日窗口请求；apif 最近三日通常各一次请求。
   摘要包含 `complete` 或 `partial`、每源入库/链接/拒绝/请求数、apif 剩余配额、脱敏失败原因，写回成功后列数据分支提交号。
   真实行数取决于当时响应、权限和存量链接；有拒绝/疑似重复或失败时应标 partial，其他源继续保存。
2. 同分支触发 `operation=verify`、`days=3`、`slot=late`。预期托管机摘要 `verified` 与五表行数，无新数据提交。
3. 仓库变量当前为空，sporttery 应显示 skipped。可额外触发 `operation=sync` 验证空变量门控；此时 apis 也 skipped。
   审查方合并后再设单个标签 `FOOTBALL_FEEDS_RUNNER=football-local`，本 PR 不设置它。
   当前 Windows `D:\actions-runner` 已注册并由登录计划任务启动 run.cmd；两种运行方式见 `docs/RUNNER.md`。
4. 如需验证 fd，在 UTC 周一触发 `operation=apis`、`days=3`、`slot=early`；其他日即使 early 也跳过 fd。

预期数据分支文件：

- `feed_manifest.json` v2，包含 `counts`、`sources`、`files`、`collection`、`collection_status`。
- `matches/YYYY-MM.csv`；`state/{teams,matches,results,closing_odds}.jsonl`；有报价月份的 `state/odds/YYYY-MM.jsonl`。
- `state/api_football_quota.json`；有相应记录时的 `state/audit/<source>_*.csv`；旧竞彩拒绝/未知清单保留兼容。
- `state/alias_candidates/{fdorg,apif}.json`（无新证据时可为空对象），只保存待人工审查的 auto_verified 候选。
- 原件只进 `apis-source-<run_id>-<attempt>` / `sporttery-source-<run_id>-<attempt>` artifact，保留 90 天；
  rebase 冲突后仍上传已安全保存的原件和状态，数据分支不强推。

正式别名文件未修改；日期窗口使用 T09 图函数/T10 infer_aliases，不调用要求完整赛季的 T09 CLI。
没有修改来源解析与去重逻辑，没有自动训练或预测。真实 Actions 验收由审查方补充。
