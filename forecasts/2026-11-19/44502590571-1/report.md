# P2 预测报告

> **非购买建议 · BET 禁用 · 各联赛P2历史评估门槛见逐场状态**

常规 90 分钟含补时。纯模型仅使用球队历史比分；市场 SP 和去水概率单独对照。
EV 为模型概率 × SP − 1 的点估计，EV > 0.05 仅加粗显示，不构成 BET 或购买建议。

批次：44502590571-1-2026-11-19；模式：prematch。
生成时间（北京时间）：2026-11-19 18:44:33；数据截止（北京时间）：2026-11-19 18:44:21。

## 模型与训练范围

版本为模型文件 SHA256；所有展示时间均为北京时间。assumed 占比表示使用假定赛果可知时间的训练场次占比，不代表已核实发布时间。

| 模型 | 版本 SHA256 | 来源 | 联赛 | 模型截点 | 训练场数 | 最近赛果可知时间 | 可知依据 | assumed 占比 | 训练库 SHA256 | 评估库 SHA256 | 数据库说明 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dixon-coles | 914b03262fea0016fe415291fdccfd5ceba271548000cc49d9a0dfa3af8729ec | fd,jc | 德甲 | 2026-11-19 18:33:03 | 1611 | 2026-11-11 04:14:29 | mixed | 95.53% | 79a2b6316819213b4ee11cbaff2bba742d95c90573c8cfb1b3708f13afbeb65a | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | f5d585c431624a5e5a9a6964aad3c5b7067b504f80821e3e9e682d9eed3ed41e | fd,jc | 意甲 | 2026-11-19 18:33:03 | 2010 | 2026-11-11 04:14:29 | mixed | 95.52% | 79a2b6316819213b4ee11cbaff2bba742d95c90573c8cfb1b3708f13afbeb65a | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 16571d4112f05d7815a2c5d5a97c1d05171e2449859075191b1ba8086cc6e8ab | fd,jc | 西甲 | 2026-11-19 18:33:03 | 2019 | 2026-11-11 04:14:29 | mixed | 95.69% | 79a2b6316819213b4ee11cbaff2bba742d95c90573c8cfb1b3708f13afbeb65a | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 1eaeab3c24cba81040a44ceedfd005d8131420ec17ee4acd0dfdd44aad49c285 | fd,jc | 英超 | 2026-11-19 18:33:03 | 1992 | 2026-11-11 04:14:29 | mixed | 96.39% | 79a2b6316819213b4ee11cbaff2bba742d95c90573c8cfb1b3708f13afbeb65a | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | ea417b2fe86a2903c8e5b03c80297ab450403ed5e729b8accec4b8fbd7d205ef | fd,jc | 法甲 | 2026-11-19 18:33:03 | 1768 | 2026-11-11 04:14:29 | mixed | 97.68% | 79a2b6316819213b4ee11cbaff2bba742d95c90573c8cfb1b3708f13afbeb65a | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 2c57ff0b1a5087290de590994e6b9388c2253366a001889b8f2bae9c604955a4 | apif,fd,fdorg,jc | 荷甲 | 2026-11-19 18:33:03 | 1638 | 2026-11-11 04:14:01 | mixed | 97.86% | 79a2b6316819213b4ee11cbaff2bba742d95c90573c8cfb1b3708f13afbeb65a | — | — |
| dixon-coles | bab17022b12d3eda6f5d57c68ebc34c1b60ed04554dfd04d046cd40e6f909023 | fd,jc | 葡超 | 2026-11-19 18:33:03 | 1622 | 2026-11-11 04:14:29 | mixed | 98.27% | 79a2b6316819213b4ee11cbaff2bba742d95c90573c8cfb1b3708f13afbeb65a | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 50bf11587abe73bef73442834b50874d34238eb54d06ec3afeccab06b834b5e6 | apif,fd,fdorg,jc | 英冠 | 2026-11-19 18:33:03 | 2940 | 2026-11-08 17:52:48 | mixed | 98.78% | 79a2b6316819213b4ee11cbaff2bba742d95c90573c8cfb1b3708f13afbeb65a | — | — |
| dixon-coles | d1604ad134921e2548157f3d70823da31f180d570dce235a7f8ee3b09f415e8f | apif,fd,jc | 瑞超 | 2026-11-19 18:33:03 | 1798 | 2026-11-14 16:18:30 | mixed | 98.67% | 79a2b6316819213b4ee11cbaff2bba742d95c90573c8cfb1b3708f13afbeb65a | — | — |
| dixon-coles | 04dd26ef9130e91c11848b713c00c9565d900fcc8bf75b52d3b9d7336c90f535 | apif,fd,jc | 挪超 | 2026-11-19 18:33:03 | 1814 | 2026-11-14 16:18:30 | mixed | 98.84% | 79a2b6316819213b4ee11cbaff2bba742d95c90573c8cfb1b3708f13afbeb65a | — | — |
| dixon-coles | 1c729aadadb8004efdef9eb5042ee7038b8409d9b63b69eceda69d7a54a41891 | apif,fd,jc | 法乙 | 2026-11-19 18:33:03 | 1845 | 2026-11-08 06:00:00 | mixed | 99.57% | 79a2b6316819213b4ee11cbaff2bba742d95c90573c8cfb1b3708f13afbeb65a | — | — |
| dixon-coles | e7418cb541f5b54bcfa7c2dc2337c51a3760e50c78cc56c6308f63a4e2945b5b | apif,fd,jc | 日职 | 2026-11-19 18:33:03 | 2986 | 2026-11-14 16:18:30 | mixed | 98.79% | 79a2b6316819213b4ee11cbaff2bba742d95c90573c8cfb1b3708f13afbeb65a | — | — |
| dixon-coles | cd29eac1261b108b1b10cb92403925c8b756bf0b78fa6ede7aadebe3dd145805 | apif,fd,jc | 芬超 | 2026-11-19 18:33:03 | 1167 | 2026-11-14 16:18:30 | mixed | 98.89% | 79a2b6316819213b4ee11cbaff2bba742d95c90573c8cfb1b3708f13afbeb65a | — | — |
| dixon-coles | a65dc047b5735e53b45520d7978bf73505bb54e8712026fccce35f5e52f16f40 | fd,jc | 巴甲 | 2026-11-19 18:33:03 | 2236 | 2026-11-19 16:13:55 | mixed | 98.30% | 79a2b6316819213b4ee11cbaff2bba742d95c90573c8cfb1b3708f13afbeb65a | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | ab182a5b834d72a9c4d95b300ececf410e30d3ffc1d4bf81d7001ac19b876a09 | apif,fd,jc | 美职 | 2026-11-19 18:33:03 | 4739 | 2026-11-14 16:18:30 | mixed | 99.54% | 79a2b6316819213b4ee11cbaff2bba742d95c90573c8cfb1b3708f13afbeb65a | — | — |
| dixon-coles | bf9740e46ddcec5327f29f88d043bf0c02c563d8f2b744c4c07e4a6836aafdd4 | apif,fd,fdorg,jc | 欧战 | 2026-11-19 18:33:03 | 27999 | 2026-11-11 04:14:29 | mixed | 97.85% | 79a2b6316819213b4ee11cbaff2bba742d95c90573c8cfb1b3708f13afbeb65a | 385035b903b4aa575365a8635f187e7d18bd49b5c24ab46cdfdfd5ed386ea1dd | 评估库 ≠ 训练库 |
| dixon-coles | e817c7624caf712312a766c1e6afabdb3f25aaa630e84af375849068b77536a4 | apif,jc | 欧国联 | 2026-11-19 18:33:03 | 818 | 2026-11-18 11:18:59 | mixed | 87.29% | 79a2b6316819213b4ee11cbaff2bba742d95c90573c8cfb1b3708f13afbeb65a | — | — |
| dixon-coles | 2313b885fcbd2c72ef95d67a15f46e9f3f5e7fbf8181695fc9c93ea1db82ea02 | apif,jc | 韩职 | 2026-11-19 18:33:03 | 2231 | 2026-11-09 00:36:53 | mixed | 98.74% | 79a2b6316819213b4ee11cbaff2bba742d95c90573c8cfb1b3708f13afbeb65a | 1f5a99bfad007629272d2272fc0dfb10d4a11b9e0dcad775bb22f7dbc4d5f44f | 评估库 ≠ 训练库 |
| dixon-coles | 4fdb8c9d6a7d45f3ee027db5f12b845875bf60501fbf7178e0c10a42bb8a4757 | apif,jc | 澳超 | 2026-11-19 18:33:03 | 2283 | 2026-11-08 17:53:22 | mixed | 99.26% | 79a2b6316819213b4ee11cbaff2bba742d95c90573c8cfb1b3708f13afbeb65a | 1f5a99bfad007629272d2272fc0dfb10d4a11b9e0dcad775bb22f7dbc4d5f44f | 评估库 ≠ 训练库 |
| dixon-coles | 8edcccb3f2fe9ef46a4f827e66a71e3bc16cef57718b9e1384972a8da95afcaf | apif,jc | 沙职 | 2026-11-19 18:33:03 | 2586 | 2026-11-01 21:08:06 | mixed | 99.54% | 79a2b6316819213b4ee11cbaff2bba742d95c90573c8cfb1b3708f13afbeb65a | 1dd8b10efce1fc56fb85ddc988fae7e7def8c908d37df16e0dae9ff62417994f | 评估库 ≠ 训练库 |
| dixon-coles | 262ac8822d61723b2263f64a471e4f48e7cfbf7991754fffdd60917bc079682f | apif,jc | 德乙 | 2026-11-19 18:33:03 | 4699 | 2026-11-08 23:30:00 | mixed | 99.83% | 79a2b6316819213b4ee11cbaff2bba742d95c90573c8cfb1b3708f13afbeb65a | 5f2eb263016233afd55c8d2b880ec21753881e49916b0ae3b4645751c5f325c1 | 评估库 ≠ 训练库 |
| dixon-coles | 04b925d19221a0d5dd04c87e2ae521efdab506e9ff0ed4b17a1fbadc196b6c37 | apif,jc | 荷乙 | 2026-11-19 18:33:03 | 3869 | 2026-11-16 20:29:34 | mixed | 99.56% | 79a2b6316819213b4ee11cbaff2bba742d95c90573c8cfb1b3708f13afbeb65a | e1da97a554d7ef2ba40fe611a7c1c5eb962b8efba2117709bbdb6e9e96385710 | 评估库 ≠ 训练库 |

来源列为显式选择的训练语料范围；实际符合截点的训练来源及场数见 forecast.json 的 training_source_counts（null 表示旧模型未记录）。

## 纯模型与市场对照

| 比赛 ID / 编号 | 联赛 / 对阵 | 开球（北京时间） | 状态 / 排除原因 | 模型主胜 | 模型平局 | 模型客胜 | 市场主胜 | 市场平局 | 市场客胜 | SP 主胜 | SP 平局 | SP 客胜 | EV 主胜 | EV 平局 | EV 客胜 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| jc:2042838 / 周三001 | 巴甲 / 科里蒂巴 vs 桑托斯 | 2026-11-19 06:30:00 | 排除：预测截点时已开赛 | — | — | — | — | — | — | — | — | — | — | — | — |
| jc:2042839 / 周三002 | 巴甲 / 弗拉门戈 vs 巴竞技 | 2026-11-19 07:30:00 | 排除：预测截点时已开赛 | — | — | — | — | — | — | — | — | — | — | — | — |
| jc:2042840 / 周三003 | 巴甲 / 科林蒂安 vs 博塔弗戈 | 2026-11-19 08:30:00 | 排除：预测截点时已开赛 | — | — | — | — | — | — | — | — | — | — | — | — |
| jc:2042841 / 周三004 | 巴甲 / 格雷米奥 vs 巴伊亚 | 2026-11-19 08:30:00 | 排除：预测截点时已开赛 | — | — | — | — | — | — | — | — | — | — | — | — |

## 比分前五与快照

