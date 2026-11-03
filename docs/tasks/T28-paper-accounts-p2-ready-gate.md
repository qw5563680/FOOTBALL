# T28 · 虚拟账户只对「P2 可用」模型下注（小修）

## 背景
T19 的五个虚拟账户（`src/zucai/scoreboard/paper.py` `advance_ledger`）只看预测行的 `status / mode / 冻结 SP`，
不看模型就绪标签 `p2_ready`。T21 合入后，欧战联合模型（P2 实验）每天给欧冠 / 欧罗巴 / 欧协联在售场次出预测，
而部分欧战球队的历史身份尚未对齐（T29 处理），个别场次概率明显偏离。2026-11-03 的验收 run 41699931511
记分板一次为 15 场欧冠新开 107 张虚拟票、合计 12,029.54 虚拟元，凯利账户 B 在 5 场全部打到 2% 上限（`jc:2042503` 边际 0.996）。
荷甲、英冠、瑞超、挪超、法乙、日职、芬超、美职等 8 个 `p2_ready=false` 的国内联赛也一直在下注。
所有者 2026-11-04 决定：虚拟账户只对「P2 可用」的模型下注；实验模型的预测继续冻结、评分、进摘要，但不进账户。

## 目标
1. `p2_ready` 为 false（或缺失）的预测行不生成任何注单（A/B/C/D/E 五个账户、三个门槛一致），只记一条跳过记录。
2. 评分（`score.py`）、摘要、冻结报告对实验模型完全不变；账本既有记录与资金历史不变。

## 范围
1. **`paper.py` `advance_ledger`**：在逐行处理批次时，`row.get("p2_ready") is not True` 的行与「无冻结 SP / 生成时已有赛果」同等处理：
   对 A/B/C 三个账户 × 三个门槛各记 `skip(reason="model_not_p2_ready")`，并且**不**进入 `candidates` / `favourites`
   （因此 C 热门对照组和 D/E 串关也不会用到它）。D/E 当日候选不足时沿用既有 `insufficient_candidates`。
   判定只用冻结预测行自带的 `p2_ready` 字段，不重新计算 P2、不读 params.json、不看联赛名单。
2. **`render.py`**：`reasons` 字典补 `"model_not_p2_ready": "模型为 P2 实验，不下注"`。
3. **`daily_digest`**：虚拟账户一节若有当日跳过统计，补显示该原因；没有则不改。
4. **幂等与历史**：已存在的注单（含本次验收 run 写入的欧冠票）保持冻结、照常结算；新规则只影响规则生效后处理的新批次。
   `record_key("skip", ...)` 去重保证重复运行不追加。
5. **测试**（`tests/test_scoreboard_paper.py` 等，只用 `data/fixtures/`）：
   - 同一批次内一行 `p2_ready=true`、一行 `p2_ready=false`（有完整冻结 SP、有正边际）：前者正常开票，后者五个账户都无票、A/B/C 各有 `model_not_p2_ready` 跳过记录；
   - `p2_ready` 字段缺失的旧冻件按 false 处理；
   - 仅剩实验行时 C 不开票、D/E 记 `insufficient_candidates`；
   - 既有账本含实验行注单时，重跑不删除、照常结算，资金历史不变；
   - 重复运行账本字节不变。
6. **文档**：`docs/ARCHITECTURE.md` T19 一节 `paper.py` 段补一句规则；`README.md` 记分板 / 虚拟账户说明补一句。

## 不做
- 不改评分、三列指标、校准、P2 判定或任何模型代码；不改 `cloud_predict`、冻结格式、摘要预测表。
- 不删除、不改写、不回滚账本中已有的任何记录（包括实验模型已开的票）。
- 不新增「实验账户」或第二套账本；不改五个账户的策略、门槛、资金规则。
- 不碰数据分支内容；`reports/` 不新增。

## 验收标准
- `pytest -q` 全绿（含上述新增用例）；`ruff check src tests examples` 与 `ruff format --check src tests` 通过。
- 审查者在 Actions 真实跑一次 `operation=scoreboard`：账本不新增实验模型注单，`summary.json` 的 `audit.paper_skips`
  出现 `model_not_p2_ready`，既有注单与资金数字不变；下一次定时运行的摘要虚拟账户一节正常。
