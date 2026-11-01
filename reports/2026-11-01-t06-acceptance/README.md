# T06 实际执行验收 · 2026-11-01

本目录是开发验收，不是购买清单。所有概率为 P2 实验；模型只使用球队历史比分，未获正式采用，BET 禁用。

## 官方真实数据链路

- 从私有数据分支提交 `ddeb76ff75fb23da736745c354db946c24b87107` 校验恢复866场赛程、833条赛果及2319条报价到独立临时库。
- 北京时间22:27:52，在能访问官方接口的临时环境真实执行 `zucai fetch jc`，观察21场在售赛程，新增1131条报价；退出码0。
- 北京时间22:29:35，按同来源jc、同联赛分别训练；英超61场、德甲61场、意甲73场、西甲74场。
- 四模型实际 `available_source` 均为captured，assumed训练占比0，最近可知结果为北京时间21:26:51。
- 北京时间22:38:04，读取比赛CSV执行 `predict`，7场输入得到6场纯模型概率，马赛—图卢兹因未提供法甲模型列明排除。
- 同批 `report` 再导出后，冻结 JSON 与原文件逐字节一致。旧版第一次验收目录 `live-batch/` 保留；最新目录为 `live-batch-v2/`。

[查看最新可读报告](live-batch-v2/report.md) · [原精度JSON](live-batch-v2/forecast.json) · [CSV](live-batch-v2/report.csv) · [SHA256清单](live-batch-v2/manifest.json)

实际命令（临时库路径省略，以环境变量 ZUCAI_DB 指向隔离库）：

```bash
zucai fetch jc
zucai train --league 英超 --source jc --as-of 2026-11-01T14:29:35.253174+00:00 --strict-availability --output models/epl.pkl
zucai train --league 德甲 --source jc --as-of 2026-11-01T14:29:35.253174+00:00 --strict-availability --output models/bundesliga.pkl
zucai train --league 意甲 --source jc --as-of 2026-11-01T14:29:35.253174+00:00 --strict-availability --output models/serie-a.pkl
zucai train --league 西甲 --source jc --as-of 2026-11-01T14:29:35.253174+00:00 --strict-availability --output models/la-liga.pkl
zucai predict --matches matches.csv --model-file models/epl.pkl --model-file models/bundesliga.pkl --model-file models/serie-a.pkl --model-file models/la-liga.pkl --out reports/live-batch-v2
zucai report --batch reports/live-batch-v2/forecast.json --out reports/frozen-rerender
```

输入 `matches.csv`：

```csv
match_id
jc:2042428
jc:2042429
jc:2042430
jc:2042431
jc:2042433
jc:2042434
jc:2042435
```

四份 `train-*.json` 保存实际训练/回测输出，模型文件SHA256在冻结JSON中。模型文件本身留在隔离目录，未提交二进制。
赛前报价来自官方计算器 `getMatchCalculatorV1`，报价时间22:27:52，不表示当前仍有效或售票资格/正式首发已核实。

**验证限制：四份本地真实归档回测的有效预测数均为0。** 此批历史结果是11月1日才实际采集，
不能把captured时间倒填为历史各周已经可知，因此历史折内没有足够合法训练行。这些训练输出不能证明模型准确或优于市场。
当前真实数据仅覆盖约两个月，球队样本少；未输入伤停/正式首发。正EV点估计不等于可执行优势。

## 可公开复现的历史回放

[回放报告](public-replay/report.md) 使用 Football-Data 2019/20 英超原始CSV，SHA256：
`100037618b94f94057400bb02bf6bac4ef74ddaa58cde4b38370839c39caee61`。

```bash
python examples/t06_public_replay.py --out reports/public-replay
# 已有原始CSV时：追加 --csv /path/to/E0.csv
```

380场真实赛果全部保留。2020-01-01T00:00Z截点前实际训练199场，预测Arsenal—Man United：
主胜0.2375927715161751、平0.2421149342171288、客胜0.5202922942666962。
该CSV没有逐场发布时刻，演示显式假设开球后3小时可知，故 mixed/assumed占比100%。
无带时刻官方SP，市场概率和EV为空。实际报告生成于2026-11-01，北京时间22:29:10；明确为historical_replay，不能当作2020年事前预测或未见验证。

## 发布与测试

代码与本次冻件保存于T06私有PR；不会在此步骤启用每日实时采集。未来命令生成的报告须另行提交私有仓库，
现有feeds自动数据归档不包含预测文件。报告是供后续网页读取的数据和可读输出，本次没有发布可视化网站。

独立审查指出的回放数据库标识和跨开球冻结问题均已用失败测试复现并修复。全套离线测试85项通过，Ruff通过。
冻结导出的Git换行转换已关闭，9份已暂存导出文件均再次按原SHA256核对，保证Windows/Linux读取一致。
当前penaltyblog 1.13.1在NumPy 2.5有上游弃用告警；实际预测和测试未失败，未屏蔽告警。
