# P2 预测报告

> **非购买建议 · BET 禁用 · 各联赛P2历史评估门槛见逐场状态**

常规 90 分钟含补时。纯模型仅使用球队历史比分；市场 SP 和去水概率单独对照。
EV 为模型概率 × SP − 1 的点估计，EV > 0.05 仅加粗显示，不构成 BET 或购买建议。

批次：41877829928-1-2026-11-06；模式：prematch。
生成时间（北京时间）：2026-11-05 01:37:47；数据截止（北京时间）：2026-11-05 01:37:47。

## 模型与训练范围

版本为模型文件 SHA256；所有展示时间均为北京时间。assumed 占比表示使用假定赛果可知时间的训练场次占比，不代表已核实发布时间。

| 模型 | 版本 SHA256 | 来源 | 联赛 | 模型截点 | 训练场数 | 最近赛果可知时间 | 可知依据 | assumed 占比 | 训练库 SHA256 | 评估库 SHA256 | 数据库说明 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dixon-coles | fb80031256a82b65ff9fc10c9a41b79726072f9c1b74618dc98707c73f381cb9 | fd,jc | 德甲 | 2026-11-05 01:35:00 | 1602 | 2026-11-02 22:58:20 | mixed | 96.07% | a3a7057a5774f9705daddfb2ba0a8112bfd3c0e8ea33c69d57305995e55554fb | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | cb02ea98d0bf4c835f8292cadaf015766dca7403624a8bbacd2a4ac7c49550cb | fd,jc | 意甲 | 2026-11-05 01:35:00 | 2000 | 2026-11-03 18:07:17 | mixed | 96.00% | a3a7057a5774f9705daddfb2ba0a8112bfd3c0e8ea33c69d57305995e55554fb | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 47f7867441fbf0367976f4dbd4ed1d4502dfceabd77b7ecfe8ae43eebef7ae6d | fd,jc | 西甲 | 2026-11-05 01:35:00 | 2009 | 2026-11-03 18:07:17 | mixed | 96.12% | a3a7057a5774f9705daddfb2ba0a8112bfd3c0e8ea33c69d57305995e55554fb | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | f1dc525928eed65e612dd191a7dd21a0090f0d4216173f5f55c5687d2cf5301b | fd,jc | 英超 | 2026-11-05 01:35:00 | 1983 | 2026-11-03 18:07:17 | mixed | 96.82% | a3a7057a5774f9705daddfb2ba0a8112bfd3c0e8ea33c69d57305995e55554fb | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 7b32e26d0b87217d1a7450f0e41a70f6361ee2388c88f754ec748694d0f17c51 | fd,jc | 法甲 | 2026-11-05 01:35:00 | 1758 | 2026-11-02 22:58:20 | mixed | 97.90% | a3a7057a5774f9705daddfb2ba0a8112bfd3c0e8ea33c69d57305995e55554fb | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | a4e2827602b6bf834d705c9528a98a1d89e62ce17087906a1af2f00624edddd8 | fd,jc | 荷甲 | 2026-11-05 01:35:00 | 1627 | 2026-11-02 22:57:58 | mixed | 98.09% | a3a7057a5774f9705daddfb2ba0a8112bfd3c0e8ea33c69d57305995e55554fb | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | c7513b756dc562c77b8e42566200e3c8f71ef5ca13a3b8c841261c7c88b7cd45 | fd,jc | 葡超 | 2026-11-05 01:35:00 | 1614 | 2026-11-03 18:07:17 | mixed | 98.45% | a3a7057a5774f9705daddfb2ba0a8112bfd3c0e8ea33c69d57305995e55554fb | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 4a3daff8e599f4b66b252963182eee8eb107639771d11923954a7afa00b4cbc9 | fd,jc | 英冠 | 2026-11-05 01:35:00 | 2916 | 2026-11-01 21:08:29 | mixed | 98.90% | a3a7057a5774f9705daddfb2ba0a8112bfd3c0e8ea33c69d57305995e55554fb | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | a2c0ab26964c5d56ce830e4ac8360da01cd2ff870b137dd65d0b9e31e58b26f7 | fd,jc | 瑞超 | 2026-11-05 01:35:00 | 1407 | 2026-11-03 18:07:17 | mixed | 98.51% | a3a7057a5774f9705daddfb2ba0a8112bfd3c0e8ea33c69d57305995e55554fb | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | c8ad8c7ebd5c5b43653e2ebab8e375f1b58a4bc7282b27e66cfdabea69474426 | fd,jc | 挪超 | 2026-11-05 01:35:00 | 1391 | 2026-11-02 22:57:58 | mixed | 98.56% | a3a7057a5774f9705daddfb2ba0a8112bfd3c0e8ea33c69d57305995e55554fb | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 6adcafdf1bbb6c6abcd7de7c79d1e1ad80b016ae7b8e8da2244fdac9c3c6ebd0 | fd,jc | 法乙 | 2026-11-05 01:35:00 | 1840 | 2026-11-03 18:07:17 | mixed | 99.62% | a3a7057a5774f9705daddfb2ba0a8112bfd3c0e8ea33c69d57305995e55554fb | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 544c3b42807f6e4cda2e753b142845709009c126757a0ba480e8beacd22de34a | fd,jc | 日职 | 2026-11-05 01:35:00 | 1872 | 2026-11-01 21:26:51 | mixed | 98.29% | a3a7057a5774f9705daddfb2ba0a8112bfd3c0e8ea33c69d57305995e55554fb | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | c59298813b6ad251f7027f01b4ba71a02d66d895066e8154ddd44ec061ccaf85 | fd,jc | 芬超 | 2026-11-05 01:35:00 | 1015 | 2026-11-03 18:07:17 | mixed | 98.82% | a3a7057a5774f9705daddfb2ba0a8112bfd3c0e8ea33c69d57305995e55554fb | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 90a66af54f5bc43f3a471ae60dca91a74ac3bac4ae284b480adfb75887847629 | fd,jc | 巴甲 | 2026-11-05 01:35:00 | 2221 | 2026-11-03 18:07:17 | mixed | 98.65% | a3a7057a5774f9705daddfb2ba0a8112bfd3c0e8ea33c69d57305995e55554fb | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 4cf7ede72077b2e27fc2eb71950eeb9eaf8dcaf2a53b64d2e570641bec1e7151 | fd,jc | 美职 | 2026-11-05 01:35:00 | 2968 | 2026-11-02 22:58:20 | mixed | 99.36% | a3a7057a5774f9705daddfb2ba0a8112bfd3c0e8ea33c69d57305995e55554fb | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 0f7b0e788fae9eff03b33e70dae01279ef4296b16258cd17a06423dba8cf4288 | apif,fd,fdorg,jc | 欧战 | 2026-11-05 01:35:00 | 20496 | 2026-11-05 01:29:11 | mixed | 97.43% | a3a7057a5774f9705daddfb2ba0a8112bfd3c0e8ea33c69d57305995e55554fb | 385035b903b4aa575365a8635f187e7d18bd49b5c24ab46cdfdfd5ed386ea1dd | 评估库 ≠ 训练库 |

来源列为显式选择的训练语料范围；实际符合截点的训练来源及场数见 forecast.json 的 training_source_counts（null 表示旧模型未记录）。

## 纯模型与市场对照

| 比赛 ID / 编号 | 联赛 / 对阵 | 开球（北京时间） | 状态 / 排除原因 | 模型主胜 | 模型平局 | 模型客胜 | 市场主胜 | 市场平局 | 市场客胜 | SP 主胜 | SP 平局 | SP 客胜 | EV 主胜 | EV 平局 | EV 客胜 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |

## 比分前五与快照

