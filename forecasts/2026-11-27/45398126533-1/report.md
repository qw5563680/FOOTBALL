# P2 预测报告

> **非购买建议 · BET 禁用 · 各联赛P2历史评估门槛见逐场状态**

常规 90 分钟含补时。纯模型仅使用球队历史比分；市场 SP 和去水概率单独对照。
EV 为模型概率 × SP − 1 的点估计，EV > 0.05 仅加粗显示，不构成 BET 或购买建议。

批次：45398126533-1-2026-11-27；模式：prematch。
生成时间（北京时间）：2026-11-24 21:48:09；数据截止（北京时间）：2026-11-24 21:47:55。

## 模型与训练范围

版本为模型文件 SHA256；所有展示时间均为北京时间。assumed 占比表示使用假定赛果可知时间的训练场次占比，不代表已核实发布时间。

| 模型 | 版本 SHA256 | 来源 | 联赛 | 模型截点 | 训练场数 | 最近赛果可知时间 | 可知依据 | assumed 占比 | 训练库 SHA256 | 评估库 SHA256 | 数据库说明 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dixon-coles | d8cb7ad9ee4426bbf326e6c69af80b190adf8f00ca7591682297a1cfe2e6f0b9 | fd,jc | 德甲 | 2026-11-24 21:39:07 | 1619 | 2026-11-23 20:08:39 | mixed | 95.06% | 24971ea9e07f49fb6bc9a2dac6ac8be0baa2af842475d6a6d5246ffdbc1c928b | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | a21688a601a24d9c4330ff2c2bceadc3f61c813089e43d1b6ef0f361daf1d3c1 | fd,jc | 意甲 | 2026-11-24 21:39:07 | 2018 | 2026-11-24 21:34:09 | mixed | 95.14% | 24971ea9e07f49fb6bc9a2dac6ac8be0baa2af842475d6a6d5246ffdbc1c928b | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | e29a8df81b7d6da6954c5222f54d8f4bbc41adc3aa2aab17eaf6cfcaf0807b40 | fd,jc | 西甲 | 2026-11-24 21:39:07 | 2025 | 2026-11-24 21:34:09 | mixed | 95.41% | 24971ea9e07f49fb6bc9a2dac6ac8be0baa2af842475d6a6d5246ffdbc1c928b | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | b0420cac1a9ae87275bafdc4be326ae9c222eff8bd71f9ceb220f6e6f202a777 | fd,jc | 英超 | 2026-11-24 21:39:07 | 2009 | 2026-11-24 21:34:09 | mixed | 95.97% | 24971ea9e07f49fb6bc9a2dac6ac8be0baa2af842475d6a6d5246ffdbc1c928b | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | d1aa17f6cb54b8f9c653dc51febf36589348f043bfad05c925f69431143ba2f4 | fd,jc | 法甲 | 2026-11-24 21:39:07 | 1771 | 2026-11-23 20:08:39 | mixed | 97.52% | 24971ea9e07f49fb6bc9a2dac6ac8be0baa2af842475d6a6d5246ffdbc1c928b | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 1b1189df30f3b43b9165066176e6581c6109f11856266b6699d3c68215449c2f | apif,fd,fdorg,jc | 荷甲 | 2026-11-24 21:39:07 | 1646 | 2026-11-23 20:08:21 | mixed | 97.39% | 24971ea9e07f49fb6bc9a2dac6ac8be0baa2af842475d6a6d5246ffdbc1c928b | — | — |
| dixon-coles | dad8fb9fcb740cb5b7cb0c2a46deff387162520a99998e4dd9b32c25ffee4b22 | fd,jc | 葡超 | 2026-11-24 21:39:07 | 1628 | 2026-11-11 04:14:29 | mixed | 98.28% | 24971ea9e07f49fb6bc9a2dac6ac8be0baa2af842475d6a6d5246ffdbc1c928b | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 883e8005c1882a57f988b4fccc47b4eacc6d0f41f144a7e7f24f2e95932efb04 | apif,fd,fdorg,jc | 英冠 | 2026-11-24 21:39:07 | 2952 | 2026-11-23 19:41:19 | mixed | 98.37% | 24971ea9e07f49fb6bc9a2dac6ac8be0baa2af842475d6a6d5246ffdbc1c928b | — | — |
| dixon-coles | f2517ffbb7b5459b29c5288dd779a42986aa53e133b726818c95ed5e4c68a12f | apif,fd,jc | 瑞超 | 2026-11-24 21:39:07 | 1817 | 2026-11-24 21:34:09 | mixed | 98.29% | 24971ea9e07f49fb6bc9a2dac6ac8be0baa2af842475d6a6d5246ffdbc1c928b | — | — |
| dixon-coles | d4d9c06d8e113842fd90f6755f1daf33822153802029b56652ded712c3817823 | apif,fd,jc | 挪超 | 2026-11-24 21:39:07 | 1835 | 2026-11-23 20:08:39 | mixed | 98.53% | 24971ea9e07f49fb6bc9a2dac6ac8be0baa2af842475d6a6d5246ffdbc1c928b | — | — |
| dixon-coles | 41257205a4808e0d857021812b418db70420374cc7a1f4cb340a20f880c5a220 | apif,fd,jc | 法乙 | 2026-11-24 21:39:07 | 1854 | 2026-11-24 21:34:09 | mixed | 99.51% | 24971ea9e07f49fb6bc9a2dac6ac8be0baa2af842475d6a6d5246ffdbc1c928b | — | — |
| dixon-coles | 6c1e4a3d57cf715bee7bd2e145af3ce116699ff02d5b76334eac39f6d97718dd | apif,fd,jc | 日职 | 2026-11-24 21:39:07 | 3006 | 2026-11-21 18:34:37 | mixed | 98.47% | 24971ea9e07f49fb6bc9a2dac6ac8be0baa2af842475d6a6d5246ffdbc1c928b | — | — |
| dixon-coles | 4ebab85c6a1864a0ed9e30062101eabeb74b0f40ea0c7ab9fed0b06a60e2d470 | apif,fd,jc | 芬超 | 2026-11-24 21:39:07 | 1172 | 2026-11-14 16:18:30 | mixed | 98.89% | 24971ea9e07f49fb6bc9a2dac6ac8be0baa2af842475d6a6d5246ffdbc1c928b | — | — |
| dixon-coles | 5a604baf26a3e4f10e89742e264fa7b3292d4f5d998034bd3377d9abe81180c6 | fd,jc | 巴甲 | 2026-11-24 21:39:07 | 2247 | 2026-11-24 21:34:09 | mixed | 98.18% | 24971ea9e07f49fb6bc9a2dac6ac8be0baa2af842475d6a6d5246ffdbc1c928b | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 0f0f89832d33d8da56a56bd7470daa3d25697e0ebf888da8b2299dc28a9add76 | apif,fd,jc | 美职 | 2026-11-24 21:39:07 | 4777 | 2026-11-24 21:34:09 | mixed | 99.41% | 24971ea9e07f49fb6bc9a2dac6ac8be0baa2af842475d6a6d5246ffdbc1c928b | — | — |
| dixon-coles | a27134898dc9d5f356c9703e3d53fe4c56d7642359d197e29f1384f262fb130c | apif,fd,fdorg,jc | 欧战 | 2026-11-24 21:39:07 | 28116 | 2026-11-24 21:34:09 | mixed | 97.61% | 24971ea9e07f49fb6bc9a2dac6ac8be0baa2af842475d6a6d5246ffdbc1c928b | 385035b903b4aa575365a8635f187e7d18bd49b5c24ab46cdfdfd5ed386ea1dd | 评估库 ≠ 训练库 |
| dixon-coles | c77f1539c26c4474adbe56b15f1440e98d4e9b8b6931cfdc15e9aba7c9462eca | apif,jc | 欧国联 | 2026-11-24 21:39:07 | 818 | 2026-11-18 11:18:59 | mixed | 87.29% | 24971ea9e07f49fb6bc9a2dac6ac8be0baa2af842475d6a6d5246ffdbc1c928b | — | — |
| dixon-coles | 47e8b606a46f412c74f586a7172f30ebbc10a1a44f8c942e8ace0a663fbdc2db | apif,jc | 韩职 | 2026-11-24 21:39:07 | 2237 | 2026-11-22 18:31:23 | mixed | 98.48% | 24971ea9e07f49fb6bc9a2dac6ac8be0baa2af842475d6a6d5246ffdbc1c928b | 1f5a99bfad007629272d2272fc0dfb10d4a11b9e0dcad775bb22f7dbc4d5f44f | 评估库 ≠ 训练库 |
| dixon-coles | 6ddcd436697132bee09e39a5844dee1e0fc15da57ad2e9771835e440bd3424dd | apif,jc | 澳超 | 2026-11-24 21:39:07 | 2288 | 2026-11-22 18:17:23 | mixed | 99.04% | 24971ea9e07f49fb6bc9a2dac6ac8be0baa2af842475d6a6d5246ffdbc1c928b | 1f5a99bfad007629272d2272fc0dfb10d4a11b9e0dcad775bb22f7dbc4d5f44f | 评估库 ≠ 训练库 |
| dixon-coles | 49ac4b47649fa567f055056da4351b42403ef767480622e426a0abd2dd933877 | apif,jc | 沙职 | 2026-11-24 21:39:07 | 2587 | 2026-11-21 18:13:05 | mixed | 99.50% | 24971ea9e07f49fb6bc9a2dac6ac8be0baa2af842475d6a6d5246ffdbc1c928b | 1dd8b10efce1fc56fb85ddc988fae7e7def8c908d37df16e0dae9ff62417994f | 评估库 ≠ 训练库 |
| dixon-coles | d7454a2e5f2817dc21d6c8ebb1d8528aa85b7096d669736e10b69419f99997e6 | apif,jc | 德乙 | 2026-11-24 21:39:07 | 4708 | 2026-11-23 01:22:23 | mixed | 99.64% | 24971ea9e07f49fb6bc9a2dac6ac8be0baa2af842475d6a6d5246ffdbc1c928b | 5f2eb263016233afd55c8d2b880ec21753881e49916b0ae3b4645751c5f325c1 | 评估库 ≠ 训练库 |
| dixon-coles | aabd56cd01e3cebf3ef59fe5ff7e22b7aff069ba66c509e825453409c46c15ea | apif,jc | 荷乙 | 2026-11-24 21:39:07 | 3882 | 2026-11-24 05:40:20 | mixed | 99.23% | 24971ea9e07f49fb6bc9a2dac6ac8be0baa2af842475d6a6d5246ffdbc1c928b | e1da97a554d7ef2ba40fe611a7c1c5eb962b8efba2117709bbdb6e9e96385710 | 评估库 ≠ 训练库 |

来源列为显式选择的训练语料范围；实际符合截点的训练来源及场数见 forecast.json 的 training_source_counts（null 表示旧模型未记录）。

## 纯模型与市场对照

| 比赛 ID / 编号 | 联赛 / 对阵 | 开球（北京时间） | 状态 / 排除原因 | 模型主胜 | 模型平局 | 模型客胜 | 市场主胜 | 市场平局 | 市场客胜 | SP 主胜 | SP 平局 | SP 客胜 | EV 主胜 | EV 平局 | EV 客胜 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| apif:1636301 / — | 欧罗巴 / 奈梅亨 vs 雷恩 | 2026-11-27 04:00:00 | 排除：缺少同来源同联赛的模型 | — | — | — | — | — | — | — | — | — | — | — | — |

## 比分前五与快照

