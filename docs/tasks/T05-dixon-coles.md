# T05 · Dixon-Coles 基线模型与回测

## 目标
实现带时间衰减的 Dixon-Coles 泊松模型，输出每场比赛的 10x10 比分概率矩阵，并给出滚动回测指标。
模型只用球队的历史比分，**不以任何形式读取赔率**；赔率隐含概率只在回测里作为对比基准单独报告。

## 范围
1. `src/zucai/predict/base.py`：`Model` 抽象类（`fit`、`predict_score_matrix`、`save`、`load`，模型文件存 `models/`）。
2. `src/zucai/predict/dixon_coles.py`：
   - 参数：各队攻击、防守强度，主场优势，低比分修正 rho，时间衰减半衰期（默认 90 天）；
   - 用 `scipy.optimize.minimize` 极大似然拟合，只用 `results` 表，不依赖 T04 特征，不读取 `odds` 表；
   - 比分矩阵截断到 0..9 球。
   - 实现方式二选一：自研；或封装第三方库（如 PR #1 调研推荐的 penaltyblog）。封装前必须在 PR 里写明
     许可证、是否支持时间衰减、以及如何满足本项目的 `Model` 接口。无论哪种方式，`derive.py` 的推导接口不变。
3. `src/zucai/predict/derive.py`：由比分矩阵推导 `had`、`hhad(line)`、`crs`、`ttg`、`hafu`（半全场可先用近似：假设上下半场独立，各半场强度为全场一半）。
4. `src/zucai/predict/backtest.py`：按周滚动，每周用之前数据拟合、预测该周比赛。输出两组指标，分开报告：
   - **纯模型**：对数损失、Brier、按模型概率分桶的校准表；
   - **赔率基准**：用开赛前最后一次 `had` 快照去抽水归一化得到的隐含概率，算同样的指标。
   基准只用于对比，不参与任何概率计算。
5. `zucai train --model dixon-coles [--since YYYY-MM-DD]`：拟合、保存模型、打印上述两组指标。
6. 测试：用构造的小数据集验证拟合收敛、矩阵归一化为 1、推导函数的概率和为 1、让球推导对称性；
   另加一个测试证明 `fit` 与 `predict_score_matrix` 在 `odds` 表为空时结果不变。

## 不做
- 不做 GBDT。
- 不做任何形式的赔率融合、赔率先验或赔率特征。
- 蒙特卡洛抽样不在本任务：单场各玩法概率由比分矩阵精确推导，抽样只在多场组合（T07）时才需要。

## 验收标准
- 在 T03 的真实数据上回测，PR 里分别给出纯模型和赔率基准的指标，并说明时间划分方式（训练截止时刻早于每场开赛）。
- 封装第三方库时，PR 描述附许可证名称和版本号。

## 回测对"假设可知时间"的披露（路线 B，2026-11-01 定稿）

T03 允许历史回填用 `available_source = 'assumed'` 标记"赛后固定延时已知"的赛果。T05 的回测必须让读者一眼看出这类样本的影响：

1. `read_history` 带出 `available_source`，NULL 视同 `captured`。
2. 回测报告新增：
   - 总体与每个 fold 的 `assumed_training_share`（训练样本中 `assumed` 占比）和 `assumed_eval_share`（评估样本中占比）；
   - 顶层字段 `availability_basis`：全部为 `published`/`captured` 时为 `strict`，否则为 `mixed`，并附占比。
3. `zucai train` 加开关 `--strict-availability`：排除所有 `assumed` 行后重跑同一回测，用于日后用真实采集时间复核。两种模式的指标在同一份输出里并列。
4. 验收测试：构造一半 `assumed` 的历史，默认模式报告 `mixed` 与正确占比；严格模式训练样本数减半且不含任何 `assumed` 行。
5. 文档与报告模板里，凡引用 `mixed` 模式的指标，必须同时写出 `assumed` 占比。
