# T-20261125-01 · 澳客备用赛程离线验收

回放使用 500 网 26171 冻结页、运行 45417504091 的澳客按期页及 HTTP 405 原件、11 月 17 日体彩公告和 #223 的逐场证据。#223 覆盖 37 个异名字段，补上 4 场同名比赛的赛程证据后，共核对两期 28 场。测试固定时钟，HTTP 由本地样本响应。原件和裁剪件的哈希及 28 场证据随测试保存。[样本与溯源](../../data/fixtures/okooo_offline/README.md) · [逐场证据](../../data/fixtures/okooo_offline/schedule_evidence.json) · [#223](https://github.com/qw5563680/FOOTBALL/pull/223) · [探测附件](https://github.com/qw5563680/FOOTBALL/actions/runs/45417504091/artifacts/14933261712)

| 回放场景 | 结果 |
| --- | --- |
| 500 网 26171 同一期页面正常 | 沿用 500 网 14 场，澳客页面迭代器未读取。 |
| 500 网缺页，26170 澳客胜负彩按期页真实返回 405 | 同轮任九给出 14 场候选，保留胜负彩 HTTP 405 和项目公告核对待核的原因。 |
| 500 网缺页，26171 澳客任九按期页真实返回 405 | 同轮胜负彩给出 14 场候选，保留任九 HTTP 405 和项目公告核对待核的原因。 |
| 只有 405，或两个玩法的完整页面一致 | 只有 405 时无候选。完整页面一致时输出一份 14 场候选，选胜负彩页并保留其来源。 |
| 返回别期、首页、只有 13 场或场序重复 | 无候选，分别记录请求期号或按期路径不符、场序不完整或重复。 |
| 主客颠倒、开球变动、澳客比赛编号变动、停售不符或缺独立赛程证据 | 无候选，待核原因指明字段，涉及单场时带场序。 |
| 两张完整按期页冲突 | 无候选。分别实跑了比赛编号、开球、停售和主客冲突，两种输入顺序均拒绝。 |
| 到达 26170 官方停售边界 | 北京时间 11 月 25 日 17:29:59 仍有 14 场候选。17:30:00 及之后无候选，记为「已到停售时间」。 |

每场澳客候选保留原文和比赛编号，并列公告主客、库内比赛 ID 与赛程来源。按原始队名运行项目的正式公告核对，26170 的 19 项、26171 的 18 项仍为待核。候选筛选和输出均在测试目录内。

```sh
pytest -q -s -o addopts= tests/test_okooo_offline_acceptance.py tests/test_okooo_public_probe.py --tb=short
pytest -q -o addopts= -n 4 --dist=worksteal -o tmp_path_retention_policy=failed --basetemp=/tmp/okooo-offline-suite
ruff check src tests examples
ruff format --check src tests
git diff --check
```

定向 34 项通过，其中新增离线验收 23 项。全量 5027 项通过，Ruff、格式及差异空白检查通过。
