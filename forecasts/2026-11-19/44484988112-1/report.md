# P2 预测报告

> **非购买建议 · BET 禁用 · 各联赛P2历史评估门槛见逐场状态**

常规 90 分钟含补时。纯模型仅使用球队历史比分；市场 SP 和去水概率单独对照。
EV 为模型概率 × SP − 1 的点估计，EV > 0.05 仅加粗显示，不构成 BET 或购买建议。

批次：44484988112-1-2026-11-19；模式：prematch。
生成时间（北京时间）：2026-11-19 16:31:47；数据截止（北京时间）：2026-11-19 16:31:44。

## 模型与训练范围

版本为模型文件 SHA256；所有展示时间均为北京时间。assumed 占比表示使用假定赛果可知时间的训练场次占比，不代表已核实发布时间。

| 模型 | 版本 SHA256 | 来源 | 联赛 | 模型截点 | 训练场数 | 最近赛果可知时间 | 可知依据 | assumed 占比 | 训练库 SHA256 | 评估库 SHA256 | 数据库说明 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dixon-coles | 53612aef93f8d6d37050a89bc191df83cc5e92b6c07fb96ad2a2f7352be45574 | fd,jc | 德甲 | 2026-11-19 16:19:19 | 1611 | 2026-11-11 04:14:29 | mixed | 95.53% | 557fe77a292686f5c8a7c9eb135c69cb7009bd90c98bbb1ec613535bfc9da2d3 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 14d285cca3e148d08520d7c1d98c25884a5317fcc1b3e96375b11f0807c48670 | fd,jc | 意甲 | 2026-11-19 16:19:19 | 2010 | 2026-11-11 04:14:29 | mixed | 95.52% | 557fe77a292686f5c8a7c9eb135c69cb7009bd90c98bbb1ec613535bfc9da2d3 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | e8f1174485cb5112199487b4e541fa14b63ee1f41949a4db2741491120e9cbaa | fd,jc | 西甲 | 2026-11-19 16:19:19 | 2019 | 2026-11-11 04:14:29 | mixed | 95.69% | 557fe77a292686f5c8a7c9eb135c69cb7009bd90c98bbb1ec613535bfc9da2d3 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | e34c3dc291403c20ed348f7b1ddbf36671d94ce5a2b46f488413a5bc9ec3f560 | fd,jc | 英超 | 2026-11-19 16:19:19 | 1992 | 2026-11-11 04:14:29 | mixed | 96.39% | 557fe77a292686f5c8a7c9eb135c69cb7009bd90c98bbb1ec613535bfc9da2d3 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 2f73eed3dbca10a6a86f40868944ec042cb30d2b85b54e1df3478e1396b62f96 | fd,jc | 法甲 | 2026-11-19 16:19:19 | 1768 | 2026-11-11 04:14:29 | mixed | 97.68% | 557fe77a292686f5c8a7c9eb135c69cb7009bd90c98bbb1ec613535bfc9da2d3 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | cefcc98de9d2a7c2a682618c2d9a6f2b089360bd03cd2c8babbab9f34f9dcdae | apif,fd,fdorg,jc | 荷甲 | 2026-11-19 16:19:19 | 1638 | 2026-11-11 04:14:01 | mixed | 97.86% | 557fe77a292686f5c8a7c9eb135c69cb7009bd90c98bbb1ec613535bfc9da2d3 | — | — |
| dixon-coles | 17f9be471efbae605707319dd62dc50b6ca76a34da367d1aa8de799f1fb2c475 | fd,jc | 葡超 | 2026-11-19 16:19:19 | 1622 | 2026-11-11 04:14:29 | mixed | 98.27% | 557fe77a292686f5c8a7c9eb135c69cb7009bd90c98bbb1ec613535bfc9da2d3 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | b92474783d28c6480148aba657843fa25b41ee138be4f506567b7029855f8778 | apif,fd,fdorg,jc | 英冠 | 2026-11-19 16:19:19 | 2940 | 2026-11-08 17:52:48 | mixed | 98.78% | 557fe77a292686f5c8a7c9eb135c69cb7009bd90c98bbb1ec613535bfc9da2d3 | — | — |
| dixon-coles | 9b4e3a0776b21f1f7231d9e1b3e7d0680b4724ad9ff309ce8df0b6cdfab6aa3c | apif,fd,jc | 瑞超 | 2026-11-19 16:19:19 | 1798 | 2026-11-14 16:18:30 | mixed | 98.67% | 557fe77a292686f5c8a7c9eb135c69cb7009bd90c98bbb1ec613535bfc9da2d3 | — | — |
| dixon-coles | 9a97a93926d5c437ac9497d9eeb80df77371e46fed588e84389a4c81324394c0 | apif,fd,jc | 挪超 | 2026-11-19 16:19:19 | 1814 | 2026-11-14 16:18:30 | mixed | 98.84% | 557fe77a292686f5c8a7c9eb135c69cb7009bd90c98bbb1ec613535bfc9da2d3 | — | — |
| dixon-coles | 07245a382fb939976dd451e0f2375f0b5805e9d0bbfede9fde691d4e508c7593 | apif,fd,jc | 法乙 | 2026-11-19 16:19:19 | 1845 | 2026-11-08 06:00:00 | mixed | 99.57% | 557fe77a292686f5c8a7c9eb135c69cb7009bd90c98bbb1ec613535bfc9da2d3 | — | — |
| dixon-coles | d4b82201e1fb62e0a898d7d2cb13f7a5700cfa613a3aeb4d823f494f214f664f | apif,fd,jc | 日职 | 2026-11-19 16:19:19 | 2986 | 2026-11-14 16:18:30 | mixed | 98.79% | 557fe77a292686f5c8a7c9eb135c69cb7009bd90c98bbb1ec613535bfc9da2d3 | — | — |
| dixon-coles | cb815e0c6480ad297fb1ca98a88ed66a8ae9eeca53af9ba3b107d4978432704a | apif,fd,jc | 芬超 | 2026-11-19 16:19:19 | 1167 | 2026-11-14 16:18:30 | mixed | 98.89% | 557fe77a292686f5c8a7c9eb135c69cb7009bd90c98bbb1ec613535bfc9da2d3 | — | — |
| dixon-coles | dd8e45c7144f8a640e5f065123eef32b18e782d7d4de40a7597402ad140085ce | fd,jc | 巴甲 | 2026-11-19 16:19:19 | 2236 | 2026-11-19 16:13:55 | mixed | 98.30% | 557fe77a292686f5c8a7c9eb135c69cb7009bd90c98bbb1ec613535bfc9da2d3 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | d9914a123af623bfad1fcc25f69b2e8b8e9dba03fdb8f83bd2a822c978c3dfee | apif,fd,jc | 美职 | 2026-11-19 16:19:19 | 4739 | 2026-11-14 16:18:30 | mixed | 99.54% | 557fe77a292686f5c8a7c9eb135c69cb7009bd90c98bbb1ec613535bfc9da2d3 | — | — |
| dixon-coles | 6a5e3b20dbc0b29ea4050fd089e664fa8fc71aa4691694f61f1d82d79616dc76 | apif,fd,fdorg,jc | 欧战 | 2026-11-19 16:19:19 | 27999 | 2026-11-11 04:14:29 | mixed | 97.85% | 557fe77a292686f5c8a7c9eb135c69cb7009bd90c98bbb1ec613535bfc9da2d3 | 385035b903b4aa575365a8635f187e7d18bd49b5c24ab46cdfdfd5ed386ea1dd | 评估库 ≠ 训练库 |
| dixon-coles | ed62e29b05e4d29a2f97ce9bb93222b4d88a267d519c4c00f9d00eb2f48825e0 | apif,jc | 欧国联 | 2026-11-19 16:19:19 | 818 | 2026-11-18 11:18:59 | mixed | 87.29% | 557fe77a292686f5c8a7c9eb135c69cb7009bd90c98bbb1ec613535bfc9da2d3 | — | — |
| dixon-coles | 01ff80702c80aba90dae96b024842632f97002af16e83c539b34c1b7fd33baa1 | apif,jc | 韩职 | 2026-11-19 16:19:19 | 2231 | 2026-11-09 00:36:53 | mixed | 98.74% | 557fe77a292686f5c8a7c9eb135c69cb7009bd90c98bbb1ec613535bfc9da2d3 | 1f5a99bfad007629272d2272fc0dfb10d4a11b9e0dcad775bb22f7dbc4d5f44f | 评估库 ≠ 训练库 |
| dixon-coles | f77515abcf505adba3a6ff467bdce76cd61b4d6520ec3c8f017c6d869bc330f4 | apif,jc | 澳超 | 2026-11-19 16:19:19 | 2283 | 2026-11-08 17:53:22 | mixed | 99.26% | 557fe77a292686f5c8a7c9eb135c69cb7009bd90c98bbb1ec613535bfc9da2d3 | 1f5a99bfad007629272d2272fc0dfb10d4a11b9e0dcad775bb22f7dbc4d5f44f | 评估库 ≠ 训练库 |
| dixon-coles | fb8fc6a0db10233645d2c1fea5f134a5ef26ba860537290c8ae06c8bb310a860 | apif,jc | 沙职 | 2026-11-19 16:19:19 | 2586 | 2026-11-01 21:08:06 | mixed | 99.54% | 557fe77a292686f5c8a7c9eb135c69cb7009bd90c98bbb1ec613535bfc9da2d3 | 1dd8b10efce1fc56fb85ddc988fae7e7def8c908d37df16e0dae9ff62417994f | 评估库 ≠ 训练库 |
| dixon-coles | d5a2cd9ea4284cbd4257a7468ef8274b752255a9bcd136b42bbe34ea1678fff6 | apif,jc | 德乙 | 2026-11-19 16:19:19 | 4699 | 2026-11-08 23:30:00 | mixed | 99.83% | 557fe77a292686f5c8a7c9eb135c69cb7009bd90c98bbb1ec613535bfc9da2d3 | 5f2eb263016233afd55c8d2b880ec21753881e49916b0ae3b4645751c5f325c1 | 评估库 ≠ 训练库 |
| dixon-coles | e78c2cf9918880a518bcfa91a9f754800d0e172ef50c8a3b161cf797e4e01d2d | apif,jc | 荷乙 | 2026-11-19 16:19:19 | 3869 | 2026-11-16 20:29:34 | mixed | 99.56% | 557fe77a292686f5c8a7c9eb135c69cb7009bd90c98bbb1ec613535bfc9da2d3 | e1da97a554d7ef2ba40fe611a7c1c5eb962b8efba2117709bbdb6e9e96385710 | 评估库 ≠ 训练库 |

来源列为显式选择的训练语料范围；实际符合截点的训练来源及场数见 forecast.json 的 training_source_counts（null 表示旧模型未记录）。

## 纯模型与市场对照

| 比赛 ID / 编号 | 联赛 / 对阵 | 开球（北京时间） | 状态 / 排除原因 | 模型主胜 | 模型平局 | 模型客胜 | 市场主胜 | 市场平局 | 市场客胜 | SP 主胜 | SP 平局 | SP 客胜 | EV 主胜 | EV 平局 | EV 客胜 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| jc:2042838 / 周三001 | 巴甲 / 科里蒂巴 vs 桑托斯 | 2026-11-19 06:30:00 | 排除：预测截点时已开赛 | — | — | — | — | — | — | — | — | — | — | — | — |
| jc:2042839 / 周三002 | 巴甲 / 弗拉门戈 vs 巴竞技 | 2026-11-19 07:30:00 | 排除：预测截点时已开赛 | — | — | — | — | — | — | — | — | — | — | — | — |
| jc:2042840 / 周三003 | 巴甲 / 科林蒂安 vs 博塔弗戈 | 2026-11-19 08:30:00 | 排除：预测截点时已开赛 | — | — | — | — | — | — | — | — | — | — | — | — |
| jc:2042841 / 周三004 | 巴甲 / 格雷米奥 vs 巴伊亚 | 2026-11-19 08:30:00 | 排除：预测截点时已开赛 | — | — | — | — | — | — | — | — | — | — | — | — |

## 比分前五与快照

