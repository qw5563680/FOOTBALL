# 赛前 1X2 模型选型与首个验收样例

核查日期为 2026-11-01。本文供 Claude Code 维护设计、Codex 实现和双方审查 PR 时共用。

## 预测口径

主胜、平、客胜覆盖 90 分钟及补时。加时和点球单独处理。只用预测截点前已经完赛、且结果已可取得的比赛拟合球队强度。每条模型概率保留模型版本、训练截点、数据来源和样本量。市场赔率换算得到的三项概率单列作基准。

## 第一项技术选择

优先测试 `penaltyblog==1.13.1` 的 Dixon–Coles 模型。日期、主客队及常规时间进球足以拟合攻防、主场优势和低比分彩果相关性，从比分网格汇总三项概率。训练截点由调用方保证。未知球队需要明示为数据不足。[拟合](https://github.com/martineastwood/penaltyblog/blob/6870f0e762e9cba35fa722dc14a8daeaf8bc6134/penaltyblog/models/dixon_coles.py#L187-L220) · [比分矩阵](https://github.com/martineastwood/penaltyblog/blob/6870f0e762e9cba35fa722dc14a8daeaf8bc6134/penaltyblog/models/probabilities.pyx#L78-L145) · [三项概率](https://github.com/martineastwood/penaltyblog/blob/6870f0e762e9cba35fa722dc14a8daeaf8bc6134/penaltyblog/models/football_probability_grid.py#L98-L117)

仓库 T05 约定 0–9 球矩阵和 90 天半衰期，本次样例使用 0–14 球和每日衰减系数 0.001。对照实现时须先统一参数、训练记录和截断方式。项目可保留已有轻量 SciPy 路线，把已验证的库作为独立数值参照。

## 已运行的历史样例

2019/20 英超公开样例共 380 场。预测截点为 2020-01-01，日期严格早于截点的训练集有 199 场，最晚日期为 2019-12-29。留出的比赛为 Arsenal 主场对 Man United。Python 3.12.14、NumPy 2.5.4、pandas 3.1.0、SciPy 1.18.1。原生下载曾等待过久，带连接和读取超时的下载成功，缓存交给原生解析器。

| 项目 | 主胜 | 平局 | 客胜 |
| --- | ---: | ---: | ---: |
| 解析概率 | 0.26132853 | 0.26088012 | 0.47779136 |
| 联合网格抽样 100000 次，种子 42 | 0.26106 | 0.25982 | 0.47912 |

进球均值分别为 1.15291335 和 1.63368082。抽样核验的是终场分布，预测质量还需用锁定的后续赛季计算对数损失、同口径的多类 Brier 和校准分箱。原始第三方数据按来源条款使用。[数据提供方说明](https://football-data.co.uk/data.php)

## 任务单的验收补充

- **T01–T03** 保留原始快照与 UTC 时间。历史比分标明常规时间口径，缺失、未完赛和取消状态分开。
- **T04–T05** 按历史比赛结束且可知的时间截断。改动截点之后的赛果，截点前预测应保持一致。
- **T06** 将模型概率与同截点赔率基准分列。用户的要求取代原任务单里默认混合赔率的提议。回归检查应覆盖仅改变赔率时模型三项概率保持一致。
- **复盘** 保存原始预测和生成时间，赛后追加评分，避免覆盖赛前版本。

完整技能方案 JetQiao 提供可执行流程，但其默认融合含市场权重。callumfm 提供带历史 xG 的备选引擎，使用时也需外置截点。项目第一轮用一个可验证的球队模型建立基准。[JetQiao 融合路径](https://github.com/JetQiao/football-prediction-skill/blob/23ddaf084d6fc9c2491ee2e8a899eb5a22a79817/src/football_prediction/modeling/fusion.py#L137-L206) · [callumfm 拟合路径](https://github.com/callumfm/dixon-coles/blob/dea06c6e4b38085d6a4b84186cb9ead321a73ce4/src/dixon_coles/model.py#L232-L281)
