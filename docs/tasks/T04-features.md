# T04 · 特征工程

## 目标
`zucai features build` 为每场比赛生成一行特征，只使用开赛前可得的信息。

## 范围
1. `src/zucai/features/build.py`：`build_features(session, match_ids=None, as_of=None) -> pandas.DataFrame`。
2. 特征（主客队各一份）：
   - 近 5 / 10 场进球、失球、积分均值（按时间衰减加权各一版）；
   - 近 5 场主场（主队）/ 客场（客队）表现；
   - 休息天数；
   - 两队近 3 次交锋结果；
   - 市场对比列 `market_p_home` / `market_p_draw` / `market_p_away` / `market_line`：最新 `had` 快照去抽水归一化的隐含概率及让球线。这组列只用于回测基准和报告对比，默认不进入任何模型输入，`build_features` 用 `market_` 前缀隔离。
3. 严格以 `kickoff_utc` 为界：任何特征只能来自该时刻之前已完赛的比赛和之前的赔率快照。写一个测试专门验证不泄漏未来信息。
4. `zucai features build [--out data/features.parquet]`，默认输出 parquet，同时打印特征列清单。

## 不做
- 不训练模型。
- 不引入外部数据（天气、伤停）。

## 验收标准
- 对 T03 回填的数据能生成特征表，缺失值比例在 PR 里说明。
- 泄漏测试通过。
