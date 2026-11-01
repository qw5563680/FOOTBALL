# FOOTBALL · 中国足彩比赛分析与预测

用公开数据对中国体育彩票的足球玩法做数据分析和结果预测。核心是一个
**每场比赛的比分概率分布模型**：有了比分分布，竞彩胜平负、让球胜平负、
比分、总进球、半全场，以及 14 场胜负彩/任选 9 场，都可以从同一个分布推导出来。

> 本项目仅做数据分析与概率估计，不构成任何投注建议。

## 支持的玩法

| 玩法 | 预测输出 | 数据来源 |
| --- | --- | --- |
| 竞彩 胜平负 (had) | 主胜 / 平 / 客胜概率，对比官方赔率算期望值 | 中国竞彩网 |
| 竞彩 让球胜平负 (hhad) | 带让球线的三项概率 | 中国竞彩网 |
| 竞彩 比分 (crs) / 总进球 (ttg) / 半全场 (hafu) | 由比分分布推导 | 中国竞彩网 |
| 传统 14 场胜负彩 / 任九 | 每期 14 场的胜平负概率表与组合建议 | 体彩官网 / 500.com |

## 技术栈

- Python 3.11+，命令行工具 `zucai`
- SQLite 存储（单文件 `data/zucai.db`），SQLAlchemy 建模
- `requests` + `lxml` 抓取，抓到的原始页面落盘到 `data/raw/` 便于回放
- `pandas` 做特征，`scikit-learn` / `scipy` 做模型（起步用 Dixon-Coles 泊松模型，再加梯度提升）
- `pytest` 测试，抓取逻辑全部基于 `data/fixtures/` 里的离线样本测试

## 快速开始

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
zucai --help
zucai db init                         # 建表，已有比赛和快照会保留
zucai fetch jc --date 2026-11-01      # 抓取该竞彩编号日在售赛程及各玩法报价
pytest -q
```

数据库默认位于 `data/zucai.db`。可以设置 `ZUCAI_DB`，也可用
`zucai db init --path /path/to/football.db` 明确指定文件。
同一场比赛可以保存不同采集时间的赔率和不同模型版本的预测。

以下命令随相应任务合并后提供：

```bash
zucai fetch results --days 30         # 回填已完赛结果
zucai features build                  # 生成特征
zucai train --model dixon-coles       # 训练
zucai predict --date 2026-11-01       # 输出预测表（markdown + csv）
```

## 目录结构

```
src/zucai/          Python 包
  cli.py            命令行入口（子命令：fetch / features / train / predict / report）
  db.py             SQLite 连接与建表
  models.py         SQLAlchemy 表定义
  sources/          各数据源抓取器（sporttery、c500 ...）
  features/         特征工程
  predict/          模型：dixon_coles、gbdt ...
  report/           预测结果输出（markdown / csv）
tests/              pytest，抓取器测试只用 data/fixtures 里的离线样本
data/raw/           抓取落盘的原始响应（不入库，.gitignore）
data/fixtures/      提交到仓库的小样本，供测试使用
docs/               架构、数据源说明、任务单
```

## 协作方式

- 架构与任务单由 Claude（本地）维护在 `docs/` 下。
- 具体实现由 Codex 云端按 `docs/tasks/` 中的任务单逐个完成，每个任务一个 PR。
- Codex 的工作约定见 [AGENTS.md](AGENTS.md)。
