# P2 预测报告

> **非购买建议 · BET 禁用 · 各联赛P2历史评估门槛见逐场状态**

常规 90 分钟含补时。纯模型仅使用球队历史比分；市场 SP 和去水概率单独对照。
EV 为模型概率 × SP − 1 的点估计，EV > 0.05 仅加粗显示，不构成 BET 或购买建议。

批次：42950468933-1-2026-11-11；模式：prematch。
生成时间（北京时间）：2026-11-11 23:00:56；数据截止（北京时间）：2026-11-11 23:00:55。

## 模型与训练范围

版本为模型文件 SHA256；所有展示时间均为北京时间。assumed 占比表示使用假定赛果可知时间的训练场次占比，不代表已核实发布时间。

| 模型 | 版本 SHA256 | 来源 | 联赛 | 模型截点 | 训练场数 | 最近赛果可知时间 | 可知依据 | assumed 占比 | 训练库 SHA256 | 评估库 SHA256 | 数据库说明 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dixon-coles | 223dc3e7270ea9601f1867c0a08e3b7e17ca1b1314c893c0764875d0422e3665 | fd,jc | 德甲 | 2026-11-11 22:59:27 | 1611 | 2026-11-11 04:14:29 | mixed | 95.53% | 6a35bd0ca1c110ba01f287b9085b70a738fee41645bc94187978b0deba7a9700 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | fdde89e6b1f00de59f14c61445baaa964c02ceea2e9806fd8eeca1814f7df3a8 | fd,jc | 意甲 | 2026-11-11 22:59:27 | 2010 | 2026-11-11 04:14:29 | mixed | 95.52% | 6a35bd0ca1c110ba01f287b9085b70a738fee41645bc94187978b0deba7a9700 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | dfc93054cd778c94d53635c7fb1ef421f7f79832cf4b60040b52f91e0e8474b1 | fd,jc | 西甲 | 2026-11-11 22:59:27 | 2018 | 2026-11-11 04:14:29 | mixed | 95.69% | 6a35bd0ca1c110ba01f287b9085b70a738fee41645bc94187978b0deba7a9700 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | c621c9f804a8e58e39f7dd30e454657b5040ecb76b993437fdeebadf372d06fa | fd,jc | 英超 | 2026-11-11 22:59:27 | 1992 | 2026-11-11 04:14:29 | mixed | 96.39% | 6a35bd0ca1c110ba01f287b9085b70a738fee41645bc94187978b0deba7a9700 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 1912d33c082ad8cd3a979d26ab6aa6545777f5dfa6a438624219bac10f37336d | fd,jc | 法甲 | 2026-11-11 22:59:27 | 1762 | 2026-11-11 04:14:29 | mixed | 97.67% | 6a35bd0ca1c110ba01f287b9085b70a738fee41645bc94187978b0deba7a9700 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 682d3d4c786a76bbf9fee5b8419f98c274aac11b8d5bf35cacbf7c3722b3fce9 | fd,jc | 荷甲 | 2026-11-11 22:59:27 | 1631 | 2026-11-11 04:14:01 | mixed | 97.85% | 6a35bd0ca1c110ba01f287b9085b70a738fee41645bc94187978b0deba7a9700 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 80be555e5d014f3cde9c4272eed442e87394469c0c79a3118b4d0ce36e463c5e | fd,jc | 葡超 | 2026-11-11 22:59:27 | 1617 | 2026-11-11 04:14:29 | mixed | 98.27% | 6a35bd0ca1c110ba01f287b9085b70a738fee41645bc94187978b0deba7a9700 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 97ad5e96c4175e35cb4fb8948f2c091c4affa00f5b39d96bae273a9cc7d8ba09 | fd,jc | 英冠 | 2026-11-11 22:59:27 | 2920 | 2026-11-08 17:52:48 | mixed | 98.77% | 6a35bd0ca1c110ba01f287b9085b70a738fee41645bc94187978b0deba7a9700 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 18186311ae136bfee92f43475353234caf87e3a7426b626b9a03c7c450acab9b | fd,jc | 瑞超 | 2026-11-11 22:59:27 | 1410 | 2026-11-11 04:14:01 | mixed | 98.30% | 6a35bd0ca1c110ba01f287b9085b70a738fee41645bc94187978b0deba7a9700 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 8f3be0c06e5ac81302b7d1b79f0f6914559ceba8eb9fe0a1dd2967ae13dd2f0b | fd,jc | 挪超 | 2026-11-11 22:59:27 | 1392 | 2026-11-11 04:14:01 | mixed | 98.49% | 6a35bd0ca1c110ba01f287b9085b70a738fee41645bc94187978b0deba7a9700 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | de176351034dfee98b29ffb5f96aae06d17c1427effd2f66f514e5078a9078ed | fd,jc | 法乙 | 2026-11-11 22:59:27 | 1841 | 2026-11-07 17:26:10 | mixed | 99.57% | 6a35bd0ca1c110ba01f287b9085b70a738fee41645bc94187978b0deba7a9700 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 874fafac051573b33054dd2ca06cddaa51cad599d70e1971cbb56d323e99d987 | fd,jc | 日职 | 2026-11-11 22:59:27 | 1876 | 2026-11-08 17:53:22 | mixed | 98.08% | 6a35bd0ca1c110ba01f287b9085b70a738fee41645bc94187978b0deba7a9700 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 35f05f6c270f6dbe4206de17eefea9d785f7ea25a31c78b12f44424a7ed035ca | fd,jc | 芬超 | 2026-11-11 22:59:27 | 1016 | 2026-11-11 04:14:01 | mixed | 98.72% | 6a35bd0ca1c110ba01f287b9085b70a738fee41645bc94187978b0deba7a9700 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 655de8f3e6a7d24283f7c6285cc71dbf5e0fe1ee3630f99885266a2c1cfb23fb | fd,jc | 巴甲 | 2026-11-11 22:59:27 | 2223 | 2026-11-07 17:26:10 | mixed | 98.56% | 6a35bd0ca1c110ba01f287b9085b70a738fee41645bc94187978b0deba7a9700 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 16c81f1d17a63485634dec788b25f062f8ae5507e669c1c1c15d32671ece64fb | fd,jc | 美职 | 2026-11-11 22:59:27 | 2969 | 2026-11-08 17:53:22 | mixed | 99.33% | 6a35bd0ca1c110ba01f287b9085b70a738fee41645bc94187978b0deba7a9700 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 3cf8dab37ad2ede88af43ab4af5fce8658f697525cf878349f3f56884167fe49 | apif,fd,fdorg,jc | 欧战 | 2026-11-11 22:59:27 | 20604 | 2026-11-11 04:14:29 | mixed | 96.92% | 6a35bd0ca1c110ba01f287b9085b70a738fee41645bc94187978b0deba7a9700 | 385035b903b4aa575365a8635f187e7d18bd49b5c24ab46cdfdfd5ed386ea1dd | 评估库 ≠ 训练库 |
| dixon-coles | 7e662523849acef93e0c80603d86516582840db3a665cf198b41bfe178549103 | apif,jc | 欧国联 | 2026-11-11 22:59:27 | 766 | 2026-11-01 21:08:02 | mixed | 93.21% | 6a35bd0ca1c110ba01f287b9085b70a738fee41645bc94187978b0deba7a9700 | — | — |

来源列为显式选择的训练语料范围；实际符合截点的训练来源及场数见 forecast.json 的 training_source_counts（null 表示旧模型未记录）。

## 纯模型与市场对照

| 比赛 ID / 编号 | 联赛 / 对阵 | 开球（北京时间） | 状态 / 排除原因 | 模型主胜 | 模型平局 | 模型客胜 | 市场主胜 | 市场平局 | 市场客胜 | SP 主胜 | SP 平局 | SP 客胜 | EV 主胜 | EV 平局 | EV 客胜 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |

## 比分前五与快照

