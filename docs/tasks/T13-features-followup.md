# T13 · 特征表的两处修正（T04 后续）

## 目标
修正 T04 审查时指出的两个问题，不改变其余特征的定义与数值。

## 范围
1. **让球线列**：`market_line` 目前恒为 0 或 NaN，没有信息量。改为：
   - 保留 `market_p_home/draw/away`（仍来自普通 had 快照）；
   - 删除 `market_line`，新增 `market_hhad_line` 与 `market_hhad_p_home/draw/away`：取同一时刻完整、严格早于截点的最新
     `hhad` 快照，`line` 为其让球线（整数），概率按逆价比例去水；无完整快照则 NaN。
   - 这组列仍只在 `MARKET_COLUMNS`，不进模型白名单；`feature_version` 升到 `t04-v2`。
2. **休息天数改按赛程算**：`home_rest_days/away_rest_days` 改为取 `matches` 表中同来源、同球队、开球严格早于目标开球的
   最近一场比赛（不要求有赛果、不要求 `available_utc`，状态 `scheduled`/`finished` 均可，排除 `cancelled`），
   因为赛程是赛前已知信息。`meta_home_rest_n/assumed_n` 相应改为记录所用比赛 ID；
   文档改写"距最近已知开球的间隔"的定义。
3. 测试：hhad 列与 had 列分别取自各自快照、line 为整数；休息天数在赛果尚未采集时仍能算出；
   原有泄漏测试全部保留并通过。
4. 验收产物只提交 README 与哈希清单（见 AGENTS.md 第 8 条），不再提交 parquet 与全量 metadata。

## 不做
- 不改其余 43 列的定义。
- 不训练模型。

## 验收标准
- 在 T04 验收同一份库上重跑，PR 贴出 `market_hhad_line` 非空比例、休息天数非空比例与 T04 的对比。
