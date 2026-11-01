# T06 · 预测报告与赔率对比

## 目标
`zucai predict` 对在售比赛生成**纯模型**预测并入库，`zucai report` 输出可读的 markdown 和 csv，
把模型概率与官方赔率并列，标出按模型概率计算有正期望值的选项。赔率只作比较基准，不参与预测。

## 范围
1. `zucai predict [--date YYYY-MM-DD] [--model dixon-coles]`：加载模型，对该日期在售比赛计算比分矩阵与各玩法概率，写 `predictions`。写入的永远是纯模型结果。
2. 赔率作为对比基准：`p_market` 来自最新 `had` 快照去抽水归一化，只出现在报告的对比列和期望值计算里，不写入 `predictions`。
3. 融合仅作实验开关：`--market-weight w` 默认 `0`（关闭）。显式给出 `w > 0` 时，`p = w * p_market + (1 - w) * p_model`
   以独立的 `model_name`（如 `dixon-coles+market`）另存一行，绝不覆盖或替代纯模型行；报告默认只展示纯模型行。
4. `zucai report [--date] [--out reports/YYYY-MM-DD.md]`：
   - 每场一行：编号、联赛、对阵、开赛（北京时间）、模型胜平负概率、赔率隐含概率、官方赔率、期望值 `p_model * odds - 1`；
   - 期望值 > 0.05 的选项加粗；
   - 附比分玩法前 5 个最可能比分；
   - 同时输出同名 csv。
5. 测试：固定一个比分矩阵，验证报告里的概率、期望值与格式；验证默认运行时 `predictions` 里没有带 `+market` 的行。

## 不做
- 不做 14 场（T07）。
- 不做任何自动投注或资金管理。
- 不默认启用融合，不把赔率写进模型输入。

## 验收标准
- 真实跑一次 `fetch jc → predict → report`，把生成的 markdown 片段贴进 PR 描述。
- `select distinct model_name from predictions` 在默认流程下只有纯模型名。
