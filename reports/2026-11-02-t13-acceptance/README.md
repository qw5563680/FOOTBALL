# T13 特征表修正验收（t04-v2）

北京时间2026-11-02执行。基线为main的`d4acd27`，特征版本升为`t04-v2`。
导出866行、84列（43个模型字段、7个独立市场字段、34个元数据字段）。未训练模型。
旧`reports/2026-11-02-t04-acceptance/`文件保持原字节。

## 输入、截点和重建限制

原T04临时SQLite库未保留在本环境。本次按既有`restore_feeds`方法，从
`data/football-feeds`的固定提交`ddeb76ff75fb23da736745c354db946c24b87107`恢复新库；
其`feed_manifest.json`及全部8个文件均通过SHA256校验。没有重新抓取源站或合成缺失报价。

重建库有456支球队、866场jc比赛、833条captured赛果、2319条报价；状态为833 finished、6 cancelled、27 scheduled。
比赛和赛果数量与原T04一致，且重建v1复现其全部43个模型字段；原库未保留，无法逐条比对全部原始赛果。
报价少于原库3450条：缺少`2026-11-01T14:27:52.953051Z`采集批次。
原T04冻结文件中的20场had快照时刻与重建库不同，其中`jc:2042435`、`jc:2042437`的had概率不同。
原T04未导出hhad，无法从旧产物恢复该批hhad报价；本报告的hhad覆盖只代表此固定重建库。

使用原T04同一截点`2026-11-01T16:25:56.611980Z`（北京时间2026-11-02 00:25:56.611980），
分别运行原`d4acd27`的v1构建器和本次v2构建器。重建v1的全部43个模型字段逐值等于原T04冻结表，
休息天数及had覆盖率也完全一致。v2生成时刻为`2026-11-01T19:38:33.930183Z`，与历史截点分开保存。

输入库在v1、v2及独立复核前后SHA256均为：
`796409737164a854fdf70e8f4eb75a5dcc576945f1109146cd1e74ec8da17ac1`。
未上传数据库或预测记录；重建库预测表为空。

## 覆盖率对比

以下都是非空比例；T04没有`market_hhad_line`列，不能把“未导出”解释为0%覆盖。

| 字段 | 原T04冻结v1 | 同一重建库v1 | 同一重建库v2 |
| --- | ---: | ---: | ---: |
| market_hhad_line | 未导出 | 未导出 | 27/866（3.12%） |
| home_rest_days | 25/866（2.89%） | 25/866（2.89%） | 624/866（72.06%） |
| away_rest_days | 24/866（2.77%） | 24/866（2.77%） | 647/866（74.71%） |
| 主客休息天数均非空 | 24/866（2.77%） | 24/866（2.77%） | 566/866（65.36%） |
| 普通had三项完整 | 26/866（3.00%） | 26/866（3.00%） | 26/866（3.00%） |

| 按相同截点分组 | 场数 | v1主/客休息非空 | v2主/客休息非空 | v2 hhad让球线非空 |
| --- | ---: | ---: | ---: | ---: |
| 已到/早于目标开球，历史重建 | 847 | 6 / 6 | 605 / 629 | 8/847（0.94%） |
| 截点时尚未开球 | 19 | 19 / 18 | 19 / 18 | 19/19（100.00%） |

主队新增599个、客队新增623个非空间隔；原已非空的休息天数全部保持原值。
新增覆盖来自赛程：同来源、同球队、跨赛事，取开球严格早于目标开球的最近一场并排除cancelled，
不再要求该场已有Result或available_utc。meta_home_rest_match_id/meta_away_rest_match_id记录所用比赛ID。
赛程开球可以在as_of与目标开球之间。当前赛程版本不能证明其在历史as_of的可知时间；该间隔也不能证明库外杯赛等完整真实休息期。

had和hhad各自选择严格早于min(as_of,目标开球)的最新完整快照；hhad的h/d/a必须同一时刻、同一整数线，
概率各自按逆价比例归一化。market_hhad_line以可空Int64导出，所有7个market字段仍不进入模型白名单。

## 逐行复核与测试

- v2与同库v1的其余41个模型字段、3个had概率及赛果窗口证据逐值一致；模型43列白名单及顺序不变。
- 核对866个目标的6062个赛果窗口、533条实际证据；历史开球和available_utc仍严格早于有效截点。
- 26个had、27个hhad快照均与库内各自完整组一致；逐项核对时刻、价格、整数线和归一化概率。
- 对双方1732个休息窗口复核最近比赛ID、同源同队、非取消状态、目标开球前边界和间隔：1271个有值、461个为空。
  其中1222个有值窗口对应的赛果在有效截点不可用，验证其不再依赖赛果采集。
- Parquet往返、metadata及manifest哈希均通过；新回归先在旧实现下确认失败，再验证修正通过。
- `python -m pytest -o addopts='' -q`：261 passed；401条既有penaltyblog/NumPy 2.5弃用警告。
- `ruff check src tests`、`ruff format --check src tests`及`git diff --check`通过；独立代码审查无发现。

实际提取命令（Python 3.12，dev依赖含pyarrow 26.0.0）：

```bash
PYTHONPATH=src work/venv/bin/python -m zucai.cli features build --db work/t13-input/restored/zucai.db --as-of 2026-11-01T16:25:56.611980Z --out work/t13-acceptance/features.parquet
PYTHONPATH=src work/venv/bin/python work/t13-acceptance/verify_acceptance.py --repo-root . --artifact-dir work/t13-acceptance --input-dir work/t13-input
```

## 归档位置与哈希

按AGENTS.md第8条，本目录只提交本README和[SHA256SUMS](SHA256SUMS)，总量小于200 KB。
完整产物存放于`data/football-feeds`提交`2b7b76eb3141ff9309daba5cdd1d027deb9d10fd`的
[`acceptance/2026-11-02-t13/`](https://github.com/qw5563680/FOOTBALL/tree/2b7b76eb3141ff9309daba5cdd1d027deb9d10fd/acceptance/2026-11-02-t13)，不进入代码PR：

- [v2 Parquet](https://github.com/qw5563680/FOOTBALL/tree/2b7b76eb3141ff9309daba5cdd1d027deb9d10fd/acceptance/2026-11-02-t13/t13-acceptance/features.parquet)、[完整metadata](https://github.com/qw5563680/FOOTBALL/tree/2b7b76eb3141ff9309daba5cdd1d027deb9d10fd/acceptance/2026-11-02-t13/t13-acceptance/features.metadata.json)、[导出manifest](https://github.com/qw5563680/FOOTBALL/tree/2b7b76eb3141ff9309daba5cdd1d027deb9d10fd/acceptance/2026-11-02-t13/t13-acceptance/features.manifest.json)。
- [完整验收摘要](https://github.com/qw5563680/FOOTBALL/tree/2b7b76eb3141ff9309daba5cdd1d027deb9d10fd/acceptance/2026-11-02-t13/t13-acceptance/acceptance-summary.json)、[独立验证脚本](https://github.com/qw5563680/FOOTBALL/tree/2b7b76eb3141ff9309daba5cdd1d027deb9d10fd/acceptance/2026-11-02-t13/t13-acceptance/verify_acceptance.py)、[归档说明与复现步骤](https://github.com/qw5563680/FOOTBALL/tree/2b7b76eb3141ff9309daba5cdd1d027deb9d10fd/acceptance/2026-11-02-t13/README.md)。
- 同库v1表与证据、输入manifest及8个归档文件、原v1源码、v2源码快照、恢复脚本、测试与复核日志均保存在同目录。

本目录SHA256SUMS与数据分支同名清单逐字节一致，路径以归档目录为根；下载该目录后运行`sha256sum -c SHA256SUMS`。
清单覆盖完整产物和复现证据；数据库SHA256仅作为只读运行证明，不表示上传了库文件。
本次不作模型准确性结论，P2/BET状态不变。
