# P2 预测报告

> **非购买建议 · BET 禁用 · 各联赛P2历史评估门槛见逐场状态**

常规 90 分钟含补时。纯模型仅使用球队历史比分；市场 SP 和去水概率单独对照。
EV 为模型概率 × SP − 1 的点估计，EV > 0.05 仅加粗显示，不构成 BET 或购买建议。

批次：46054646588-1-2026-11-30；模式：prematch。
生成时间（北京时间）：2026-11-28 03:40:48；数据截止（北京时间）：2026-11-28 03:40:33。

## 模型与训练范围

版本为模型文件 SHA256；所有展示时间均为北京时间。assumed 占比表示使用假定赛果可知时间的训练场次占比，不代表已核实发布时间。

| 模型 | 版本 SHA256 | 来源 | 联赛 | 模型截点 | 训练场数 | 最近赛果可知时间 | 可知依据 | assumed 占比 | 训练库 SHA256 | 评估库 SHA256 | 数据库说明 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dixon-coles | 3dbf3eb0a453ff70a84b88e59bd09fb4adb46a5f4387e438612b9d346531f57b | fd,jc | 德甲 | 2026-11-28 03:23:32 | 1619 | 2026-11-23 20:08:39 | mixed | 95.06% | ed53800cd96af1fcf10bc30ccd7c08410c80dd94d0e49e5e10bd8bd7e509eaf8 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | e9f3ab7a5e9a73a786791adab01c6dfa61b2375c4d547b318f525444fbc87ee4 | fd,jc | 意甲 | 2026-11-28 03:23:32 | 2018 | 2026-11-24 21:34:09 | mixed | 95.14% | ed53800cd96af1fcf10bc30ccd7c08410c80dd94d0e49e5e10bd8bd7e509eaf8 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 4b5127d5c851a8de7a0591ec16859d6daddddc7fbf915ac7ee6e7f92f8a945b0 | fd,jc | 西甲 | 2026-11-28 03:23:32 | 2025 | 2026-11-24 21:34:09 | mixed | 95.41% | ed53800cd96af1fcf10bc30ccd7c08410c80dd94d0e49e5e10bd8bd7e509eaf8 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | b57beddc1e5f25338f4920f81c8b897d2eb1de51646eff4dd3a1cc902eafbf68 | fd,jc | 英超 | 2026-11-28 03:23:32 | 2009 | 2026-11-24 21:34:09 | mixed | 95.97% | ed53800cd96af1fcf10bc30ccd7c08410c80dd94d0e49e5e10bd8bd7e509eaf8 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 5d4edd3021aad16a0558d3ef0be1ee2013f92b92974f9430310e6c52bba50728 | fd,jc | 法甲 | 2026-11-28 03:23:32 | 1771 | 2026-11-23 20:08:39 | mixed | 97.52% | ed53800cd96af1fcf10bc30ccd7c08410c80dd94d0e49e5e10bd8bd7e509eaf8 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 7203affca81a403fbff2498899f61199eae19cd4c01132423e7c4531a450f3b4 | apif,fd,fdorg,jc | 荷甲 | 2026-11-28 03:23:32 | 1646 | 2026-11-23 20:08:21 | mixed | 97.39% | ed53800cd96af1fcf10bc30ccd7c08410c80dd94d0e49e5e10bd8bd7e509eaf8 | — | — |
| dixon-coles | baa40488e92bd7fdf0cb05fd9fed8abf361c5a3ec53a00dadb30ec23800aa0ad | fd,jc | 葡超 | 2026-11-28 03:23:32 | 1628 | 2026-11-11 04:14:29 | mixed | 98.28% | ed53800cd96af1fcf10bc30ccd7c08410c80dd94d0e49e5e10bd8bd7e509eaf8 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 4b0aff1c4167ac0abf02b63c27b3ed0a3a4d95f0352995cb3b6ede26c945fdd5 | apif,fd,fdorg,jc | 英冠 | 2026-11-28 03:23:32 | 2964 | 2026-11-26 11:12:53 | mixed | 97.98% | ed53800cd96af1fcf10bc30ccd7c08410c80dd94d0e49e5e10bd8bd7e509eaf8 | — | — |
| dixon-coles | 558a02be463560ab28ad04a6e0a9a354131da6bab26c436c4e4b60af984f29bd | apif,fd,jc | 瑞超 | 2026-11-28 03:23:32 | 1817 | 2026-11-24 21:34:09 | mixed | 98.29% | ed53800cd96af1fcf10bc30ccd7c08410c80dd94d0e49e5e10bd8bd7e509eaf8 | — | — |
| dixon-coles | 0513f240ba5c48807343ec10d6b1978adcb1b7b914c18540b69f0f4784138a35 | apif,fd,jc | 挪超 | 2026-11-28 03:23:32 | 1835 | 2026-11-23 20:08:39 | mixed | 98.53% | ed53800cd96af1fcf10bc30ccd7c08410c80dd94d0e49e5e10bd8bd7e509eaf8 | — | — |
| dixon-coles | a4aef0ac8c9f5e2349db6628d7bbb116150bd45114dbaea3f4cc4440951fb5ae | apif,fd,jc | 法乙 | 2026-11-28 03:23:32 | 1854 | 2026-11-24 21:34:09 | mixed | 99.51% | ed53800cd96af1fcf10bc30ccd7c08410c80dd94d0e49e5e10bd8bd7e509eaf8 | — | — |
| dixon-coles | 7a7fd8e8a4be0a0a9c7da138b10e9d2845f088ad7068d9ce45eefa8fbedd11f1 | apif,fd,jc | 日职 | 2026-11-28 03:23:32 | 3012 | 2026-11-26 03:34:44 | mixed | 98.27% | ed53800cd96af1fcf10bc30ccd7c08410c80dd94d0e49e5e10bd8bd7e509eaf8 | — | — |
| dixon-coles | 848551fdf6977245a2cfa836f0f3f59df448e60e6d81ce9b6809fd072901650c | apif,fd,jc | 芬超 | 2026-11-28 03:23:32 | 1172 | 2026-11-14 16:18:30 | mixed | 98.89% | ed53800cd96af1fcf10bc30ccd7c08410c80dd94d0e49e5e10bd8bd7e509eaf8 | — | — |
| dixon-coles | eff1f753dc9758e029b64de449d6be96425d69b661b1c786696526848552293f | fd,jc | 巴甲 | 2026-11-28 03:23:32 | 2247 | 2026-11-24 21:34:09 | mixed | 98.18% | ed53800cd96af1fcf10bc30ccd7c08410c80dd94d0e49e5e10bd8bd7e509eaf8 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | b3608798f89dd46aedf7cd54cb560061628aaf29dafb650a81777b74407cb734 | apif,fd,jc | 美职 | 2026-11-28 03:23:32 | 4777 | 2026-11-24 21:34:09 | mixed | 99.41% | ed53800cd96af1fcf10bc30ccd7c08410c80dd94d0e49e5e10bd8bd7e509eaf8 | — | — |
| dixon-coles | df03081edac5a7e3599e564f625eed880c37e0d45ab237aa6a0ef95a3d3ddaba | apif,fd,fdorg,jc | 欧战 | 2026-11-28 03:23:32 | 28144 | 2026-11-27 11:27:26 | mixed | 97.51% | ed53800cd96af1fcf10bc30ccd7c08410c80dd94d0e49e5e10bd8bd7e509eaf8 | 385035b903b4aa575365a8635f187e7d18bd49b5c24ab46cdfdfd5ed386ea1dd | 评估库 ≠ 训练库 |
| dixon-coles | b207f140a60a82538c999465145bf14e76728e8dd815690bd00f797002616028 | apif,jc | 欧国联 | 2026-11-28 03:23:32 | 818 | 2026-11-18 11:18:59 | mixed | 87.29% | ed53800cd96af1fcf10bc30ccd7c08410c80dd94d0e49e5e10bd8bd7e509eaf8 | — | — |
| dixon-coles | 83a76b141daa7bc53bcb0e93f998bd12699c2063ee28d68e3c52097f677aae98 | apif,jc | 韩职 | 2026-11-28 03:23:32 | 2237 | 2026-11-22 18:31:23 | mixed | 98.48% | ed53800cd96af1fcf10bc30ccd7c08410c80dd94d0e49e5e10bd8bd7e509eaf8 | 1f5a99bfad007629272d2272fc0dfb10d4a11b9e0dcad775bb22f7dbc4d5f44f | 评估库 ≠ 训练库 |
| dixon-coles | 0ed4afb6e795316c4e744b1daddb83f0eb413a9751158b66630f5c0b6ad15b38 | apif,jc | 澳超 | 2026-11-28 03:23:32 | 2289 | 2026-11-27 19:03:29 | mixed | 99.00% | ed53800cd96af1fcf10bc30ccd7c08410c80dd94d0e49e5e10bd8bd7e509eaf8 | 1f5a99bfad007629272d2272fc0dfb10d4a11b9e0dcad775bb22f7dbc4d5f44f | 评估库 ≠ 训练库 |
| dixon-coles | 4beacb446e9f7847acd631b0b2c509fcaaba8da48e82d0251b147b004af7444b | apif,jc | 沙职 | 2026-11-28 03:23:32 | 2589 | 2026-11-28 03:10:38 | mixed | 99.42% | ed53800cd96af1fcf10bc30ccd7c08410c80dd94d0e49e5e10bd8bd7e509eaf8 | 1dd8b10efce1fc56fb85ddc988fae7e7def8c908d37df16e0dae9ff62417994f | 评估库 ≠ 训练库 |
| dixon-coles | 6c28e0b4d4ddfbc0d5b64bda25b4b4e9362a90d6d5feb55365f6c6321837474b | apif,jc | 德乙 | 2026-11-28 03:23:32 | 4708 | 2026-11-23 01:22:23 | mixed | 99.64% | ed53800cd96af1fcf10bc30ccd7c08410c80dd94d0e49e5e10bd8bd7e509eaf8 | 5f2eb263016233afd55c8d2b880ec21753881e49916b0ae3b4645751c5f325c1 | 评估库 ≠ 训练库 |
| dixon-coles | 6a17b76dcb76a775c7a5c9d5024f0b04b7fed88ec14f0f771a26589b1871e2f5 | apif,jc | 荷乙 | 2026-11-28 03:23:32 | 3882 | 2026-11-24 05:40:20 | mixed | 99.23% | ed53800cd96af1fcf10bc30ccd7c08410c80dd94d0e49e5e10bd8bd7e509eaf8 | e1da97a554d7ef2ba40fe611a7c1c5eb962b8efba2117709bbdb6e9e96385710 | 评估库 ≠ 训练库 |

来源列为显式选择的训练语料范围；实际符合截点的训练来源及场数见 forecast.json 的 training_source_counts（null 表示旧模型未记录）。

## 纯模型与市场对照

| 比赛 ID / 编号 | 联赛 / 对阵 | 开球（北京时间） | 状态 / 排除原因 | 模型主胜 | 模型平局 | 模型客胜 | 市场主胜 | 市场平局 | 市场客胜 | SP 主胜 | SP 平局 | SP 客胜 | EV 主胜 | EV 平局 | EV 客胜 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| jc:2043126 / 周日017 | 英超 / 阿森纳 vs 曼城 | 2026-11-30 00:30:00 | 已预测 · 可用（旧规，考核中） · 仅历史证据 | 46.06% | 29.24% | 24.70% | 45.48% | 26.20% | 28.32% | 1.93 | 3.35 | 3.1 | -11.10% | -2.06% | -23.42% |
| jc:2043131 / 周日022 | 葡超 / 波尔图 vs 里斯本 | 2026-11-30 04:30:00 | 已预测 · 可用（旧规，考核中） · 仅历史证据 | 41.59% | 27.97% | 30.44% | 50.15% | 25.81% | 24.04% | 1.75 | 3.4 | 3.65 | -27.21% | -4.90% | **11.09%** |

## 比分前五与快照

- jc:2043126：1:1（13.89%）、1:0（11.23%）、0:0（9.57%）、2:1（8.97%）、2:0（8.85%）；市场快照（北京时间）：2026-11-27 21:35:19；模型：dixon-coles / b57beddc1e5f25338f4920f81c8b897d2eb1de51646eff4dd3a1cc902eafbf68。
- jc:2043131：1:1（13.26%）、1:0（10.99%）、0:1（9.19%）、2:1（8.46%）、0:0（7.73%）；市场快照（北京时间）：2026-11-27 21:35:19；模型：dixon-coles / baa40488e92bd7fdf0cb05fd9fed8abf361c5a3ec53a00dadb30ec23800aa0ad。

半全场（实验，纯推导量；不参与任何下注建议，BET 禁用）：

- 阿森纳 vs 曼城（jc:2043126）：胜/胜 27.62%；胜/平 5.25%；胜/负 1.45%；平/胜 16.12%；平/平 18.73%；平/负 9.53%；负/胜 2.31%；负/平 5.25%；负/负 13.73%；半场进球占比 0.443535（联赛，2009 场）。
- 波尔图 vs 里斯本（jc:2043131）：胜/胜 24.37%；胜/平 5.48%；胜/负 1.75%；平/胜 14.99%；平/平 17.01%；平/负 11.59%；负/胜 2.23%；负/平 5.48%；负/负 17.10%；半场进球占比 0.442486（联赛，1628 场）。

## 球队＋市场（实验）

- jc:2043126：45.48% / 26.20% / 28.32%，市场权重 100.00%。
- jc:2043131：50.15% / 25.81% / 24.04%，市场权重 100.00%。
