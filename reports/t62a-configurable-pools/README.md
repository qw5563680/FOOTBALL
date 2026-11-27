# T62-A 模型池离线验收

## 实际比赛归档

在两份独立副本上用 `cloud_predict.run` 跑一遍正常的恢复、训练和冻结路径。从同一归档原件取 `feed_manifest.json`、`matches/` 和 `state/`，归档校验会检查各文件的 SHA-256。两版共享同一个绝对路径的 `data/model/params.json`，固定批次名 `replay-t62a`、代码环境和时间。预测截点为北京时间 2026-11-26 14:47:36.453413（`2026-11-26T06:47:36.453413Z`），与 T94 的 26171 样本相同。基线 `2d7510f7b20eff989f03940b376b5356df33163e`。[固定采集归档](https://github.com/qw5563680/FOOTBALL/tree/b94cdc0b63995b152922297d78921d7bec474377) · [原预测回放记录](../t94-coverage/README.md)

原归档清单 SHA-256 为 `6516caa2b781f4b5a93956da101d1e7c1af775dbb67ff0190c27364ddc2a4800`。参数 SHA-256 为 `12cafa6c60adc19a6629da74f6cc40d360cb8c34ef3997da1f26a01099dfa888`。对两版保存原始模型文件哈希、冻结目录文件哈希及逐场计数。`coverage.json` 单独比较原因字段。

这个对照比较相同截点的一次真实拟合与冻结。两次均把训练命令的冒烟回测窗口设为截点前一微秒，省去重复回测折；拟合使用完整的原配置训练语料，两个副本的训练参数相同。56 天冒烟回测没有用作本次证据。

两版均完整训练成功，恢复库 SHA-256 均为 `a25ab2505fee16f5be4c00e2ac11dde28baa9fde2ab83be8299a71d60939245c`。**22 个已配置模型文件逐字节一致**，包括原 15 联赛、欧国联和欧战。欧战用 28,133 场训练，模型 SHA-256 为 `e5b7dd124172ae2a70aaa73db27169edd1250a8c3eb5e7e169d0a218a564356a`。

| 北京开球日 | 已出预测 | 欧战预测 | 排除 | `forecast.json` SHA-256，两版相同 |
| --- | ---: | ---: | ---: | --- |
| 2026-11-26 | 0 | 0 | 10 | `f7d08c25698f9f30107d15a29080c15078b8d761c2977449e308026985e5fa7c` |
| 2026-11-27 | 16 | 15 | 0 | `66fceeb6eff041d1f23581df4eac2e836f262bef309963593a686b97105a8325` |
| 2026-11-28 | 10 | 0 | 0 | `157ddc1a5a9b79d07f022eaff8c321741dcfbdc4dc6eafad8ebe3841427d1b40` |

三个目录的 `manifest.json`、`report.csv` 和 `report.md` 同样逐字节一致。逐文件比较结果见 [SHA256SUMS](SHA256SUMS)。

覆盖清单里的周四001 `jc:2043015`（亚冠乙）从「该联赛没有配置正式模型」改为「球队尚未核实与接口历史的身份绑定」。比较两版全部覆盖行，该场只有原因和后续事项两字段不同；比赛、预测覆盖和概率字段一致。

每个工作树用相同命令运行。`REPLAY_ARCHIVE` 指向各自的新副本，`REPLAY_DB` 指向尚不存在的独立目录，`REPLAY_PARAMS` 是两次共用的同一个参数路径。

```bash
PYTHONHASHSEED=0 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 PYTHONPATH=src python - <<'PY'
from datetime import datetime, timedelta
from os import environ
from pathlib import Path

from zucai import cloud_predict

cutoff = datetime.fromisoformat('2026-11-26T06:47:36.453413+00:00')
cloud_predict._now = lambda: cutoff
original_train = cloud_predict.train

def train_once(**kwargs):
    kwargs['since'] = cutoff - timedelta(microseconds=1)
    return original_train(**kwargs)

cloud_predict.train = train_once
cloud_predict.run(
    Path(environ['REPLAY_ARCHIVE']), Path(environ['REPLAY_PARAMS']), 'replay-t62a',
    sporttery_status='success', feed_commit='b94cdc0b63995b152922297d78921d7bec474377',
    production_state=Path(environ['REPLAY_DB']),
)
PY
```

## 边界与日报

- 训练范围漏掉目标赛事时拒绝配置；两个池声明同一个目标时拒绝冲突。训练范围可以共用。
- 样本的两个交手组在桥接赛果的可知时间等于截点时仍被拒；下一截点桥接赛果已经可知时才拟合成功。目标不在池内或球队缺少已核实交手连接时拒绝预测。
- 两个样本池经训练命令、模型登记、赛前预测和报告校验成功。两场分别使用所属池的模型，均标 P2 实验，`bet_allowed=false`。
- 欧战离线样本用同一语料 45 条训练记录重放，生成 14 场预测。欧战模型与 `forecast.json`、`report.csv`、`report.md`、`manifest.json` 逐字节一致。模型 SHA-256 `93eb7bcf9a8b2e0f72176932d537253eeb5fff5b26a861cc60d757ad93860f16`，`forecast.json` SHA-256 `62a048155227c7bc403ece6d0b7c5a1ae877ea07063eaa96d6a2c4e5bf0a988f`。
- 竞彩样本经归档恢复、定时预测、发布回执和日报渲染，六场均有基准概率。周二001、004、005、006 标球队绑定待核，周二002 标无截点前接口历史，周二003 标未配置模型池。其中 004 检查跨赛事别名，005 和 006 检查重复供应商球队编号。旧日报字节校验通过。

```bash
pytest -q tests/test_t62_configurable_pools.py tests/test_pooled_pipeline.py tests/test_t94_digest_coverage.py
```

第一班正式运行由 Claude 在合并后验收。
