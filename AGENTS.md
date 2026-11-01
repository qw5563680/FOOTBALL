# AGENTS.md · 给 Codex 的工作约定

你在为「中国足彩比赛分析与预测」项目工作。先读 `README.md`、`docs/ARCHITECTURE.md`、
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

## 代码约定

- 包名 `zucai`，源码在 `src/zucai/`，使用 `from zucai...` 绝对导入。
- 抓取器统一实现 `zucai.sources.base.Source` 接口：`fetch_raw()` 负责联网并落盘到
  `data/raw/<source>/<yyyy-mm-dd>/`，`parse(raw)` 负责解析为 dataclass 列表，二者分离以便离线测试。
- 时间一律存 UTC 的 ISO 字符串，展示时转北京时间（UTC+8）。
- 比赛主键 `match_id` 用「来源前缀 + 来源 ID」，例如 `jc:2026110101`，避免不同来源撞号。
- 预测输出必须带 `model_name` 和 `model_version`，同一场比赛允许多个模型各存一行。
- 日志用标准库 `logging`，不要 `print`。

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
