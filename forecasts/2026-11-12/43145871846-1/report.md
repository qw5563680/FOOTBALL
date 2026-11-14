# P2 预测报告

> **非购买建议 · BET 禁用 · 各联赛P2历史评估门槛见逐场状态**

常规 90 分钟含补时。纯模型仅使用球队历史比分；市场 SP 和去水概率单独对照。
EV 为模型概率 × SP − 1 的点估计，EV > 0.05 仅加粗显示，不构成 BET 或购买建议。

批次：43145871846-1-2026-11-12；模式：prematch。
生成时间（北京时间）：2026-11-12 11:12:06；数据截止（北京时间）：2026-11-12 11:12:05。

## 模型与训练范围

版本为模型文件 SHA256；所有展示时间均为北京时间。assumed 占比表示使用假定赛果可知时间的训练场次占比，不代表已核实发布时间。

| 模型 | 版本 SHA256 | 来源 | 联赛 | 模型截点 | 训练场数 | 最近赛果可知时间 | 可知依据 | assumed 占比 | 训练库 SHA256 | 评估库 SHA256 | 数据库说明 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dixon-coles | 6c4b3883a6ab34a70054406060b852c2bc141e37872ba075f552eb48d3ad5151 | fd,jc | 德甲 | 2026-11-12 11:10:21 | 1611 | 2026-11-11 04:14:29 | mixed | 95.53% | bbb564aefd4d653eb90b2805b42cdf2bb39bb839528a7bfad3c388d3445f4dc2 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 058ef84886419691ae27815bcfd899f4dd464eb45e3aae55c2841e29100689eb | fd,jc | 意甲 | 2026-11-12 11:10:21 | 2010 | 2026-11-11 04:14:29 | mixed | 95.52% | bbb564aefd4d653eb90b2805b42cdf2bb39bb839528a7bfad3c388d3445f4dc2 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 066e521836b33b47d1aa7adb6787d99bf86b209bd350823a5775201cb8924993 | fd,jc | 西甲 | 2026-11-12 11:10:21 | 2018 | 2026-11-11 04:14:29 | mixed | 95.69% | bbb564aefd4d653eb90b2805b42cdf2bb39bb839528a7bfad3c388d3445f4dc2 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | ff8e823181cf0528aba0b48cfeb5e1236886c72e4c0f4f5180ff14517215808f | fd,jc | 英超 | 2026-11-12 11:10:21 | 1992 | 2026-11-11 04:14:29 | mixed | 96.39% | bbb564aefd4d653eb90b2805b42cdf2bb39bb839528a7bfad3c388d3445f4dc2 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 807940570629b795fc3dff4dbbb0c666115892ba44b5eed835a5e812523683ed | fd,jc | 法甲 | 2026-11-12 11:10:21 | 1762 | 2026-11-11 04:14:29 | mixed | 97.67% | bbb564aefd4d653eb90b2805b42cdf2bb39bb839528a7bfad3c388d3445f4dc2 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 867266adebb0c99ef1f97359e82729dcf12830966853f6812a5125a48523a305 | fd,jc | 荷甲 | 2026-11-12 11:10:21 | 1631 | 2026-11-11 04:14:01 | mixed | 97.85% | bbb564aefd4d653eb90b2805b42cdf2bb39bb839528a7bfad3c388d3445f4dc2 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 1dc74b82360c217be62186e1ed382077cb4875b064eab3e0bec9260e8762618d | fd,jc | 葡超 | 2026-11-12 11:10:21 | 1617 | 2026-11-11 04:14:29 | mixed | 98.27% | bbb564aefd4d653eb90b2805b42cdf2bb39bb839528a7bfad3c388d3445f4dc2 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 7fa82e7ddf0e11b33d05235722a33055541b2c056618b0fad6ec810190ba4db8 | fd,jc | 英冠 | 2026-11-12 11:10:21 | 2920 | 2026-11-08 17:52:48 | mixed | 98.77% | bbb564aefd4d653eb90b2805b42cdf2bb39bb839528a7bfad3c388d3445f4dc2 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | f25b26e21eada3ec6f1586d09ce15a8aed6684a524c6877ae49b0a2b0cbdd3cd | fd,jc | 瑞超 | 2026-11-12 11:10:21 | 1410 | 2026-11-11 04:14:01 | mixed | 98.30% | bbb564aefd4d653eb90b2805b42cdf2bb39bb839528a7bfad3c388d3445f4dc2 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 68b1aa27dfd6a30b2f565547ec3a1fff06c116c6430c2655df86bf97de9de63e | fd,jc | 挪超 | 2026-11-12 11:10:21 | 1392 | 2026-11-11 04:14:01 | mixed | 98.49% | bbb564aefd4d653eb90b2805b42cdf2bb39bb839528a7bfad3c388d3445f4dc2 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | c597bbafad2dd91a7d45a4aea0948bc9f29c68b0e1c431678792091d23f1b183 | fd,jc | 法乙 | 2026-11-12 11:10:21 | 1841 | 2026-11-07 17:26:10 | mixed | 99.57% | bbb564aefd4d653eb90b2805b42cdf2bb39bb839528a7bfad3c388d3445f4dc2 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | a073373cdd6da0eb3da64361db5a961a5e4107a35280ee7ddd4fd0a213d97b9d | fd,jc | 日职 | 2026-11-12 11:10:21 | 1876 | 2026-11-08 17:53:22 | mixed | 98.08% | bbb564aefd4d653eb90b2805b42cdf2bb39bb839528a7bfad3c388d3445f4dc2 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | ca5036ea1e9bf4c5b3f11685f4013708702b63d14c6e7e076ddf32d1952bfd2e | fd,jc | 芬超 | 2026-11-12 11:10:21 | 1016 | 2026-11-11 04:14:01 | mixed | 98.72% | bbb564aefd4d653eb90b2805b42cdf2bb39bb839528a7bfad3c388d3445f4dc2 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | e058508f57d124d02fb4fc416d1e8a59c572cd4bee7ae58d45e11dee117795a2 | fd,jc | 巴甲 | 2026-11-12 11:10:21 | 2223 | 2026-11-07 17:26:10 | mixed | 98.56% | bbb564aefd4d653eb90b2805b42cdf2bb39bb839528a7bfad3c388d3445f4dc2 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 58d3c556116bba1ee33130cdcfa9a43520c92858e4f5f6328482c185b3ee42b8 | fd,jc | 美职 | 2026-11-12 11:10:21 | 2969 | 2026-11-08 17:53:22 | mixed | 99.33% | bbb564aefd4d653eb90b2805b42cdf2bb39bb839528a7bfad3c388d3445f4dc2 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 41de886352a97e5eaa33037441febe8fa7397edc48c61c4430b45ffaa8d531e4 | apif,fd,fdorg,jc | 欧战 | 2026-11-12 11:10:21 | 20604 | 2026-11-11 04:14:29 | mixed | 96.92% | bbb564aefd4d653eb90b2805b42cdf2bb39bb839528a7bfad3c388d3445f4dc2 | 385035b903b4aa575365a8635f187e7d18bd49b5c24ab46cdfdfd5ed386ea1dd | 评估库 ≠ 训练库 |
| dixon-coles | 62d88baaf3b211860a872e6af20496126bde78480e0a304e13c174813df7531d | apif,jc | 欧国联 | 2026-11-12 11:10:21 | 766 | 2026-11-01 21:08:02 | mixed | 93.21% | bbb564aefd4d653eb90b2805b42cdf2bb39bb839528a7bfad3c388d3445f4dc2 | — | — |

来源列为显式选择的训练语料范围；实际符合截点的训练来源及场数见 forecast.json 的 training_source_counts（null 表示旧模型未记录）。

## 纯模型与市场对照

| 比赛 ID / 编号 | 联赛 / 对阵 | 开球（北京时间） | 状态 / 排除原因 | 模型主胜 | 模型平局 | 模型客胜 | 市场主胜 | 市场平局 | 市场客胜 | SP 主胜 | SP 平局 | SP 客胜 | EV 主胜 | EV 平局 | EV 客胜 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| jc:2042690 / 周三005 | 英锦标赛 / 谢周三 vs 阿克宁顿 | 2026-11-12 03:00:00 | 排除：预测截点时已开赛 | — | — | — | — | — | — | — | — | — | — | — | — |
| jc:2042691 / 周三006 | 英锦标赛 / 巴恩斯利 vs 奥德汉姆 | 2026-11-12 03:00:00 | 排除：预测截点时已开赛 | — | — | — | — | — | — | — | — | — | — | — | — |

## 比分前五与快照

