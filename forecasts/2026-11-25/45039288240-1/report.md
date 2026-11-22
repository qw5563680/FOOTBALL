# P2 预测报告

> **非购买建议 · BET 禁用 · 各联赛P2历史评估门槛见逐场状态**

常规 90 分钟含补时。纯模型仅使用球队历史比分；市场 SP 和去水概率单独对照。
EV 为模型概率 × SP − 1 的点估计，EV > 0.05 仅加粗显示，不构成 BET 或购买建议。

批次：45039288240-1-2026-11-25；模式：prematch。
生成时间（北京时间）：2026-11-22 23:57:32；数据截止（北京时间）：2026-11-22 23:57:15。

## 模型与训练范围

版本为模型文件 SHA256；所有展示时间均为北京时间。assumed 占比表示使用假定赛果可知时间的训练场次占比，不代表已核实发布时间。

| 模型 | 版本 SHA256 | 来源 | 联赛 | 模型截点 | 训练场数 | 最近赛果可知时间 | 可知依据 | assumed 占比 | 训练库 SHA256 | 评估库 SHA256 | 数据库说明 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dixon-coles | 2cbd8944759bb45da887fb6daaf37fa72109089733186e8d504a35bcf4c2e701 | fd,jc | 德甲 | 2026-11-22 23:47:26 | 1617 | 2026-11-22 18:31:23 | mixed | 95.18% | f3e2a7601a77e6ff4352013decd4dae19fc3db00870be89daa1e1d554039f312 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 20e2e1a844274c440bef63f5d1486f9589e752a98517c9fe1c15688163e81f95 | fd,jc | 意甲 | 2026-11-22 23:47:26 | 2013 | 2026-11-22 23:41:52 | mixed | 95.38% | f3e2a7601a77e6ff4352013decd4dae19fc3db00870be89daa1e1d554039f312 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 3b790c82170790366e0b5f67dd8057f166d9b9424a0accc847c5a4c87f9b5ea0 | fd,jc | 西甲 | 2026-11-22 23:47:26 | 2021 | 2026-11-22 18:31:23 | mixed | 95.60% | f3e2a7601a77e6ff4352013decd4dae19fc3db00870be89daa1e1d554039f312 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 8106499d8e2b6b7e032efda85925771fa2914e107abf818cb05eaa0ee8a5cdf4 | fd,jc | 英超 | 2026-11-22 23:47:26 | 1999 | 2026-11-22 18:31:23 | mixed | 96.05% | f3e2a7601a77e6ff4352013decd4dae19fc3db00870be89daa1e1d554039f312 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | e4bfe95da83bad491c3d12c65674ddefd29edf2b238666314da71b9dc37a3068 | fd,jc | 法甲 | 2026-11-22 23:47:26 | 1770 | 2026-11-22 18:31:23 | mixed | 97.57% | f3e2a7601a77e6ff4352013decd4dae19fc3db00870be89daa1e1d554039f312 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 7dc85c7a6ac9bd5b258fb62cc575e5921a383f5b3905e596d383fe7242057f80 | apif,fd,fdorg,jc | 荷甲 | 2026-11-22 23:47:26 | 1644 | 2026-11-22 23:41:52 | mixed | 97.51% | f3e2a7601a77e6ff4352013decd4dae19fc3db00870be89daa1e1d554039f312 | — | — |
| dixon-coles | d0a72f35ba709ed8da3dc273e18a27b41c56f0a6f393a610759b6868c8f6ecf3 | fd,jc | 葡超 | 2026-11-22 23:47:26 | 1622 | 2026-11-11 04:14:29 | mixed | 98.27% | f3e2a7601a77e6ff4352013decd4dae19fc3db00870be89daa1e1d554039f312 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 3a61a46babdcc187147bbcb5c65ce3af08f3022f474209b562239607b11c038b | apif,fd,fdorg,jc | 英冠 | 2026-11-22 23:47:26 | 2951 | 2026-11-22 23:41:52 | mixed | 98.41% | f3e2a7601a77e6ff4352013decd4dae19fc3db00870be89daa1e1d554039f312 | — | — |
| dixon-coles | 7686c901fc826d4db8b8f42f99a9f2bb4aeb738cfdea942101e2410eca86b806 | apif,fd,jc | 瑞超 | 2026-11-22 23:47:26 | 1801 | 2026-11-22 18:17:29 | mixed | 98.50% | f3e2a7601a77e6ff4352013decd4dae19fc3db00870be89daa1e1d554039f312 | — | — |
| dixon-coles | d7fec0a7722d654daec1a491e547fbae5b5dcd889bd1d5a1eb32e0e19bea7280 | apif,fd,jc | 挪超 | 2026-11-22 23:47:26 | 1817 | 2026-11-22 00:56:31 | mixed | 98.68% | f3e2a7601a77e6ff4352013decd4dae19fc3db00870be89daa1e1d554039f312 | — | — |
| dixon-coles | 602999d7bfd8a6e40a5b3b30acccf1d01fca376d69818f262481461f2787499d | apif,fd,jc | 法乙 | 2026-11-22 23:47:26 | 1845 | 2026-11-08 06:00:00 | mixed | 99.57% | f3e2a7601a77e6ff4352013decd4dae19fc3db00870be89daa1e1d554039f312 | — | — |
| dixon-coles | 64e2c47fb26cf6c8b254369d26932d26b8bb9ca94738d738e1f4a04e1bf78c88 | apif,fd,jc | 日职 | 2026-11-22 23:47:26 | 2996 | 2026-11-21 18:34:37 | mixed | 98.46% | f3e2a7601a77e6ff4352013decd4dae19fc3db00870be89daa1e1d554039f312 | — | — |
| dixon-coles | 8dcf67d417442abca1f34568950c12c943da827eb9acbe970c31018f9625d545 | apif,fd,jc | 芬超 | 2026-11-22 23:47:26 | 1167 | 2026-11-14 16:18:30 | mixed | 98.89% | f3e2a7601a77e6ff4352013decd4dae19fc3db00870be89daa1e1d554039f312 | — | — |
| dixon-coles | b1e30bb5918c8c71bca3a967b45e59df825f2178ab61da9f6c767469142981ba | fd,jc | 巴甲 | 2026-11-22 23:47:26 | 2238 | 2026-11-20 18:55:42 | mixed | 98.21% | f3e2a7601a77e6ff4352013decd4dae19fc3db00870be89daa1e1d554039f312 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | b6b51e3346092bd92da12c7bd7e3549a796ce0b5681a49c2a0a4d7585cf13555 | apif,fd,jc | 美职 | 2026-11-22 23:47:26 | 4738 | 2026-11-22 18:17:29 | mixed | 99.43% | f3e2a7601a77e6ff4352013decd4dae19fc3db00870be89daa1e1d554039f312 | — | — |
| dixon-coles | 6930b374df41b6e2d986dee619491328c6bdca56a97ecc2f3caa736763afc1e0 | apif,fd,fdorg,jc | 欧战 | 2026-11-22 23:47:26 | 28043 | 2026-11-22 23:41:52 | mixed | 97.70% | f3e2a7601a77e6ff4352013decd4dae19fc3db00870be89daa1e1d554039f312 | 385035b903b4aa575365a8635f187e7d18bd49b5c24ab46cdfdfd5ed386ea1dd | 评估库 ≠ 训练库 |
| dixon-coles | 1f4e7e475de98b12ddd05de846b42e490c596b6e8c74886553a790eae840a0b8 | apif,jc | 欧国联 | 2026-11-22 23:47:26 | 818 | 2026-11-18 11:18:59 | mixed | 87.29% | f3e2a7601a77e6ff4352013decd4dae19fc3db00870be89daa1e1d554039f312 | — | — |
| dixon-coles | 752c5d989420bfeaa6efee64ad92a0fb48dc92b1e476b0a024b7ee0c36566d16 | apif,jc | 韩职 | 2026-11-22 23:47:26 | 2237 | 2026-11-22 18:31:23 | mixed | 98.48% | f3e2a7601a77e6ff4352013decd4dae19fc3db00870be89daa1e1d554039f312 | 1f5a99bfad007629272d2272fc0dfb10d4a11b9e0dcad775bb22f7dbc4d5f44f | 评估库 ≠ 训练库 |
| dixon-coles | 7337a8c3cb1184dfd43363f004ff6b950376608eca211e209d5724c75da753b8 | apif,jc | 澳超 | 2026-11-22 23:47:26 | 2288 | 2026-11-22 18:17:23 | mixed | 99.04% | f3e2a7601a77e6ff4352013decd4dae19fc3db00870be89daa1e1d554039f312 | 1f5a99bfad007629272d2272fc0dfb10d4a11b9e0dcad775bb22f7dbc4d5f44f | 评估库 ≠ 训练库 |
| dixon-coles | b7f45380970117222df4b729cb7cfbe687a33b70bbc111d8914eef3bc4ad5b7d | apif,jc | 沙职 | 2026-11-22 23:47:26 | 2587 | 2026-11-21 18:13:05 | mixed | 99.50% | f3e2a7601a77e6ff4352013decd4dae19fc3db00870be89daa1e1d554039f312 | 1dd8b10efce1fc56fb85ddc988fae7e7def8c908d37df16e0dae9ff62417994f | 评估库 ≠ 训练库 |
| dixon-coles | 3d4d63789e34ef13e76ab56eec444a69a166f209cfc9658823a995e9edfb6fc1 | apif,jc | 德乙 | 2026-11-22 23:47:26 | 4705 | 2026-11-22 18:17:29 | mixed | 99.70% | f3e2a7601a77e6ff4352013decd4dae19fc3db00870be89daa1e1d554039f312 | 5f2eb263016233afd55c8d2b880ec21753881e49916b0ae3b4645751c5f325c1 | 评估库 ≠ 训练库 |
| dixon-coles | de04e085894d48d1dc6160a0b57d19e06cc5105afdefc421c7a49cb075d60b4c | apif,jc | 荷乙 | 2026-11-22 23:47:26 | 3879 | 2026-11-22 18:17:29 | mixed | 99.30% | f3e2a7601a77e6ff4352013decd4dae19fc3db00870be89daa1e1d554039f312 | e1da97a554d7ef2ba40fe611a7c1c5eb962b8efba2117709bbdb6e9e96385710 | 评估库 ≠ 训练库 |

来源列为显式选择的训练语料范围；实际符合截点的训练来源及场数见 forecast.json 的 training_source_counts（null 表示旧模型未记录）。

## 纯模型与市场对照

| 比赛 ID / 编号 | 联赛 / 对阵 | 开球（北京时间） | 状态 / 排除原因 | 模型主胜 | 模型平局 | 模型客胜 | 市场主胜 | 市场平局 | 市场客胜 | SP 主胜 | SP 平局 | SP 客胜 | EV 主胜 | EV 平局 | EV 客胜 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| fdorg:561957 / — | 英冠 / 伍尔弗 vs 德比郡 | 2026-11-25 04:00:00 | 已预测 · 实验 · 仅历史证据 | 64.24% | 20.86% | 14.90% | — | — | — | — | — | — | — | — | — |

## 比分前五与快照

- fdorg:561957：2:0（11.28%）、1:0（10.77%）、1:1（9.92%）、2:1（9.87%）、3:0（7.69%）；市场快照（北京时间）：无完整赛前快照；模型：dixon-coles / 3a61a46babdcc187147bbcb5c65ce3af08f3022f474209b562239607b11c038b。

半全场（实验，纯推导量；不参与任何下注建议，BET 禁用）：

- 伍尔弗 vs 德比郡（fdorg:561957）：胜/胜 41.89%；胜/平 4.13%；胜/负 1.03%；平/胜 19.33%；平/平 12.60%；平/负 5.79%；负/胜 3.02%；负/平 4.13%；负/负 8.09%；半场进球占比 0.448664（联赛，2951 场）。
