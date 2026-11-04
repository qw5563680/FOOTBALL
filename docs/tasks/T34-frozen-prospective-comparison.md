# T34 · 冻结前瞻预测、新旧并行比较与采用决定（阶段 E）

> 2026-11-05 改写。原稿前提是「T33 至少一个候选不劣于基线」，T33 第一轮（PR #64，审查记录
> `docs/reviews/2026-11-04-t33-review.md`）的结果是射正 / xG 强度两个候选在历史回放上都**略差**于原模型
> （fd 共同样本 LL 差 +0.006 [−0.004, +0.016]，apif +0.011 [−0.004, +0.027]），前提不成立。
> 所有者 2026-11-05 决定：**同时做**「第二轮离线实验（候选二）」与「实验模型每日并行跑、只攒证据不下注」，
> 并把实验大文件从数据分支改放 Release 资产。本任务拆成两个 PR：**T34a**（先做）与 **T34b**。

## 前置依赖
T33 已合入（`0b5a20b`）；T17 / T19 / T25 / T28 冻结、记分板、摘要与「P2 实验不下注」链路；T32 的 `cloud_stats daily`。
T37（基线无效矩阵）独立，不互相阻塞。

## 共同约束（两个 PR 都适用）
- TEAM 模型的训练、特征、先验、校准、选参不读赔率；市场列只在全部拟合与选窗之后作对照附加。
- 不替换正式模型、不改 `params.json` P2 门槛、不改旧评价口径；BET 禁用、T28 规则不变；不删难预测比赛。
- 结论只说证据支持的话；历史回放不冒充前瞻验证；并行跑的实验模型在达到采用规则前标签固定「P2 实验」。
- **大文件规则（所有者决定）**：逐场预测、特征证据、折参数等全量产物上传到 Release `experiments-YYYYMM` 资产
  （复用 `stats_release.py` 的上传校验与 `SHA256SUMS-<run>` 机制，空文件不上传）；数据分支 `experiments/<批次>/` 只放
  README 与 SHA256SUMS（≤ 1 MB）；main 的 `reports/` 仍只放 README + SHA256SUMS（≤ 200 KB）。T33 已入数据分支的
  149 MB 不重写历史。

---

## T34a · 日常技术数据的可知时间 + 实验模型每日并行输出（先做）

### 目标
1. 从现在起每天捕获的技术统计带真实可知时间，让 `strict` 口径的特征在前瞻中逐步可用。
2. 每日预测在原模型之外并行输出 T33 的两个实验模型（只记录、只评分，不进虚拟账户），冻结全部赛前输入。
3. 记分板与摘要按模型分列；采用规则事先写死。

### 范围
1. **可知时间**（`stats_storage.py` 等）：`cloud_stats daily` 写入的行，若该场 `status=finished` 且本次 `captured_utc`
   晚于该场开球 + 2 小时，则 `availability_basis=captured`、`available_utc=captured_utc`；回填（`backfill`）与不满足条件的行仍为
   `backfilled` / NULL。已有行不改写（更正仍按 T31 规则新增版本）。manifest / 分片 / `restore-rows` 不改格式（列已存在）。
   `stats_coverage.json` 新增按 `availability_basis` 的行数，摘要「技术数据覆盖率」节各加一列 captured。
2. **并行模型**（`cloud_predict.py` 新增步骤，失败不阻断原模型）：
   - `dc-sot-v1`（fd 射正，窗口按 T33 各联赛选窗结果，九联赛全部 N=10）与 `dc-xg-v1`（apif xG，英超 / 西甲 / 意甲 N=10、
     德甲 N=5），训练截点 = 当批原模型截点，固定 90 天半衰期。所有者采纳 PR #68 建议修 1：
     **训练特征用 `backfill_replay`**（与 T33 一致，历史开球+3h 假设），训练样本特征截点仍是样本自身开球，
     标签仍须 `kickoff < available_utc < 拟合截点`；**目标特征用 `strict_capture_time`**（特征构建器的 `strict` 模式），
     回填行只要 `captured_utc < 特征截点` 即在真实捕获时刻已知，`effective_available_utc=captured_utc`；
     相等或晚于截点仍拒绝，保留原 `availability_basis=backfilled`、`captured_utc` 与 available_utc，不改库中旧行。
     captured 行仍须捕获/可知时间均早于截点；所有窗口的历史开球+3h 边界不变。
   - 每场输出 `model_name` / `model_version` / 三项概率 / 最可能比分及概率 / `p2_ready=false` / `validation_status=experimental_unadopted`；
     特征不足或拟合失败时该场记 `fallback_reason` 与缺失明细，**不**用原模型概率冒充实验模型的预测（与 T33 离线不同：前瞻不回退，只记空）。
   - 冻结赛前输入快照：每队用到的历史场次 ID、n/N、各统计值与 `availability_basis` / `available_utc` / `captured_utc` /
     `effective_available_utc`、特征截点、模型系数哈希；训练证据与目标 features 分别记录
     `training_availability_mode=backfill_replay`、`target_availability_mode=strict_capture_time`，不把训练回放说成严格历史实时验证；
     写入 `forecasts/<日>/<run>/experimental.json`，SHA256 进 `forecast_manifest.json`。
   - 理由：已经真实抓到的历史统计可用于之后的预测，不必再等数月积累 captured 标签。
     窗口完整且训练足够时可出实验预测；缺数据仍记空，不保证首日所有联赛都有结果。
     T33 原历史截点早于这些回填的真实捕获时间，仍拒绝；其冻结协议、产物与结论不改。
3. **记分板 / 摘要**：`score.py` 已按 `model_version` 分组，扩展为按 `model_name × 联赛`；实验模型只配对自己有预测的场次，
   另给出「同场共同样本」上原模型 vs 实验模型的配对 LL / Brier 差与 UTC 周整块自助区间（复用 T33 `metrics.paired`）。
   摘要新增「实验模型」小节：当日实验预测数 / 空缺数及原因、累计共同样本 n、配对差、距采用规则的差距；
   首行固定「实验 · 不下注」。`paper.py` 不改（T28 已按 `p2_ready` 排除）。
4. **采用规则（事先写死进 `scoreboard/summary.json` 与 README）**：某联赛前瞻**共同样本** n ≥ 200、配对 LL 差 < 0 且自助 95% 区间
   上限 < 0、10 桶校准不劣、覆盖率不低于原模型 90%；满足时记分板只标「达到采用门槛，待所有者决策」，不自动替换。
5. **文档**：README 每日结果说明与 `forecasts/` 布局；ARCHITECTURE「并行模型与冻结输入」「可知时间」；工作流无新 job。

### 不做
- 不把实验模型概率用于任何注单；不改原模型任何输出字段；不读赔率。
- 不回填历史行的 `available_utc`；不重写数据分支历史。

### 测试（离线）
daily 行按条件得到 captured / available_utc，backfill 行不变；strict 按真实捕获时间判定，覆盖回填早于/等于/晚于截点；
训练 replay 与目标 strict 分离，T33 原离线测试结果不变；单个实验文件格式异常记 experimental_audit 并整文件跳过，不影响正式记分板；
实验模型失败不影响原模型冻结；
`experimental.json` 字节固定、哈希进 manifest；记分板共同样本配对与区间可复现；采用规则满足 / 不满足两例；P2 实验不下注。

### 真实验收（审查者）
合入后第一次晚班 `predict`：原模型输出与前一日格式一致，`experimental.json` 存在；输入齐全的场次有独立实验预测，空缺仍有原因；
一周后 `stats_coverage.json` 出现 captured 行，摘要「实验模型」小节有数。连续 ≥ 14 天两模型并行后出第一份比较报告。

---

## T34b · 第二轮离线实验：候选二（校准层）

### 目标
在不改原模型比分分布的前提下，检验「原模型胜平负概率 + 赛前技术特征」的逻辑校准层是否提升 LL；同时修正第一轮协议的两处口径。

### 范围
1. **协议先冻结**（`docs/experiments/T34b-evaluation-protocol.json` + 设计文档，先于结果提交）：沿用 T33 的时间切分
   （2024/25 选参、2025/26 评价、周折）、自助设置与六列对照；两处修正：选参按**共同实验样本**比较；基线与候选在同一折点拟合、
   特征截点为比赛开球，报告里显式写出这一不对称及其方向。
2. **候选二** `calib-stats-v1`：多项逻辑回归，输入 = 原模型三项概率的 logit + 技术特征差（主客近 N 场射正 for/against 差、
   xG 差（apif 组）、T13 休息天数差、红牌场次标记），系数由训练折拟合，L2 正则强度在选参期按共同样本选定；
   **消融** = 只用原模型 logit 的逻辑重校准（不含技术特征），用来区分「重校准」与「技术信息」各贡献多少。
3. 复用 T33 的特征层、评估与报告代码；新增只在 `predict/experiments/` 下；全量产物按共同约束上 Release。
4. 报告格式沿用 T33 README（全目标 / 共同样本、六组配对区间、分组），允许「无改善」。

### 不做
- 不改 T33 第一轮的任何产物与结论；不做第三个候选；不接入每日预测（若有改善，另开任务单并经 T34a 的并行机制前瞻验证）。

### 测试（离线）
校准层系数由拟合产生、无赔率输入；消融与全模型共用样本掩码；选参只用共同样本；输出禁止覆盖；Release 上传走现有校验。

### 真实验收（审查者）
在同一数据分支快照上独立复算，配对差与区间一致（容差 1e-4）。

---

## 完成标准
两个 PR 各自 pytest / ruff 通过；T34a 真实并行 ≥ 14 天后、T34b 复算一致后，审查者写 `docs/reviews/<日期>-t34-review.md`，
把「是否采用、依据哪条规则、差多少」写清楚。
