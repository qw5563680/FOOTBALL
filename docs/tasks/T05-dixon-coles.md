# T05 · Dixon-Coles 基线模型与回测

## 目标
实现带时间衰减的 Dixon-Coles 泊松模型，输出每场比赛的 10x10 比分概率矩阵，并给出滚动回测指标。

## 范围
1. `src/zucai/predict/base.py`：`Model` 抽象类（`fit`、`predict_score_matrix`、`save`、`load`，模型文件存 `models/`）。
2. `src/zucai/predict/dixon_coles.py`：
   - 参数：各队攻击、防守强度，主场优势，低比分修正 rho，时间衰减半衰期（默认 90 天）；
   - 用 `scipy.optimize.minimize` 极大似然拟合，只用 `results` 表，不依赖 T04 特征；
   - 比分矩阵截断到 0..9 球。
3. `src/zucai/predict/derive.py`：由比分矩阵推导 `had`、`hhad(line)`、`crs`、`ttg`、`hafu`（半全场可先用近似：假设上下半场独立，各半场强度为全场一半）。
4. `src/zucai/predict/backtest.py`：按周滚动，每周用之前数据拟合、预测该周比赛，输出对数损失、Brier、按赔率分桶命中率。
5. `zucai train --model dixon-coles [--since YYYY-MM-DD]`：拟合、保存模型、打印回测指标。
6. 测试：用构造的小数据集验证拟合收敛、矩阵归一化为 1、推导函数的概率和为 1、让球推导对称性。

## 不做
- 不做 GBDT，不做市场融合（T06）。

## 验收标准
- 在 T03 的真实数据上回测，PR 里给出指标，并与「直接用赔率隐含概率」的基线对比。
