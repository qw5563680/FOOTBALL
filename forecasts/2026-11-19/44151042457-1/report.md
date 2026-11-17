# P2 预测报告

> **非购买建议 · BET 禁用 · 各联赛P2历史评估门槛见逐场状态**

常规 90 分钟含补时。纯模型仅使用球队历史比分；市场 SP 和去水概率单独对照。
EV 为模型概率 × SP − 1 的点估计，EV > 0.05 仅加粗显示，不构成 BET 或购买建议。

批次：44151042457-1-2026-11-19；模式：prematch。
生成时间（北京时间）：2026-11-18 03:19:53；数据截止（北京时间）：2026-11-18 03:19:51。

## 模型与训练范围

版本为模型文件 SHA256；所有展示时间均为北京时间。assumed 占比表示使用假定赛果可知时间的训练场次占比，不代表已核实发布时间。

| 模型 | 版本 SHA256 | 来源 | 联赛 | 模型截点 | 训练场数 | 最近赛果可知时间 | 可知依据 | assumed 占比 | 训练库 SHA256 | 评估库 SHA256 | 数据库说明 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dixon-coles | cc1dc67c721e4acc2fc57b102a5d92b073b5b03e7719f9cad192aff635935d92 | fd,jc | 德甲 | 2026-11-18 03:11:44 | 1611 | 2026-11-11 04:14:29 | mixed | 95.53% | 47c5710875633dfd8552e3b8aabdc86a7dd5dbc8e70c1b9fc27f7411e0616bf7 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 4bc480aba2e7da56d707bd0f41749fbcdef38cac2464b781a82ef3726e5bd871 | fd,jc | 意甲 | 2026-11-18 03:11:44 | 2010 | 2026-11-11 04:14:29 | mixed | 95.52% | 47c5710875633dfd8552e3b8aabdc86a7dd5dbc8e70c1b9fc27f7411e0616bf7 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 8fef907647504723497589652e513793c2e93ca2c762ddb8d3ea2841b3542b4a | fd,jc | 西甲 | 2026-11-18 03:11:44 | 2019 | 2026-11-11 04:14:29 | mixed | 95.69% | 47c5710875633dfd8552e3b8aabdc86a7dd5dbc8e70c1b9fc27f7411e0616bf7 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 93546e73141ec520a4a684fc1d888f9e5bec98e6f063a3fedf27573c6e6f1283 | fd,jc | 英超 | 2026-11-18 03:11:44 | 1992 | 2026-11-11 04:14:29 | mixed | 96.39% | 47c5710875633dfd8552e3b8aabdc86a7dd5dbc8e70c1b9fc27f7411e0616bf7 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | d99f94922bb67129c5e9d7bc1069106073ce4aed1a195f5e8838aafc1fa69290 | fd,jc | 法甲 | 2026-11-18 03:11:44 | 1768 | 2026-11-11 04:14:29 | mixed | 97.68% | 47c5710875633dfd8552e3b8aabdc86a7dd5dbc8e70c1b9fc27f7411e0616bf7 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 730136408c49aa37748b2471abb8a530c3a8c24031f0b5fd5483e0354eec4798 | apif,fd,fdorg,jc | 荷甲 | 2026-11-18 03:11:44 | 1638 | 2026-11-11 04:14:01 | mixed | 97.86% | 47c5710875633dfd8552e3b8aabdc86a7dd5dbc8e70c1b9fc27f7411e0616bf7 | — | — |
| dixon-coles | 8d1d21ce032ec8c196a52ebdb434a38a82e7da9c3efaec57b32fb6e3640279c5 | fd,jc | 葡超 | 2026-11-18 03:11:44 | 1622 | 2026-11-11 04:14:29 | mixed | 98.27% | 47c5710875633dfd8552e3b8aabdc86a7dd5dbc8e70c1b9fc27f7411e0616bf7 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 00bfaa7b7ce7222d9a4177a845435b0be987948ac1e036caf056603d7578b332 | apif,fd,fdorg,jc | 英冠 | 2026-11-18 03:11:44 | 2940 | 2026-11-08 17:52:48 | mixed | 98.78% | 47c5710875633dfd8552e3b8aabdc86a7dd5dbc8e70c1b9fc27f7411e0616bf7 | — | — |
| dixon-coles | 47dc46d3520e189275b8837fae989d5fbc204af6e21df4e0b081e76a5298e8ae | apif,fd,jc | 瑞超 | 2026-11-18 03:11:44 | 1798 | 2026-11-14 16:18:30 | mixed | 98.67% | 47c5710875633dfd8552e3b8aabdc86a7dd5dbc8e70c1b9fc27f7411e0616bf7 | — | — |
| dixon-coles | 8b64682588ba7f9b9a052408d004d7c691a265241782c1ced1f362be43340894 | apif,fd,jc | 挪超 | 2026-11-18 03:11:44 | 1814 | 2026-11-14 16:18:30 | mixed | 98.84% | 47c5710875633dfd8552e3b8aabdc86a7dd5dbc8e70c1b9fc27f7411e0616bf7 | — | — |
| dixon-coles | c340579622f31a663732838bddad93fb4d7fd6ad75fa529dae0e80c39950224e | apif,fd,jc | 法乙 | 2026-11-18 03:11:44 | 1845 | 2026-11-08 06:00:00 | mixed | 99.57% | 47c5710875633dfd8552e3b8aabdc86a7dd5dbc8e70c1b9fc27f7411e0616bf7 | — | — |
| dixon-coles | 7a44c1afaffaf18356962ece1e5fae0c99767af4a734f8a1e049c6b1852af2d3 | apif,fd,jc | 日职 | 2026-11-18 03:11:44 | 2986 | 2026-11-14 16:18:30 | mixed | 98.79% | 47c5710875633dfd8552e3b8aabdc86a7dd5dbc8e70c1b9fc27f7411e0616bf7 | — | — |
| dixon-coles | a6db3843afd0550d7f16829e71609f795b4e18f4bd77e8cd23944000c373949c | apif,fd,jc | 芬超 | 2026-11-18 03:11:44 | 1167 | 2026-11-14 16:18:30 | mixed | 98.89% | 47c5710875633dfd8552e3b8aabdc86a7dd5dbc8e70c1b9fc27f7411e0616bf7 | — | — |
| dixon-coles | 5a97943627860ca65f5a74e725cbe694d6188a6ecffaaef96ef9baecc5948930 | fd,jc | 巴甲 | 2026-11-18 03:11:44 | 2232 | 2026-11-15 11:11:24 | mixed | 98.48% | 47c5710875633dfd8552e3b8aabdc86a7dd5dbc8e70c1b9fc27f7411e0616bf7 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 634092cda6874fc3dc55988d9175775b24965f6700f03c01bd6aa46474bdb28f | apif,fd,jc | 美职 | 2026-11-18 03:11:44 | 4739 | 2026-11-14 16:18:30 | mixed | 99.54% | 47c5710875633dfd8552e3b8aabdc86a7dd5dbc8e70c1b9fc27f7411e0616bf7 | — | — |
| dixon-coles | bc08a13d90840733af40f41243d05e9b0ac140646d778eca72f663046cc39220 | apif,fd,fdorg,jc | 欧战 | 2026-11-18 03:11:44 | 27999 | 2026-11-11 04:14:29 | mixed | 97.85% | 47c5710875633dfd8552e3b8aabdc86a7dd5dbc8e70c1b9fc27f7411e0616bf7 | 385035b903b4aa575365a8635f187e7d18bd49b5c24ab46cdfdfd5ed386ea1dd | 评估库 ≠ 训练库 |
| dixon-coles | a4054e0804dda9b873b19ba77ca668051a9810185e1be3d2f53d954e475434cc | apif,jc | 欧国联 | 2026-11-18 03:11:44 | 810 | 2026-11-17 11:12:35 | mixed | 88.15% | 47c5710875633dfd8552e3b8aabdc86a7dd5dbc8e70c1b9fc27f7411e0616bf7 | — | — |
| dixon-coles | 704a7c93b595faccc425f7c2547a5d471ef6c2af5c026c42340207f6c3b01223 | apif,jc | 韩职 | 2026-11-18 03:11:44 | 2231 | 2026-11-09 00:36:53 | mixed | 98.74% | 47c5710875633dfd8552e3b8aabdc86a7dd5dbc8e70c1b9fc27f7411e0616bf7 | 1f5a99bfad007629272d2272fc0dfb10d4a11b9e0dcad775bb22f7dbc4d5f44f | 评估库 ≠ 训练库 |
| dixon-coles | 3a63602a9efc2b9586e95849bc48b7de648c4a50968d8bb72d882b3c3b2d8ee4 | apif,jc | 澳超 | 2026-11-18 03:11:44 | 2283 | 2026-11-08 17:53:22 | mixed | 99.26% | 47c5710875633dfd8552e3b8aabdc86a7dd5dbc8e70c1b9fc27f7411e0616bf7 | 1f5a99bfad007629272d2272fc0dfb10d4a11b9e0dcad775bb22f7dbc4d5f44f | 评估库 ≠ 训练库 |

来源列为显式选择的训练语料范围；实际符合截点的训练来源及场数见 forecast.json 的 training_source_counts（null 表示旧模型未记录）。

## 纯模型与市场对照

| 比赛 ID / 编号 | 联赛 / 对阵 | 开球（北京时间） | 状态 / 排除原因 | 模型主胜 | 模型平局 | 模型客胜 | 市场主胜 | 市场平局 | 市场客胜 | SP 主胜 | SP 平局 | SP 客胜 | EV 主胜 | EV 平局 | EV 客胜 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |

## 比分前五与快照

