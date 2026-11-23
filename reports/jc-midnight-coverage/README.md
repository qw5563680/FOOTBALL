# T-20261123-02 竞彩销售日跨午夜覆盖验收

原日报在北京时间 11 月 23 日 00:18:43 报漏 8 场。它用的预测运行是 45039288240-1。11 月 22 日 23:57:32 预测时，周一001–008 已经入库，全部在 24 日开球。当时归档有 22、23 和 25 日批次，其中 25 日只用于胜负彩。00:15:40 的发布回执里没有 24 日批次。23:41:20 同步时记录的“销售状态非 1”只说明那次同步跳过。后来 02:04 的预测在首版发报时尚不可用。

[预测输入 226c3709](https://github.com/qw5563680/FOOTBALL/tree/226c37096a6010a75ae41d149b190ed7fe6d09b9/state) · [冻结与回执](https://github.com/qw5563680/FOOTBALL/blob/daae216e043dfef9315b6863bb72469223d22c43/forecast_manifest.json) · [当时的日报](https://github.com/qw5563680/FOOTBALL/blob/daae216e043dfef9315b6863bb72469223d22c43/daily/2026-11-23/45039288240-1.md)

## 离线回放

在仓库根目录运行。SNAPSHOT 指向 226c3709 的本地采集归档。需要 feed_manifest.json、state 下的 teams、matches、results 三个 JSONL 和 state/audit 下 jc_catalog、jc_skipped 的 2026-11 CSV。脚本核验固定清单哈希及各文件哈希，只在临时目录训练、导出和渲染。

```sh
PYTHONPATH=src:tests python reports/jc-midnight-coverage/replay.py "$SNAPSHOT"
```

七个目标联赛各取截点前已有赛果的最近至多 250 场，按项目参数训练。在历史预测时间冻结，用同一历史发布时刻生成测试回执，然后在历史发报时间读档。回放检验覆盖范围和发布规则；缩小过的训练集所算概率仅供本次试验。单独的射正等技术统计实验未加载 Release 数据。

```text
跨午夜批次 ['2026-11-24'] 已预测 8
应有 8 场、已预测 8 场、缺 0 场。
```

周一001 至 008 均显示北京时间 11 月 22 日 23:57:32 生成。新逻辑仍只预测已入库的竞彩比赛，同一开球日中不属于跨午夜销售范围的比赛、只有目录没有入库的比赛，都不会因补覆盖而取得资格。日报用 ID 和赛前发布证据挑选冻结预测；历史排除与后续空批次不会覆盖有效行。测试另核对了没有回执、生成时已开球、同编号不同 ID、缺一场和旧目录无销售日等情况。

## 检查记录

```sh
pytest -q -o addopts= -n 4 --dist=worksteal -o tmp_path_retention_policy=failed --basetemp=/tmp/p0-final
ruff check src tests examples reports/jc-midnight-coverage
ruff format --check src tests
git diff --check
```

全量 4966 passed，10510 条依赖警告。Ruff 检查及 diff 空白检查通过。
