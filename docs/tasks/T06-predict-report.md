# T06 · 预测报告与赔率期望值

## 目标
`zucai predict` 对在售比赛生成预测并入库，`zucai report` 输出可读的 markdown 和 csv，标出与官方赔率相比有正期望值的选项。

## 范围
1. `zucai predict [--date YYYY-MM-DD] [--model dixon-coles]`：加载模型，对该日期在售比赛计算比分矩阵与各玩法概率，写 `predictions`。
2. 市场融合：`p_final = w * p_model + (1 - w) * p_market`，`w` 默认 0.6，可用 `--market-weight` 调整；`p_market` 来自最新 `had` 赔率。
3. `zucai report [--date] [--out reports/YYYY-MM-DD.md]`：
   - 每场一行：编号、联赛、对阵、开赛（北京时间）、模型胜平负概率、官方赔率、期望值 `p * odds - 1`；
   - 期望值 > 0.05 的选项加粗；
   - 附比分玩法前 5 个最可能比分；
   - 同时输出同名 csv。
4. 测试：固定一个比分矩阵，验证报告里的概率、期望值与格式。

## 不做
- 不做 14 场（T07）。
- 不做任何自动投注或资金管理。

## 验收标准
- 真实跑一次 `fetch jc → predict → report`，把生成的 markdown 片段贴进 PR 描述。
