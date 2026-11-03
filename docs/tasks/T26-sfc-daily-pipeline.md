# T26 · 14 场胜负彩接入每日预测与摘要

## 背景
T07 已有 `zucai fetch sfc`（从 500.com `/sfc/` 取当期 14 场对阵，robots 允许，见 T12）和 `zucai report sfc`
（用 T06 冻结的纯模型批次输出 14 行胜平负概率表与 ≤64 注的复式建议），但只能手工运行；T17 的每日自动预测与 T19 记分板都没有它。
14 场胜负彩是奖池分配（pari-mutuel），没有固定赔率，不能像竞彩那样算期望值与虚拟下注；但每期的 14 场概率表和「单选命中数」仍然是
检验模型的好样本，也是所有者想每天看到的内容。

## 目标
1. 每天自动抓当期 14 场对阵，映射到库里的比赛，用当天冻结的预测批次生成 14 行概率表和复式建议，进入 T25 的每日摘要。
2. 期次截止并结算后，自动记录每期单选命中数与复式是否覆盖全中，作为模型校验的诊断，不设虚拟资金账户。

## 范围
1. **采集**（`src/zucai/cloud_feeds.py` sync 路径、`sources/c500.py`）：`sync` 在自建 runner 上增加一步 `fetch sfc`（当期 1 次请求，
   若 500.com 对托管机不限制则可移到 apis 腿，需审查者 probe 确认），结果写 `sfc_issues` / `sfc_issue_matches` 并随数据分支归档
   `state/sfc/<issue>.json`；重复抓同一期不重复入库；每期映射不上的比赛写 `state/audit/sfc_unmatched.csv`，不新建球队。
2. **预测衔接**（`src/zucai/cloud_predict.py` 或 `report/sfc`）：predict job 结束后，对所有未截止的期次用当天最新 `forecast.json`
   生成 `sfc/<issue>/report.md|report.csv|sfc.json`（复用 T07 的 `report sfc` 逻辑），写入数据分支；复式注数上限默认 64。
   没有模型的比赛在表里标「无模型」，复式建议对该场按三选处理并在报告里说明。
3. **结算与诊断**（`src/zucai/scoreboard/`）：期内 14 场全部有赛果后，记录 `sfc/settled.jsonl`：期号、单选命中数、复式是否全中、
   各场概率与结果；`summary.json` 增加 14 场诊断小节（期数、平均命中数、命中数分布），记分板 `scoreboard.md` 末尾加一节。
   **不建虚拟账户、不估算奖金**；文案保持「诊断」口径。
4. **摘要**：T25 的每日摘要加「14 场胜负彩」小节：当期期号、截止时间、14 行概率表、复式注数与覆盖概率、上一期命中数。
5. **文档与测试**：README 快速开始补自动化说明；`docs/ARCHITECTURE.md` 补 `sfc/` 布局与诊断字段；`docs/DATA_SOURCES.md` 补 500.com
   `/sfc/` 的每日请求次数；离线测试覆盖采集幂等、映射与未匹配审计、报告生成、结算诊断、摘要小节。

## 不做
- 不做奖金估算、不做任九单独优化、不设虚拟资金账户。
- 不改模型、不改 T19 竞彩账户规则。
- 不请求 500.com 被 robots 禁止的路径（`/jczq/?` 等，见 T12）。

## 验收标准
- 真实运行（审查者触发）：一次 `sync` 抓到当期并入库；一次 `predict` 后数据分支出现 `sfc/<issue>/report.md`，14 行表与复式注数合理；
  摘要出现 14 场小节；结算后 `sfc/settled.jsonl` 有一期记录。
- 重复运行不重复入库、不重复结算。
- `pytest -q` 全绿、`ruff check src tests examples` 与 `ruff format --check src tests` 通过；测试不联网。
