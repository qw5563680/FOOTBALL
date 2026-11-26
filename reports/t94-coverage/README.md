# T94 两期归档离线重放

- 26170 使用预测运行 `45537628477-1`，预测截点为北京时间 2026-11-25 11:34:13.605796。该批次早于三条英冠简称批准。归档保留第 11、13 场当时缺少对应关系的状态。[原预测目录](https://github.com/qw5563680/FOOTBALL/tree/c4c52b06d34c94c8c31f4b2933dd6c2dd6f1eeb7/forecasts/2026-11-26/45537628477-1) · [同期赛程快照](https://github.com/qw5563680/FOOTBALL/blob/dba6226289c236482f55a66cbfe69fa9dcfb9771/state/sfc/26170.json) · [原期次报告](https://github.com/qw5563680/FOOTBALL/blob/202d4720095a32e8e1a1e1d1107c050e19389522/sfc/26170/sfc.json) · [后续简称批准 #225](https://github.com/qw5563680/FOOTBALL/pull/225)
- 26171 使用预测运行 `45769244983-1`，预测截点为北京时间 2026-11-26 14:47:36.453413。第 5、9、10、14 场用 #249 已合入的 T93 赛事路由和同批冻结模型离线重算，其余冻结预测保持原值。训练与校准沿用原模型，市场权重为 0。[原预测](https://github.com/qw5563680/FOOTBALL/blob/c4c52b06d34c94c8c31f4b2933dd6c2dd6f1eeb7/forecasts/2026-11-27/45769244983-1/forecast.json) · [同期赛程快照](https://github.com/qw5563680/FOOTBALL/blob/b94cdc0b63995b152922297d78921d7bec474377/state/sfc/26171.json) · [T93 #249](https://github.com/qw5563680/FOOTBALL/pull/249)

脚本从固定提交准备 GitHub 仓库原件，按冻结预测清单及对应批次的采集清单校验 SHA-256。重放把比赛、赛果、来源关联和期次快照载入内存 SQLite。赛前可知时间和简称批准时间均取原截点，基准使用严格早于截点的已核实 90 分钟赛果。26171 旧赔率快照仅供 T93 回放原有正式输出中的市场对照。重放步骤不联网，没有改动归档原件。

```bash
PYTHONPATH=src python reports/t94-coverage/replay.py --input /tmp/t94-replay-input --fetch
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 PYTHONPATH=src python reports/t94-coverage/replay.py \
  --input /tmp/t94-replay-input --output /tmp/t94-replay-output.json
```

## 实际输出

| 期次 | 正式预测 | 基准 | 总覆盖 |
| --- | ---: | ---: | ---: |
| 26170 | 10 | 4 | 14/14 |
| 26171 | 13 | 1 | 14/14 |

| 场次 | 正式预测缺口 | 截点前可用赛果 | 基准主胜 / 平 / 客胜 |
| --- | --- | ---: | --- |
| 26170 第 1、2 场 | 亚冠尚无正式模型，胜负彩与库内比赛身份也待核对 | 全库 1,373 场 | 46.39% / 22.14% / 31.46% |
| 26170 第 11、13 场 | 当时缺少英冠简称对应，后续已按 #225 批准 | 英冠 78 场 | 47.44% / 26.92% / 25.64% |
| 26171 第 3 场 | 同联赛、同开球的候选 `apif:1636298` 有球队仍是占位编号 `apif:team:646`，待核实身份 | 欧罗巴 46 场 | 36.96% / 17.39% / 45.65% |

两个截点的 28 场逐场校验了三项概率之和。有基准的场次在同期正式胜负彩诊断里仍没有球队模型概率，沿用三选，未进入任九单选或复式候选。11 月 25 日当班竞彩目录汇总中，亚冠精英有 2 场未配置正式模型。全量概率和逐场待办见 [实际运行产出](replay-output.json)。这些结果用于离线验收，正式上线后由 Claude 核对连续三期胜负彩和连续三个竞彩销售日。
