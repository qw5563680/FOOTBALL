# P2 预测报告

> **非购买建议 · BET 禁用 · 各联赛P2历史评估门槛见逐场状态**

常规 90 分钟含补时。纯模型仅使用球队历史比分；市场 SP 和去水概率单独对照。
EV 为模型概率 × SP − 1 的点估计，EV > 0.05 仅加粗显示，不构成 BET 或购买建议。

批次：42498987096-1-2026-11-10；模式：prematch。
生成时间（北京时间）：2026-11-09 00:41:50；数据截止（北京时间）：2026-11-09 00:41:50。

## 模型与训练范围

版本为模型文件 SHA256；所有展示时间均为北京时间。assumed 占比表示使用假定赛果可知时间的训练场次占比，不代表已核实发布时间。

| 模型 | 版本 SHA256 | 来源 | 联赛 | 模型截点 | 训练场数 | 最近赛果可知时间 | 可知依据 | assumed 占比 | 训练库 SHA256 | 评估库 SHA256 | 数据库说明 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dixon-coles | a1ff9e149988da435c4d10f66975fefeed1038386bc71c7934060a82ececf5ac | fd,jc | 德甲 | 2026-11-09 00:40:17 | 1608 | 2026-11-08 17:53:22 | mixed | 95.71% | 3aa3ffcc89e4a0f22e4e3b1588114ffeac85200fe70e4da7573ee05d51674722 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 6d2d08efcb8210e2934a977feabc6e2c7637718e8b78ed168d5e8bc604f3d9d8 | fd,jc | 意甲 | 2026-11-09 00:40:17 | 2006 | 2026-11-09 00:36:53 | mixed | 95.71% | 3aa3ffcc89e4a0f22e4e3b1588114ffeac85200fe70e4da7573ee05d51674722 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 306641ccae7dbfd527bddfef1bf9843a2568ad3fee5c02ae74dfcb9d1b88982b | fd,jc | 西甲 | 2026-11-09 00:40:17 | 2013 | 2026-11-08 17:53:22 | mixed | 95.93% | 3aa3ffcc89e4a0f22e4e3b1588114ffeac85200fe70e4da7573ee05d51674722 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 30a52c0115985ed477c95f40a4b4efa42403182d3b7c5aa58628cd8593a39f11 | fd,jc | 英超 | 2026-11-09 00:40:17 | 1988 | 2026-11-08 17:53:22 | mixed | 96.58% | 3aa3ffcc89e4a0f22e4e3b1588114ffeac85200fe70e4da7573ee05d51674722 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | c2ee48ad76a94b34f37bb4135e5f807120c0abdd8df4dfb3ad72203216e8c27c | fd,jc | 法甲 | 2026-11-09 00:40:17 | 1760 | 2026-11-08 17:53:22 | mixed | 97.78% | 3aa3ffcc89e4a0f22e4e3b1588114ffeac85200fe70e4da7573ee05d51674722 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 1776c15aba8053d8761d3c6ba548ac31e1f0d321c2b939383a8f6a3a6c1427e5 | fd,jc | 荷甲 | 2026-11-09 00:40:17 | 1629 | 2026-11-08 17:52:48 | mixed | 97.97% | 3aa3ffcc89e4a0f22e4e3b1588114ffeac85200fe70e4da7573ee05d51674722 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 6dc76ddbfd7c653a85beb6d7c892f4fea7b8eea8b8a0939130e08d9161c49071 | fd,jc | 葡超 | 2026-11-09 00:40:17 | 1616 | 2026-11-08 17:53:22 | mixed | 98.33% | 3aa3ffcc89e4a0f22e4e3b1588114ffeac85200fe70e4da7573ee05d51674722 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 1a1fd4da42b5537e69c445672bb3172a23c9fcda7ec95fd3145e6355f8fcc33a | fd,jc | 英冠 | 2026-11-09 00:40:17 | 2920 | 2026-11-08 17:52:48 | mixed | 98.77% | 3aa3ffcc89e4a0f22e4e3b1588114ffeac85200fe70e4da7573ee05d51674722 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 6314866b47240538000da59295405a0403c49e4d9d8b9b19293a6b77e0d68a94 | fd,jc | 瑞超 | 2026-11-09 00:40:17 | 1409 | 2026-11-08 17:53:22 | mixed | 98.37% | 3aa3ffcc89e4a0f22e4e3b1588114ffeac85200fe70e4da7573ee05d51674722 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 892e543974a942581b6981e0c9febe49c502138f25371a7ac54a08daaa628205 | fd,jc | 挪超 | 2026-11-09 00:40:17 | 1391 | 2026-11-02 22:57:58 | mixed | 98.56% | 3aa3ffcc89e4a0f22e4e3b1588114ffeac85200fe70e4da7573ee05d51674722 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 5e04afabcc67422306cbe0cb526c2a17a4250bfb33a51d4b43c3508d16ae6c86 | fd,jc | 法乙 | 2026-11-09 00:40:17 | 1841 | 2026-11-07 17:26:10 | mixed | 99.57% | 3aa3ffcc89e4a0f22e4e3b1588114ffeac85200fe70e4da7573ee05d51674722 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 8af9a492c565456f1510fb68cac9594396c010f7fe9bf09622df06336be378fb | fd,jc | 日职 | 2026-11-09 00:40:17 | 1876 | 2026-11-08 17:53:22 | mixed | 98.08% | 3aa3ffcc89e4a0f22e4e3b1588114ffeac85200fe70e4da7573ee05d51674722 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 554bf04625d7c5fceb2f08820c82cf595b3bf092a75e9458c4e45adf924c7395 | fd,jc | 芬超 | 2026-11-09 00:40:17 | 1015 | 2026-11-03 18:07:17 | mixed | 98.82% | 3aa3ffcc89e4a0f22e4e3b1588114ffeac85200fe70e4da7573ee05d51674722 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | c747bd686115673c9e3e54c12b507e515632c1df9cd7effaaa6f4d3a7057812c | fd,jc | 巴甲 | 2026-11-09 00:40:17 | 2223 | 2026-11-07 17:26:10 | mixed | 98.56% | 3aa3ffcc89e4a0f22e4e3b1588114ffeac85200fe70e4da7573ee05d51674722 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 7670f6cb52f37aa70668d3bf5cb9484cf54194a2f3c38ff2f5fc73d75ccf4d55 | fd,jc | 美职 | 2026-11-09 00:40:17 | 2969 | 2026-11-08 17:53:22 | mixed | 99.33% | 3aa3ffcc89e4a0f22e4e3b1588114ffeac85200fe70e4da7573ee05d51674722 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 03e94a06237fcb5b8fe47078470093b98bb2694bb37471e965a053a1698378f2 | apif,fd,fdorg,jc | 欧战 | 2026-11-09 00:40:17 | 20566 | 2026-11-09 00:36:53 | mixed | 97.10% | 3aa3ffcc89e4a0f22e4e3b1588114ffeac85200fe70e4da7573ee05d51674722 | 385035b903b4aa575365a8635f187e7d18bd49b5c24ab46cdfdfd5ed386ea1dd | 评估库 ≠ 训练库 |

来源列为显式选择的训练语料范围；实际符合截点的训练来源及场数见 forecast.json 的 training_source_counts（null 表示旧模型未记录）。

## 纯模型与市场对照

| 比赛 ID / 编号 | 联赛 / 对阵 | 开球（北京时间） | 状态 / 排除原因 | 模型主胜 | 模型平局 | 模型客胜 | 市场主胜 | 市场平局 | 市场客胜 | SP 主胜 | SP 平局 | SP 客胜 | EV 主胜 | EV 平局 | EV 客胜 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |

## 比分前五与快照

