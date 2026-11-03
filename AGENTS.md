# AGENTS.md · 给 AI 助手（Codex / Claude）的工作约定

你在为「中国足彩比赛分析与预测」项目工作。本文件对参与项目的所有 AI 助手生效：
Codex 负责实现任务单，Claude 负责审查与质量门（见「审查流程」）。先读 `README.md`、`docs/ARCHITECTURE.md`、
`docs/DATA_SOURCES.md`，再读分配给你的 `docs/tasks/Txx-*.md`。

## 环境

- Python 3.11+。安装：`pip install -e ".[dev]"`（环境已配置时可跳过）。
- 测试：`pytest -q`。提交前必须全绿。
- 代码风格：`ruff check src tests` 与 `ruff format src tests` 不报错。
- 云端环境默认可能没有外网。**所有抓取代码的测试必须只依赖 `data/fixtures/` 里的离线样本**，
  绝不能在测试里真实联网。需要新样本时，把真实响应裁剪到几十 KB 以内再放进 fixtures。

## 任务规则

1. 一个任务单 = 一个分支 = 一个 PR。分支名 `codex/Txx-短描述`，PR 标题以 `Txx:` 开头。
2. 只做任务单「范围」里写的事；「不做」里列的内容即使顺手也不要碰。
3. 任务单里标注「需验证」的接口或页面结构，先用 fixtures 里的样本或真实请求确认，
   发现与文档不符时以实际为准，并在 PR 描述里写明差异，同时更新 `docs/DATA_SOURCES.md`。
4. 新增命令行子命令要在 `README.md` 的快速开始里补一行用法。
5. 数据库表结构变化必须同时更新 `docs/ARCHITECTURE.md` 的「数据模型」一节。
6. 不要提交 `data/raw/`、`data/*.db`、`.venv/`，`.gitignore` 已覆盖。
7. 不要引入重型依赖（深度学习框架、浏览器自动化）。抓取只用 `requests` + `lxml`。
   如果某个页面必须执行 JS 才能拿到数据，停下来在 PR 里说明，不要自行上 Playwright。
8. 验收产物只把 `README.md` 和 SHA256 清单提交到 `reports/`；逐场证据、全量 JSON、parquet、模型文件等
   大体积文件放 `data/football-feeds` 分支或 Release 附件，并在 README 里写明位置与哈希。单个 PR 新增的
   `reports/` 内容不超过 200 KB。
9. 遵守各站 robots.txt：实现前先读一次并把结论写进 `docs/DATA_SOURCES.md`，禁止路径不请求。
10. 正式 API key 只存在于 GitHub Actions Secrets；任何开发环境（含 Codex、Claude 的云端会话）
    不得用正式 key 发真实请求。权限探测只能通过 `operation=probe` 由审查者触发，测试一律使用离线 fixtures。
    需真实验证的接口、计费或权限结论由审查者在 Actions 留下证据后回填，不用合成样本冒充实测。

## 代码约定

- 包名 `zucai`，源码在 `src/zucai/`，使用 `from zucai...` 绝对导入。
- 抓取器统一实现 `zucai.sources.base.Source` 接口：`fetch_raw()` 负责联网并落盘到
  `data/raw/<source>/<yyyy-mm-dd>/`，`parse(raw)` 负责解析为 dataclass 列表，二者分离以便离线测试。
- 时间一律存 UTC 的 ISO 字符串，展示时转北京时间（UTC+8）。
- 比赛主键 `match_id` 用「来源前缀 + 来源 ID」，例如 `jc:2026110101`，避免不同来源撞号。
- 预测输出必须带 `model_name` 和 `model_version`，同一场比赛允许多个模型各存一行。
- 日志用标准库 `logging`，不要 `print`。

## 审查流程

实现者与审查者分离：Codex 在 `codex/Txx-*` 分支实现并开 PR，Claude 审查；两边不改对方的分支。
仓库所有者负责最终合并。

1. **开 PR 后即进入审查**。实现者在 PR 描述里按下方模板写清验证结果即可，不必另行通知：
   审查者每小时巡检一次 `codex/*` 分支的 open PR，自动订阅并审查；之后的新提交、CI 结果和评论都由审查者自动跟进。
   实现者修复后在对应评论线程里回复「已在 <commit> 修复」或说明不采纳的理由，审查者会被该回复唤醒并复审。
2. **审查内容**：读完整 diff，本地重跑 `pytest -q` 与 `ruff check src tests`，
   逐条对照任务单的「范围 / 不做 / 验收标准」。本项目重点核查：
   - 赔率、收盘价或任何市场信息是否进入模型输入、先验、校准或选参；
   - 训练、回测是否使用了截点之后才可知的数据（赛果可知时间、跨赛季泄漏）；
   - 抓取是否遵守 robots.txt 与请求间隔，测试是否只用 `data/fixtures/` 离线样本；
   - `reports/` 新增体积是否超过 200 KB，大文件是否改走数据分支或 Release 附件；
   - 数据库表结构或命令行子命令变化是否同步更新了 `docs/ARCHITECTURE.md` 与 `README.md`。
3. **审查结论写在 PR 上**：以 GitHub review 评论提出，按「必须修 / 建议修 / 仅说明」分级，
   每条给出文件、行号和复现方式。实现者修复后在同一 PR 推新提交；不采纳的意见要在 PR 里说明理由。
4. **合并条件**：CI 绿、审查者无未解决的「必须修」、所有者确认。
   合并一律用 GitHub 的 Merge 按钮，不在本地 merge 后直接推 main，否则 PR 不会自动关闭。
5. **审查记录归档**：涉及模型、数据来源或回测规则的 PR，审查者把审查记录写到
   `docs/reviews/<日期>-txx-review.md`（范围、发现的问题与修复、验证结果、遗留限制），
   格式沿用已有记录。纯文档或小修 PR 可以不归档。
6. **审查者自己的改动**走 `claude/*` 分支并同样开 PR 接受检查；审查者不自动合并任何 PR。

## PR 描述模板

```
## 任务
Txx 标题

## 做了什么
- ...

## 验证
- pytest 输出摘要
- 如有真实联网验证，写明日期和样本量

## 与任务单的差异 / 遗留问题
- ...
```
