# P2 预测报告

> **非购买建议 · BET 禁用 · 各联赛P2历史评估门槛见逐场状态**

常规 90 分钟含补时。纯模型仅使用球队历史比分；市场 SP 和去水概率单独对照。
EV 为模型概率 × SP − 1 的点估计，EV > 0.05 仅加粗显示，不构成 BET 或购买建议。

批次：42998472550-1-2026-11-12；模式：prematch。
生成时间（北京时间）：2026-11-11 23:35:51；数据截止（北京时间）：2026-11-11 23:35:49。

## 模型与训练范围

版本为模型文件 SHA256；所有展示时间均为北京时间。assumed 占比表示使用假定赛果可知时间的训练场次占比，不代表已核实发布时间。

| 模型 | 版本 SHA256 | 来源 | 联赛 | 模型截点 | 训练场数 | 最近赛果可知时间 | 可知依据 | assumed 占比 | 训练库 SHA256 | 评估库 SHA256 | 数据库说明 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dixon-coles | ff97687e9a25361cda780708028485dec8714e1de7902339a16206634afb32ba | fd,jc | 德甲 | 2026-11-11 23:33:16 | 1611 | 2026-11-11 04:14:29 | mixed | 95.53% | 4ecc03198ef8ead67991422f5cdb5768b95802e7be5049af38b4ae2ba01bcc79 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | fdffa132abca7c97289a31dfa8d14e38059b0521f04432d7e33d78f800eeb983 | fd,jc | 意甲 | 2026-11-11 23:33:16 | 2010 | 2026-11-11 04:14:29 | mixed | 95.52% | 4ecc03198ef8ead67991422f5cdb5768b95802e7be5049af38b4ae2ba01bcc79 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 1e307832f669a18770793f559d23f1b084961c622c3e6b4e15f33986befc5cda | fd,jc | 西甲 | 2026-11-11 23:33:16 | 2018 | 2026-11-11 04:14:29 | mixed | 95.69% | 4ecc03198ef8ead67991422f5cdb5768b95802e7be5049af38b4ae2ba01bcc79 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | beeae94c95284fc4ad26f3d95051318d15a48e776bffc7fffc81e9e22987629e | fd,jc | 英超 | 2026-11-11 23:33:16 | 1992 | 2026-11-11 04:14:29 | mixed | 96.39% | 4ecc03198ef8ead67991422f5cdb5768b95802e7be5049af38b4ae2ba01bcc79 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 6a2e30e2deaa3b97363c51db56810cf4fc258c75fb36142716197ff7b6ba43b4 | fd,jc | 法甲 | 2026-11-11 23:33:16 | 1762 | 2026-11-11 04:14:29 | mixed | 97.67% | 4ecc03198ef8ead67991422f5cdb5768b95802e7be5049af38b4ae2ba01bcc79 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 27e0fb4b942aa8cf973ce4c4af66eb78c887f91e9c0078c6e2d794fcdf5b8ca9 | fd,jc | 荷甲 | 2026-11-11 23:33:16 | 1631 | 2026-11-11 04:14:01 | mixed | 97.85% | 4ecc03198ef8ead67991422f5cdb5768b95802e7be5049af38b4ae2ba01bcc79 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 0b44da493a1aca5256056302f09de98c3ea593dc628245afe0a3399052939429 | fd,jc | 葡超 | 2026-11-11 23:33:16 | 1617 | 2026-11-11 04:14:29 | mixed | 98.27% | 4ecc03198ef8ead67991422f5cdb5768b95802e7be5049af38b4ae2ba01bcc79 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 93ef9e68c21cee8525151186031980cdd6f44704402b1d327350b5fe47b64586 | fd,jc | 英冠 | 2026-11-11 23:33:16 | 2920 | 2026-11-08 17:52:48 | mixed | 98.77% | 4ecc03198ef8ead67991422f5cdb5768b95802e7be5049af38b4ae2ba01bcc79 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 153772ba1dc4f98ac8a1933efc5c4197ff0656ae8f8597fed14cc76e4abbae5f | fd,jc | 瑞超 | 2026-11-11 23:33:16 | 1410 | 2026-11-11 04:14:01 | mixed | 98.30% | 4ecc03198ef8ead67991422f5cdb5768b95802e7be5049af38b4ae2ba01bcc79 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 02770aeab912c2f95c6406b78b8520fa238a576db55ce3b613f104e5a8e85fa6 | fd,jc | 挪超 | 2026-11-11 23:33:16 | 1392 | 2026-11-11 04:14:01 | mixed | 98.49% | 4ecc03198ef8ead67991422f5cdb5768b95802e7be5049af38b4ae2ba01bcc79 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | ce3df120395cd5ee6cb0bd28c269cba24ff72e006d31407d1bac450d3cdfca9b | fd,jc | 法乙 | 2026-11-11 23:33:16 | 1841 | 2026-11-07 17:26:10 | mixed | 99.57% | 4ecc03198ef8ead67991422f5cdb5768b95802e7be5049af38b4ae2ba01bcc79 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 9dd54ad2cf3cae38c7318e577eb9d31807cb793995d03b7cf8c2e69729c65dca | fd,jc | 日职 | 2026-11-11 23:33:16 | 1876 | 2026-11-08 17:53:22 | mixed | 98.08% | 4ecc03198ef8ead67991422f5cdb5768b95802e7be5049af38b4ae2ba01bcc79 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 82e025f48c860284082dad38c300e046c7d217b5ade00236446cba495a99c22b | fd,jc | 芬超 | 2026-11-11 23:33:16 | 1016 | 2026-11-11 04:14:01 | mixed | 98.72% | 4ecc03198ef8ead67991422f5cdb5768b95802e7be5049af38b4ae2ba01bcc79 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 2623d9376332506048bae34e7b426dc5bed12f0fc833ec909a9f1f0bc4845d65 | fd,jc | 巴甲 | 2026-11-11 23:33:16 | 2223 | 2026-11-07 17:26:10 | mixed | 98.56% | 4ecc03198ef8ead67991422f5cdb5768b95802e7be5049af38b4ae2ba01bcc79 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | f6e1ad4b835e5491e9e50c06aaf1894950a500ed1f6128f28d47180f13d4084c | fd,jc | 美职 | 2026-11-11 23:33:16 | 2969 | 2026-11-08 17:53:22 | mixed | 99.33% | 4ecc03198ef8ead67991422f5cdb5768b95802e7be5049af38b4ae2ba01bcc79 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 7de57fab7cf63fed84e647826582b401218684b3a5f32db53e16ab97e2368c55 | apif,fd,fdorg,jc | 欧战 | 2026-11-11 23:33:16 | 20604 | 2026-11-11 04:14:29 | mixed | 96.92% | 4ecc03198ef8ead67991422f5cdb5768b95802e7be5049af38b4ae2ba01bcc79 | 385035b903b4aa575365a8635f187e7d18bd49b5c24ab46cdfdfd5ed386ea1dd | 评估库 ≠ 训练库 |
| dixon-coles | db5507d4e59aee1b44d7b116cd5a05afc426615ceb15a28be9686512051839a4 | apif,jc | 欧国联 | 2026-11-11 23:33:16 | 766 | 2026-11-01 21:08:02 | mixed | 93.21% | 4ecc03198ef8ead67991422f5cdb5768b95802e7be5049af38b4ae2ba01bcc79 | — | — |

来源列为显式选择的训练语料范围；实际符合截点的训练来源及场数见 forecast.json 的 training_source_counts（null 表示旧模型未记录）。

## 纯模型与市场对照

| 比赛 ID / 编号 | 联赛 / 对阵 | 开球（北京时间） | 状态 / 排除原因 | 模型主胜 | 模型平局 | 模型客胜 | 市场主胜 | 市场平局 | 市场客胜 | SP 主胜 | SP 平局 | SP 客胜 | EV 主胜 | EV 平局 | EV 客胜 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |

## 比分前五与快照

