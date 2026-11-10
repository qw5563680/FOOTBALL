# P2 预测报告

> **非购买建议 · BET 禁用 · 各联赛P2历史评估门槛见逐场状态**

常规 90 分钟含补时。纯模型仅使用球队历史比分；市场 SP 和去水概率单独对照。
EV 为模型概率 × SP − 1 的点估计，EV > 0.05 仅加粗显示，不构成 BET 或购买建议。

批次：42798481499-1-2026-11-11；模式：prematch。
生成时间（北京时间）：2026-11-11 03:50:13；数据截止（北京时间）：2026-11-11 03:50:12。

## 模型与训练范围

版本为模型文件 SHA256；所有展示时间均为北京时间。assumed 占比表示使用假定赛果可知时间的训练场次占比，不代表已核实发布时间。

| 模型 | 版本 SHA256 | 来源 | 联赛 | 模型截点 | 训练场数 | 最近赛果可知时间 | 可知依据 | assumed 占比 | 训练库 SHA256 | 评估库 SHA256 | 数据库说明 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dixon-coles | 8786e79360981e0d546b30a826da174c260111fc19c05210e4731382a0ee2700 | fd,jc | 德甲 | 2026-11-11 03:47:36 | 1608 | 2026-11-08 17:53:22 | mixed | 95.71% | 6414fbc775dbcdb18cee72cae72331e82ae7b571d5df41d4033ea9c9ea48ffc5 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | a5da6d04f2a3eb60f27d39e8dc0434f9858de758fb54c7dee06f8c399fb44047 | fd,jc | 意甲 | 2026-11-11 03:47:36 | 2006 | 2026-11-09 00:36:53 | mixed | 95.71% | 6414fbc775dbcdb18cee72cae72331e82ae7b571d5df41d4033ea9c9ea48ffc5 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | d12c988461af49b5ac2e6bec918e01aa448733f90debc525a6e5af310a089ea1 | fd,jc | 西甲 | 2026-11-11 03:47:36 | 2013 | 2026-11-08 17:53:22 | mixed | 95.93% | 6414fbc775dbcdb18cee72cae72331e82ae7b571d5df41d4033ea9c9ea48ffc5 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | da9b40e40481655b95f39ac250f331fd5eed1ecc5a673b4537ed815f37273fb7 | fd,jc | 英超 | 2026-11-11 03:47:36 | 1988 | 2026-11-08 17:53:22 | mixed | 96.58% | 6414fbc775dbcdb18cee72cae72331e82ae7b571d5df41d4033ea9c9ea48ffc5 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | e80291979347d4c2b4e18af452ccd7059c0a47d0ad6cf461096121b30ffc8c99 | fd,jc | 法甲 | 2026-11-11 03:47:36 | 1760 | 2026-11-08 17:53:22 | mixed | 97.78% | 6414fbc775dbcdb18cee72cae72331e82ae7b571d5df41d4033ea9c9ea48ffc5 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 173e16b4f8d08a463bc02c533e785c365a47df0ecee93f4c2a858d88468a2b37 | fd,jc | 荷甲 | 2026-11-11 03:47:36 | 1629 | 2026-11-08 17:52:48 | mixed | 97.97% | 6414fbc775dbcdb18cee72cae72331e82ae7b571d5df41d4033ea9c9ea48ffc5 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | d6c27ee4bd9043a3e8ed47f32b324567dc579e002b4493297699d422eb1b601b | fd,jc | 葡超 | 2026-11-11 03:47:36 | 1616 | 2026-11-08 17:53:22 | mixed | 98.33% | 6414fbc775dbcdb18cee72cae72331e82ae7b571d5df41d4033ea9c9ea48ffc5 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | cb4e7339ab633bd0db749323123a29000139469fcc9e261d0f1bf2fd7dfa90b4 | fd,jc | 英冠 | 2026-11-11 03:47:36 | 2920 | 2026-11-08 17:52:48 | mixed | 98.77% | 6414fbc775dbcdb18cee72cae72331e82ae7b571d5df41d4033ea9c9ea48ffc5 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 9e4736f00910fad8337f070aeae8348d131c9333c7593497b5b1be0e743e3997 | fd,jc | 瑞超 | 2026-11-11 03:47:36 | 1409 | 2026-11-08 17:53:22 | mixed | 98.37% | 6414fbc775dbcdb18cee72cae72331e82ae7b571d5df41d4033ea9c9ea48ffc5 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 2c2332b6a497f772a8f37f3044720a8a56fcf6e8cb875cfa8392481427ed16f7 | fd,jc | 挪超 | 2026-11-11 03:47:36 | 1391 | 2026-11-02 22:57:58 | mixed | 98.56% | 6414fbc775dbcdb18cee72cae72331e82ae7b571d5df41d4033ea9c9ea48ffc5 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | b9d331e8d881deabc9f40018e36a4e2a3e3a630e825ba740649b67e66d38c16a | fd,jc | 法乙 | 2026-11-11 03:47:36 | 1841 | 2026-11-07 17:26:10 | mixed | 99.57% | 6414fbc775dbcdb18cee72cae72331e82ae7b571d5df41d4033ea9c9ea48ffc5 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 64a5d2f345f8e757044fd7a0da86542c299b957aa13b887cb8459a2276d8e6c2 | fd,jc | 日职 | 2026-11-11 03:47:36 | 1876 | 2026-11-08 17:53:22 | mixed | 98.08% | 6414fbc775dbcdb18cee72cae72331e82ae7b571d5df41d4033ea9c9ea48ffc5 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | f4ab9ec13e4b42fb224de5672dee61892bfdb1370e8cdfb2ff20bfc8300eaeb0 | fd,jc | 芬超 | 2026-11-11 03:47:36 | 1015 | 2026-11-03 18:07:17 | mixed | 98.82% | 6414fbc775dbcdb18cee72cae72331e82ae7b571d5df41d4033ea9c9ea48ffc5 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | aaf6b129ae67503db8c096c333b25fabd902180e925782e524001bbc61dd6db5 | fd,jc | 巴甲 | 2026-11-11 03:47:36 | 2223 | 2026-11-07 17:26:10 | mixed | 98.56% | 6414fbc775dbcdb18cee72cae72331e82ae7b571d5df41d4033ea9c9ea48ffc5 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 2192986124b779ab8d5e3325eba7f912d05dbf2d6f302c8ce0f29db8d2545598 | fd,jc | 美职 | 2026-11-11 03:47:36 | 2969 | 2026-11-08 17:53:22 | mixed | 99.33% | 6414fbc775dbcdb18cee72cae72331e82ae7b571d5df41d4033ea9c9ea48ffc5 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | fec49d34945e94925aa416ae9442fd38d45e79d1a324319fc528d55090f8f647 | apif,fd,fdorg,jc | 欧战 | 2026-11-11 03:47:36 | 20580 | 2026-11-09 18:19:18 | mixed | 97.03% | 6414fbc775dbcdb18cee72cae72331e82ae7b571d5df41d4033ea9c9ea48ffc5 | 385035b903b4aa575365a8635f187e7d18bd49b5c24ab46cdfdfd5ed386ea1dd | 评估库 ≠ 训练库 |
| dixon-coles | 372e7ee95c8fcd280e8b2d0ad280d7cedc294eb8ea0b0d72279152d44f761663 | apif,jc | 欧国联 | 2026-11-11 03:47:36 | 766 | 2026-11-01 21:08:02 | mixed | 93.21% | 6414fbc775dbcdb18cee72cae72331e82ae7b571d5df41d4033ea9c9ea48ffc5 | — | — |

来源列为显式选择的训练语料范围；实际符合截点的训练来源及场数见 forecast.json 的 training_source_counts（null 表示旧模型未记录）。

## 纯模型与市场对照

| 比赛 ID / 编号 | 联赛 / 对阵 | 开球（北京时间） | 状态 / 排除原因 | 模型主胜 | 模型平局 | 模型客胜 | 市场主胜 | 市场平局 | 市场客胜 | SP 主胜 | SP 平局 | SP 客胜 | EV 主胜 | EV 平局 | EV 客胜 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |

## 比分前五与快照

