# P2 预测报告

> **非购买建议 · BET 禁用 · 各联赛P2历史评估门槛见逐场状态**

常规 90 分钟含补时。纯模型仅使用球队历史比分；市场 SP 和去水概率单独对照。
EV 为模型概率 × SP − 1 的点估计，EV > 0.05 仅加粗显示，不构成 BET 或购买建议。

批次：45249092891-1-2026-11-27；模式：prematch。
生成时间（北京时间）：2026-11-24 03:49:47；数据截止（北京时间）：2026-11-24 03:49:32。

## 模型与训练范围

版本为模型文件 SHA256；所有展示时间均为北京时间。assumed 占比表示使用假定赛果可知时间的训练场次占比，不代表已核实发布时间。

| 模型 | 版本 SHA256 | 来源 | 联赛 | 模型截点 | 训练场数 | 最近赛果可知时间 | 可知依据 | assumed 占比 | 训练库 SHA256 | 评估库 SHA256 | 数据库说明 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dixon-coles | d9f4f339ac73ef8956b026994c4d95b64557f2106d249251e31100ff1db95ebd | fd,jc | 德甲 | 2026-11-24 03:34:47 | 1619 | 2026-11-23 20:08:39 | mixed | 95.06% | 7ab85dadf442049d2f25a8e35a673d6f3364034c56ee86df9d5a4164089902ae | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 65a6ccb2d33bd3f0a5693b03676ddf5fc890ad847411e25f2a3ce8fdb42e4a7e | fd,jc | 意甲 | 2026-11-24 03:34:47 | 2017 | 2026-11-23 20:08:39 | mixed | 95.19% | 7ab85dadf442049d2f25a8e35a673d6f3364034c56ee86df9d5a4164089902ae | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 9443768213524c94cb592c35a1cfadf83e0ec7734a67d8e454cc57a2223a28aa | fd,jc | 西甲 | 2026-11-24 03:34:47 | 2024 | 2026-11-23 20:08:39 | mixed | 95.45% | 7ab85dadf442049d2f25a8e35a673d6f3364034c56ee86df9d5a4164089902ae | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | d630fb9af240723dacf27a6074f4858ca644cbabe0b01323b16366081bda641b | fd,jc | 英超 | 2026-11-24 03:34:47 | 2008 | 2026-11-23 20:08:39 | mixed | 96.02% | 7ab85dadf442049d2f25a8e35a673d6f3364034c56ee86df9d5a4164089902ae | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | fb37621dfb1c6d815034b3058d59612e74c5a0d6afac632fa235e7322b555014 | fd,jc | 法甲 | 2026-11-24 03:34:47 | 1771 | 2026-11-23 20:08:39 | mixed | 97.52% | 7ab85dadf442049d2f25a8e35a673d6f3364034c56ee86df9d5a4164089902ae | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 117607784f5c30bf5251d9f129ae3501b3f0c2fde227747b9d1291116cf3f58d | apif,fd,fdorg,jc | 荷甲 | 2026-11-24 03:34:47 | 1646 | 2026-11-23 20:08:21 | mixed | 97.39% | 7ab85dadf442049d2f25a8e35a673d6f3364034c56ee86df9d5a4164089902ae | — | — |
| dixon-coles | b145dbfc0674c9ae304f538ede1d5a98d415598e236c95815291fbdc5fca440d | fd,jc | 葡超 | 2026-11-24 03:34:47 | 1628 | 2026-11-11 04:14:29 | mixed | 98.28% | 7ab85dadf442049d2f25a8e35a673d6f3364034c56ee86df9d5a4164089902ae | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | fe2bc6e1bf9b81700858cea5d50e40068a03feb8cabdfc301bb628d6adfa0990 | apif,fd,fdorg,jc | 英冠 | 2026-11-24 03:34:47 | 2952 | 2026-11-23 19:41:19 | mixed | 98.37% | 7ab85dadf442049d2f25a8e35a673d6f3364034c56ee86df9d5a4164089902ae | — | — |
| dixon-coles | 03d787a17739b8d8f4576e45a895f848d3891417c795f6d2fcb7326d8d6a9228 | apif,fd,jc | 瑞超 | 2026-11-24 03:34:47 | 1815 | 2026-11-23 01:22:24 | mixed | 98.40% | 7ab85dadf442049d2f25a8e35a673d6f3364034c56ee86df9d5a4164089902ae | — | — |
| dixon-coles | f2de8f7d1d9eaf0d97661253c75b5b2f6e6f75d034fad91a474ead6a34bff373 | apif,fd,jc | 挪超 | 2026-11-24 03:34:47 | 1835 | 2026-11-23 20:08:39 | mixed | 98.53% | 7ab85dadf442049d2f25a8e35a673d6f3364034c56ee86df9d5a4164089902ae | — | — |
| dixon-coles | 43cad3ecd3060d3bda6d58cbb3587d7d4b028f3422241f53aa27c5b3bacffa1a | apif,fd,jc | 法乙 | 2026-11-24 03:34:47 | 1853 | 2026-11-08 06:00:00 | mixed | 99.57% | 7ab85dadf442049d2f25a8e35a673d6f3364034c56ee86df9d5a4164089902ae | — | — |
| dixon-coles | 359836e386c4f986a850a3da6d81dcd4580c4d4bcea92743555c88b6b2712a75 | apif,fd,jc | 日职 | 2026-11-24 03:34:47 | 3006 | 2026-11-21 18:34:37 | mixed | 98.47% | 7ab85dadf442049d2f25a8e35a673d6f3364034c56ee86df9d5a4164089902ae | — | — |
| dixon-coles | b6c4677eefd4f9734434f5890be24f9138b6c1eb930c6996f27115a5c3d13767 | apif,fd,jc | 芬超 | 2026-11-24 03:34:47 | 1172 | 2026-11-14 16:18:30 | mixed | 98.89% | 7ab85dadf442049d2f25a8e35a673d6f3364034c56ee86df9d5a4164089902ae | — | — |
| dixon-coles | 63e1d9b5b6c3fbedf8c926ec80fd6e9a9f5f184ae04f6618e592979ca9dc4542 | fd,jc | 巴甲 | 2026-11-24 03:34:47 | 2246 | 2026-11-20 18:55:42 | mixed | 98.22% | 7ab85dadf442049d2f25a8e35a673d6f3364034c56ee86df9d5a4164089902ae | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 6ba469842cf5e87014b5a92f929838e4202ac5c2938f00036d8dabd9b9f010f9 | apif,fd,jc | 美职 | 2026-11-24 03:34:47 | 4776 | 2026-11-23 19:41:35 | mixed | 99.43% | 7ab85dadf442049d2f25a8e35a673d6f3364034c56ee86df9d5a4164089902ae | — | — |
| dixon-coles | ea486e8a4c0d2b124d2f434d841abce7245d8d1120ab5e895d45b765828ea084 | apif,fd,fdorg,jc | 欧战 | 2026-11-24 03:34:47 | 28111 | 2026-11-23 20:08:39 | mixed | 97.63% | 7ab85dadf442049d2f25a8e35a673d6f3364034c56ee86df9d5a4164089902ae | 385035b903b4aa575365a8635f187e7d18bd49b5c24ab46cdfdfd5ed386ea1dd | 评估库 ≠ 训练库 |
| dixon-coles | 61a17367fcc8ca4ae913a5d5dbfe9ab956d1498378f879f774163b1460ddb5f7 | apif,jc | 欧国联 | 2026-11-24 03:34:47 | 818 | 2026-11-18 11:18:59 | mixed | 87.29% | 7ab85dadf442049d2f25a8e35a673d6f3364034c56ee86df9d5a4164089902ae | — | — |
| dixon-coles | 5d2c077a1cd65eda180c86627f13ebe8fd332c99f00b44a950e0a0dcf114401a | apif,jc | 韩职 | 2026-11-24 03:34:47 | 2237 | 2026-11-22 18:31:23 | mixed | 98.48% | 7ab85dadf442049d2f25a8e35a673d6f3364034c56ee86df9d5a4164089902ae | 1f5a99bfad007629272d2272fc0dfb10d4a11b9e0dcad775bb22f7dbc4d5f44f | 评估库 ≠ 训练库 |
| dixon-coles | 87a4aa3a78aefad9c803835d7611d047f41b1a0a8e08e80c066cbce6d67f908c | apif,jc | 澳超 | 2026-11-24 03:34:47 | 2288 | 2026-11-22 18:17:23 | mixed | 99.04% | 7ab85dadf442049d2f25a8e35a673d6f3364034c56ee86df9d5a4164089902ae | 1f5a99bfad007629272d2272fc0dfb10d4a11b9e0dcad775bb22f7dbc4d5f44f | 评估库 ≠ 训练库 |
| dixon-coles | 28f13be12d65e862dd21d60c2d26cce1e2baa11d3b1fb87ac2427b43f143b654 | apif,jc | 沙职 | 2026-11-24 03:34:47 | 2587 | 2026-11-21 18:13:05 | mixed | 99.50% | 7ab85dadf442049d2f25a8e35a673d6f3364034c56ee86df9d5a4164089902ae | 1dd8b10efce1fc56fb85ddc988fae7e7def8c908d37df16e0dae9ff62417994f | 评估库 ≠ 训练库 |
| dixon-coles | 33eb80d23ec1368ea89ec0b4e7a9ccf64c52b7d8232d9732f9787cbe8fb6bd78 | apif,jc | 德乙 | 2026-11-24 03:34:47 | 4708 | 2026-11-23 01:22:23 | mixed | 99.64% | 7ab85dadf442049d2f25a8e35a673d6f3364034c56ee86df9d5a4164089902ae | 5f2eb263016233afd55c8d2b880ec21753881e49916b0ae3b4645751c5f325c1 | 评估库 ≠ 训练库 |
| dixon-coles | cd3ff7ea78e821cd1e6f09a851393e42634ea86452870cd53da7dd2be5e45920 | apif,jc | 荷乙 | 2026-11-24 03:34:47 | 3879 | 2026-11-22 18:17:29 | mixed | 99.30% | 7ab85dadf442049d2f25a8e35a673d6f3364034c56ee86df9d5a4164089902ae | e1da97a554d7ef2ba40fe611a7c1c5eb962b8efba2117709bbdb6e9e96385710 | 评估库 ≠ 训练库 |

来源列为显式选择的训练语料范围；实际符合截点的训练来源及场数见 forecast.json 的 training_source_counts（null 表示旧模型未记录）。

## 纯模型与市场对照

| 比赛 ID / 编号 | 联赛 / 对阵 | 开球（北京时间） | 状态 / 排除原因 | 模型主胜 | 模型平局 | 模型客胜 | 市场主胜 | 市场平局 | 市场客胜 | SP 主胜 | SP 平局 | SP 客胜 | EV 主胜 | EV 平局 | EV 客胜 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| apif:1636301 / — | 欧罗巴 / 奈梅亨 vs 雷恩 | 2026-11-27 04:00:00 | 排除：缺少同来源同联赛的模型 | — | — | — | — | — | — | — | — | — | — | — | — |

## 比分前五与快照

