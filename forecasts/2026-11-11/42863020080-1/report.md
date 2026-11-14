# P2 预测报告

> **非购买建议 · BET 禁用 · 各联赛P2历史评估门槛见逐场状态**

常规 90 分钟含补时。纯模型仅使用球队历史比分；市场 SP 和去水概率单独对照。
EV 为模型概率 × SP − 1 的点估计，EV > 0.05 仅加粗显示，不构成 BET 或购买建议。

批次：42863020080-1-2026-11-11；模式：prematch。
生成时间（北京时间）：2026-11-11 04:18:51；数据截止（北京时间）：2026-11-11 04:18:50。

## 模型与训练范围

版本为模型文件 SHA256；所有展示时间均为北京时间。assumed 占比表示使用假定赛果可知时间的训练场次占比，不代表已核实发布时间。

| 模型 | 版本 SHA256 | 来源 | 联赛 | 模型截点 | 训练场数 | 最近赛果可知时间 | 可知依据 | assumed 占比 | 训练库 SHA256 | 评估库 SHA256 | 数据库说明 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dixon-coles | 0f9358d0d042a3245b8c0b52c83a46139e2a7b89111f51c6d605094e1529f2f6 | fd,jc | 德甲 | 2026-11-11 04:16:21 | 1611 | 2026-11-11 04:14:29 | mixed | 95.53% | cbaed1c72c9c960b1cd8a5aecdbcedea80a03d51572902833090c566c3633a73 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | a6abeb499dcf1a009f621fecec8e2108e9a1301104b6fed4b9f7c6b2c2de21c2 | fd,jc | 意甲 | 2026-11-11 04:16:21 | 2010 | 2026-11-11 04:14:29 | mixed | 95.52% | cbaed1c72c9c960b1cd8a5aecdbcedea80a03d51572902833090c566c3633a73 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | f4566304501f5fdc14e2df6ae6b545cfc2c4b974fc7fd9cf70b52dc028d71979 | fd,jc | 西甲 | 2026-11-11 04:16:21 | 2018 | 2026-11-11 04:14:29 | mixed | 95.69% | cbaed1c72c9c960b1cd8a5aecdbcedea80a03d51572902833090c566c3633a73 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 6f4a4e53876e6ab94627a30a35d05136abade17534f855386ac94e222aa0345a | fd,jc | 英超 | 2026-11-11 04:16:21 | 1992 | 2026-11-11 04:14:29 | mixed | 96.39% | cbaed1c72c9c960b1cd8a5aecdbcedea80a03d51572902833090c566c3633a73 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 3ceeed23e87e16cedc5176f0bf822c1577667b135cc6c413cf651544d37cc6d9 | fd,jc | 法甲 | 2026-11-11 04:16:21 | 1762 | 2026-11-11 04:14:29 | mixed | 97.67% | cbaed1c72c9c960b1cd8a5aecdbcedea80a03d51572902833090c566c3633a73 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 3e49c1761067b01ba1ed679e954c1172ad765e9c63757a904f6856c29a757dfb | fd,jc | 荷甲 | 2026-11-11 04:16:21 | 1631 | 2026-11-11 04:14:01 | mixed | 97.85% | cbaed1c72c9c960b1cd8a5aecdbcedea80a03d51572902833090c566c3633a73 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 4e04a79b56c7e402f6a0229110bd6d755e8e3dc03516d40a8c4268865ae9bf79 | fd,jc | 葡超 | 2026-11-11 04:16:21 | 1617 | 2026-11-11 04:14:29 | mixed | 98.27% | cbaed1c72c9c960b1cd8a5aecdbcedea80a03d51572902833090c566c3633a73 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 5e6cace825735ef1895677a0488aa5848f126c5be18e871ee95d56d6d78a9d11 | fd,jc | 英冠 | 2026-11-11 04:16:21 | 2920 | 2026-11-08 17:52:48 | mixed | 98.77% | cbaed1c72c9c960b1cd8a5aecdbcedea80a03d51572902833090c566c3633a73 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 5018ba298db94a30c3595433b7bc77cb412df9e252654d36c9a5cc297f974e5b | fd,jc | 瑞超 | 2026-11-11 04:16:21 | 1410 | 2026-11-11 04:14:01 | mixed | 98.30% | cbaed1c72c9c960b1cd8a5aecdbcedea80a03d51572902833090c566c3633a73 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | e2996fd0ca5eda1535c0acf8c66e20456a87f432f013b2ce90822aedc5e0ccb2 | fd,jc | 挪超 | 2026-11-11 04:16:21 | 1392 | 2026-11-11 04:14:01 | mixed | 98.49% | cbaed1c72c9c960b1cd8a5aecdbcedea80a03d51572902833090c566c3633a73 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | e883a005e56bb754d3e493e344ca1bf55051592a62c3559ddc160e6fdfde2bef | fd,jc | 法乙 | 2026-11-11 04:16:21 | 1841 | 2026-11-07 17:26:10 | mixed | 99.57% | cbaed1c72c9c960b1cd8a5aecdbcedea80a03d51572902833090c566c3633a73 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 977fb2fb1acc966d9086266c56175576455c64966b0d10118ea832b7a7fa43d6 | fd,jc | 日职 | 2026-11-11 04:16:21 | 1876 | 2026-11-08 17:53:22 | mixed | 98.08% | cbaed1c72c9c960b1cd8a5aecdbcedea80a03d51572902833090c566c3633a73 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | fea98409a93f4fd5fddfb39ab65ca57e588b7abfcc77eb31e5558a53c3e36db8 | fd,jc | 芬超 | 2026-11-11 04:16:21 | 1016 | 2026-11-11 04:14:01 | mixed | 98.72% | cbaed1c72c9c960b1cd8a5aecdbcedea80a03d51572902833090c566c3633a73 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | e23a4d637c3912e5b035ff4151ff7d1adca22f113bb47068c912a0274bb78656 | fd,jc | 巴甲 | 2026-11-11 04:16:21 | 2223 | 2026-11-07 17:26:10 | mixed | 98.56% | cbaed1c72c9c960b1cd8a5aecdbcedea80a03d51572902833090c566c3633a73 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 1a4952f42a25160a83281ce78a6ab64c31c0eb7dd32e2d7d18de4563362a2667 | fd,jc | 美职 | 2026-11-11 04:16:21 | 2969 | 2026-11-08 17:53:22 | mixed | 99.33% | cbaed1c72c9c960b1cd8a5aecdbcedea80a03d51572902833090c566c3633a73 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 243b8bfa3ef8885265d3a163faff8ad0e27e2545a93f207f9c52a5971da29640 | apif,fd,fdorg,jc | 欧战 | 2026-11-11 04:16:21 | 20604 | 2026-11-11 04:14:29 | mixed | 96.92% | cbaed1c72c9c960b1cd8a5aecdbcedea80a03d51572902833090c566c3633a73 | 385035b903b4aa575365a8635f187e7d18bd49b5c24ab46cdfdfd5ed386ea1dd | 评估库 ≠ 训练库 |
| dixon-coles | 9563a2ba5c2664f2d0328bdbedce853338fb3216188112fe485b233b18cb5b30 | apif,jc | 欧国联 | 2026-11-11 04:16:21 | 766 | 2026-11-01 21:08:02 | mixed | 93.21% | cbaed1c72c9c960b1cd8a5aecdbcedea80a03d51572902833090c566c3633a73 | — | — |

来源列为显式选择的训练语料范围；实际符合截点的训练来源及场数见 forecast.json 的 training_source_counts（null 表示旧模型未记录）。

## 纯模型与市场对照

| 比赛 ID / 编号 | 联赛 / 对阵 | 开球（北京时间） | 状态 / 排除原因 | 模型主胜 | 模型平局 | 模型客胜 | 市场主胜 | 市场平局 | 市场客胜 | SP 主胜 | SP 平局 | SP 客胜 | EV 主胜 | EV 平局 | EV 客胜 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |

## 比分前五与快照

