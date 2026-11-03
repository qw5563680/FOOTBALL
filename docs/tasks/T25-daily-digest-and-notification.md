# T25 · 每日摘要页面与推送（所有者直接可看）

## 背景
每天的预测、记分板和采集结果现在分散在三个地方：数据分支 `forecasts/<北京日>/<run>/report.md`、`scoreboard/scoreboard.md`、
Actions 运行摘要。所有者需要翻分支和运行记录才能看到「今天预测了什么、昨天对了几场、虚拟账户现在多少钱」。
仓库是私有库，GitHub Pages 需要付费计划，因此不用 Pages；用 GitHub 自带、免费且私密的渠道：数据分支里的 Markdown 页面 + 一条 Issue 评论推送
（Issue 评论会通过 GitHub 的通知到所有者的手机 App 和邮箱）。

## 目标
1. 每天两班运行后自动生成一页 Markdown「今日摘要」，手机上打开 GitHub 就能读完。
2. 同一份摘要作为评论发到一条固定的「每日报告」Issue 上，所有者收到 GitHub 通知即可查看。
3. 摘要只复述已冻结的报告和记分板数字，不重算、不改写；第一行固定「非购买建议 · BET 禁用」。

## 范围
1. **摘要生成**（新模块 `src/zucai/daily_digest.py`，命令 `python -m zucai.daily_digest --archive ../cloud-data --out ...`）：
   只读数据分支，产出 `daily/<北京日>.md` 与 `daily/latest.md`，内容按顺序：
   - 头部：日期、本次 run ID、数据水位（竞彩同步到哪天）、采集状态（四来源 complete / partial / skipped 一行）；
   - 今日与明日预测：按联赛分组，每场一行：开球北京时间、对阵、胜/平/负概率、模型版本、P2 标签（ready / experimental）、
     比分前五（T19 已在摘要里有）；来自当天最新 `forecast.json`，没有预测的比赛列「无模型」；
   - 昨日结算：逐场预测概率 vs 实际结果、当日 LogLoss / Brier、累计样本数与两道门槛的进度条式文字（如「前瞻样本 37 / 200」）；
   - 虚拟账户：五个账户在 1.05 档的资金、昨日注单与结果、累计 ROI；
   - 异常：partial 原因、疑似重复数、配额剩余（apif plan / limit_day）。
2. **推送**：工作流在 `scoreboard` job 之后新增 `digest` 步骤或 job：生成摘要、随数据分支一起发布，
   然后用 `GITHUB_TOKEN`（`issues: write`）把 `daily/<北京日>.md` 正文作为评论发到仓库里标题为「每日报告」的 Issue；
   Issue 不存在则创建并置顶（pin）；每月 1 日自动新开一条「每日报告 YYYY-MM」并关闭上月的，避免单条 Issue 评论过多。
   同一天第二班运行时编辑当天已有评论而不是新发一条。
3. **手动入口**：`workflow_dispatch operation=digest` 可只重发当日摘要，不采集不训练。
4. **文档**：README「云端采集」补 `daily/` 目录、Issue 推送与 `operation=digest`；`docs/ARCHITECTURE.md` 补模块与数据分支布局；
   工作流权限只增加 `issues: write`，不加其他权限。
5. **测试**（离线）：用 fixtures 的 `forecast.json`、`summary.json`、`ledger.jsonl` 生成摘要并与期望 Markdown 对比；
   无预测日、partial 日、门槛达成日三种样本；Issue 发布逻辑用拦截器测试创建 / 复用 / 编辑三条路径。

## 不做
- 不做网页服务器、不开 GitHub Pages、不用第三方推送服务，不新增任何 Secret。
- 不在摘要里出现「建议购买」「推荐」字样；门槛未达成时明确写「证据不足」。
- 不重算概率、不改记分板规则（T19）。

## 验收标准
- 真实运行：一次 `operation=digest` 生成 `daily/<北京日>.md` 并在「每日报告」Issue 下出现评论；所有者在手机 GitHub App 收到通知并能读完整内容。
- 同日第二次运行编辑原评论而非新增；跨月自动开新 Issue 的逻辑有离线测试。
- 摘要中的每个数字都能在 `forecast.json` / `summary.json` / `scoreboard.md` 找到来源，PR 描述抽查 5 个数字。
- `pytest -q` 全绿、`ruff check src tests examples` 与 `ruff format --check src tests` 通过；测试不联网。
