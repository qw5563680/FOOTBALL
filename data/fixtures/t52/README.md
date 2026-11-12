# T52：26163 期真实归档回归样本

来源是 `qw5563680/FOOTBALL` 数据分支的不可变提交
[`1b8cc9c697b863d4c92b4e0006f2190d053e1fcb`](https://github.com/qw5563680/FOOTBALL/tree/1b8cc9c697b863d4c92b4e0006f2190d053e1fcb)，
对应 [run 43067904858](https://github.com/qw5563680/FOOTBALL/actions/runs/43067904858)。
本目录从已保存的原件裁剪，没有新增体育数据采集；没有生成、改写球队名称、开球时间或概率。
`provenance.json` 记录完整来源文件 SHA256、裁剪规则和本目录各数据文件 SHA256。

| 文件 | 内容与裁剪 |
| --- | --- |
| `26163.json` | `state/sfc/26163.json` 原始字节，完整 14 场快照与原有 8 场关系 |
| `matches.jsonl` | 原始 `state/matches.jsonl` 中本期 14 个 APIF ID 的原始行 |
| `teams.jsonl` | 上述 14 场涉及的 28 队原始行，名称、别名和 ID 不改 |
| `jc_catalog.csv` | 原始月审计中 6 个 JC ID 在 `2026-11-11T17:39:34.170299Z` 的完整行，包括 poolList |
| `jc_skipped.csv` | 同批 6 条跳过记录，均 `sellStatus=2`、`retained=0`、`reason=sell_status_not_1` |
| `forecast-eight.json` | 原 `forecasts/2026-11-14/43067904858-1/forecast.json` 全部 8 个预测，保留全部预测字段与欧国联模型元信息；仅删其他联赛模型、压缩 JSON 空白 |

CSV 为规范化换行后的完整列投影；JSONL 和期次文件直接复制原始行/字节。
这些裁剪不是全库；“6 个 JC 记录在全库不存在”由下述完整归档恢复验收另证。
最新清单采集在 SFC 快照（17:39:40 UTC）和 500 编号对照（17:39:41 UTC）之前；
清单保留不售状态供审计、入库过滤非售卖场次，是原采集规则的预期行为。
修复针对“对照目标不存在却阻断严格国家队匹配”，不改变销售过滤。

## 离线测试

```bash
PYTHONPATH=src pytest -q tests/test_t52_real_archive.py tests/test_t52_sfc_bridge.py
```

普通回归测试只读本目录，无模型二进制、密钥或联网请求。
其中批次输入测试不冒充真实模型预测；真实预测由下述完整验收单独执行。

## 完整原件重放

先将上述不可变提交通过 `git archive` 解压到独立目录，再执行：

```bash
PYTHONPATH=src python scripts/verify-t52-archive.py \
  --archive /path/to/extracted-1b8cc9c \
  --output /path/to/new-t52-evidence
```

输出目录必须不存在，且必须位于归档目录之外。脚本会：

1. 核对固定来源 SHA256，通过正式 `restore_feeds` 校验/恢复到新临时数据库。
2. 重放原 26163 期、检查 14/14 映射，以及旧 6 条对照与降级原因完整保留。
3. 使用原批次截止时间 `2026-11-11T17:43:58.323712+00:00`，实际调用
   `open_national_fixtures`、`predict_batch`。北京 11 月 13 日输入与实际预测均须有 6 场。
4. 只加载原档案 `models/43067904858-1/欧国联.pkl`，加载前检查固定 SHA256；
   不训练或调参。参数文件 SHA256 也须与原批次相同。14 场实际重放，原 8 场 ID/概率
   转为相同规范的 UTF-8 JSON 字节后直接比较，不四舍五入、不用误差容忍。
5. 把第一场对照故意指向另一场**归档中真实存在**的比赛，必须拒绝并返回身份冲突。
6. 再核对整个输入归档的全部文件 SHA256、恢复数据库 SHA256 和原期次快照均未改变。

完整输出包括摘要、14 场映射证据、6/14 场实际预测、8 场比较字节、冲突样例以及全档案
验收前后 SHA256 清单。输出仅供离线检查：`mode=historical_replay`，生成时间记录实际运行时刻；
不会将本次结果冒充历史赛前发布，不写回正式预测、报告或账本。
原模型二进制保留在上述不可变数据提交，不复制进代码 PR。
