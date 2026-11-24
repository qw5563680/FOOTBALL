# P2 预测报告

> **非购买建议 · BET 禁用 · 各联赛P2历史评估门槛见逐场状态**

常规 90 分钟含补时。纯模型仅使用球队历史比分；市场 SP 和去水概率单独对照。
EV 为模型概率 × SP − 1 的点估计，EV > 0.05 仅加粗显示，不构成 BET 或购买建议。

批次：45466093990-1-2026-11-27；模式：prematch。
生成时间（北京时间）：2026-11-25 03:22:34；数据截止（北京时间）：2026-11-25 03:22:23。

## 模型与训练范围

版本为模型文件 SHA256；所有展示时间均为北京时间。assumed 占比表示使用假定赛果可知时间的训练场次占比，不代表已核实发布时间。

| 模型 | 版本 SHA256 | 来源 | 联赛 | 模型截点 | 训练场数 | 最近赛果可知时间 | 可知依据 | assumed 占比 | 训练库 SHA256 | 评估库 SHA256 | 数据库说明 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dixon-coles | 384306a527eda4cefdceb9cdc0af770c33b17fcb97144d94ea07683a1b4bf11a | fd,jc | 德甲 | 2026-11-25 03:10:47 | 1619 | 2026-11-23 20:08:39 | mixed | 95.06% | bf385d125271a5fbc18ea68674485bfd40696339a4fe7dce067e6b78d630ef57 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 443c13d706c0b54576a73402756cfe9e48e8fb8c5a1a786bffe5725e156e206c | fd,jc | 意甲 | 2026-11-25 03:10:47 | 2018 | 2026-11-24 21:34:09 | mixed | 95.14% | bf385d125271a5fbc18ea68674485bfd40696339a4fe7dce067e6b78d630ef57 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 58066f09e0f1f3d7e840f33621fc15906322e0fd76f08e787b0d13ae7c4338ef | fd,jc | 西甲 | 2026-11-25 03:10:47 | 2025 | 2026-11-24 21:34:09 | mixed | 95.41% | bf385d125271a5fbc18ea68674485bfd40696339a4fe7dce067e6b78d630ef57 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 53fa34e004d22ad17b865b1a0f695da92384d2f9628ba7657ae13df4a8be18a2 | fd,jc | 英超 | 2026-11-25 03:10:47 | 2009 | 2026-11-24 21:34:09 | mixed | 95.97% | bf385d125271a5fbc18ea68674485bfd40696339a4fe7dce067e6b78d630ef57 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 64d647adf0985a0ac9c2563da23cbfeedc66402b47a3d211ecb95511bd9a27e2 | fd,jc | 法甲 | 2026-11-25 03:10:47 | 1771 | 2026-11-23 20:08:39 | mixed | 97.52% | bf385d125271a5fbc18ea68674485bfd40696339a4fe7dce067e6b78d630ef57 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | eab50a4d4492a9c0f4b564f149aa675bb6d359759c34fde3c4dbc3e16bc50503 | apif,fd,fdorg,jc | 荷甲 | 2026-11-25 03:10:47 | 1646 | 2026-11-23 20:08:21 | mixed | 97.39% | bf385d125271a5fbc18ea68674485bfd40696339a4fe7dce067e6b78d630ef57 | — | — |
| dixon-coles | d37e2078b8542af73d716885dd46e6eba9e6992fcbe6f9682abf248e15bc688f | fd,jc | 葡超 | 2026-11-25 03:10:47 | 1628 | 2026-11-11 04:14:29 | mixed | 98.28% | bf385d125271a5fbc18ea68674485bfd40696339a4fe7dce067e6b78d630ef57 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 6983335e8cf69bca079cadcf453024998cd855cada9b8f1a8ab4a89983ee64fc | apif,fd,fdorg,jc | 英冠 | 2026-11-25 03:10:47 | 2952 | 2026-11-23 19:41:19 | mixed | 98.37% | bf385d125271a5fbc18ea68674485bfd40696339a4fe7dce067e6b78d630ef57 | — | — |
| dixon-coles | ad347eed3fb6c488206f65a3dbfd798a155c92744dcbce761e8d88f51049713e | apif,fd,jc | 瑞超 | 2026-11-25 03:10:47 | 1817 | 2026-11-24 21:34:09 | mixed | 98.29% | bf385d125271a5fbc18ea68674485bfd40696339a4fe7dce067e6b78d630ef57 | — | — |
| dixon-coles | 6b2f9744dddba89ea8742737688e930a07026ea1f822172c279e09575ebae6da | apif,fd,jc | 挪超 | 2026-11-25 03:10:47 | 1835 | 2026-11-23 20:08:39 | mixed | 98.53% | bf385d125271a5fbc18ea68674485bfd40696339a4fe7dce067e6b78d630ef57 | — | — |
| dixon-coles | cf8fd2b3ebcf03ebcdd99e3fb3312f90125cddae63a254c5d7dccea956378943 | apif,fd,jc | 法乙 | 2026-11-25 03:10:47 | 1854 | 2026-11-24 21:34:09 | mixed | 99.51% | bf385d125271a5fbc18ea68674485bfd40696339a4fe7dce067e6b78d630ef57 | — | — |
| dixon-coles | 5c9372820f22c03d2341e1b86b99f98392658ffa04cd51cf09e225cf7c35b2f9 | apif,fd,jc | 日职 | 2026-11-25 03:10:47 | 3006 | 2026-11-21 18:34:37 | mixed | 98.47% | bf385d125271a5fbc18ea68674485bfd40696339a4fe7dce067e6b78d630ef57 | — | — |
| dixon-coles | 86faebcb7d9061b8bc22b169f903e50f5de1c08260da20ed426de09751c59557 | apif,fd,jc | 芬超 | 2026-11-25 03:10:47 | 1172 | 2026-11-14 16:18:30 | mixed | 98.89% | bf385d125271a5fbc18ea68674485bfd40696339a4fe7dce067e6b78d630ef57 | — | — |
| dixon-coles | 7a859aa32cc90515bdb3b952495499f01348c16788031c04b1d19ebd658e4585 | fd,jc | 巴甲 | 2026-11-25 03:10:47 | 2247 | 2026-11-24 21:34:09 | mixed | 98.18% | bf385d125271a5fbc18ea68674485bfd40696339a4fe7dce067e6b78d630ef57 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | a9c2ba5397024ca2c94adf3421b92182d1028963718e65657c4e0a767aa25916 | apif,fd,jc | 美职 | 2026-11-25 03:10:47 | 4777 | 2026-11-24 21:34:09 | mixed | 99.41% | bf385d125271a5fbc18ea68674485bfd40696339a4fe7dce067e6b78d630ef57 | — | — |
| dixon-coles | be45c78ceb8019f5dfcb9c2473bf85a63c549dbd68a15f42d586fac11f633cbb | apif,fd,fdorg,jc | 欧战 | 2026-11-25 03:10:47 | 28116 | 2026-11-24 21:34:09 | mixed | 97.61% | bf385d125271a5fbc18ea68674485bfd40696339a4fe7dce067e6b78d630ef57 | 385035b903b4aa575365a8635f187e7d18bd49b5c24ab46cdfdfd5ed386ea1dd | 评估库 ≠ 训练库 |
| dixon-coles | e453fa8bcb3087548f7b8e25ba2ad3d080668a8ab534302c3317028d6cc3e136 | apif,jc | 欧国联 | 2026-11-25 03:10:47 | 818 | 2026-11-18 11:18:59 | mixed | 87.29% | bf385d125271a5fbc18ea68674485bfd40696339a4fe7dce067e6b78d630ef57 | — | — |
| dixon-coles | 5c9586865606ca72f2f33e49cbb3afd083d40b1425ca7a50d005069c4ed78926 | apif,jc | 韩职 | 2026-11-25 03:10:47 | 2237 | 2026-11-22 18:31:23 | mixed | 98.48% | bf385d125271a5fbc18ea68674485bfd40696339a4fe7dce067e6b78d630ef57 | 1f5a99bfad007629272d2272fc0dfb10d4a11b9e0dcad775bb22f7dbc4d5f44f | 评估库 ≠ 训练库 |
| dixon-coles | 204d9e337bd83485ddd59ca9296bc44ae9d50c2e723813c79d1c72204fffe278 | apif,jc | 澳超 | 2026-11-25 03:10:47 | 2288 | 2026-11-22 18:17:23 | mixed | 99.04% | bf385d125271a5fbc18ea68674485bfd40696339a4fe7dce067e6b78d630ef57 | 1f5a99bfad007629272d2272fc0dfb10d4a11b9e0dcad775bb22f7dbc4d5f44f | 评估库 ≠ 训练库 |
| dixon-coles | c64ddc4c74a25fafd1ea62deb29ed085189d95351b264845c54385114176a422 | apif,jc | 沙职 | 2026-11-25 03:10:47 | 2587 | 2026-11-21 18:13:05 | mixed | 99.50% | bf385d125271a5fbc18ea68674485bfd40696339a4fe7dce067e6b78d630ef57 | 1dd8b10efce1fc56fb85ddc988fae7e7def8c908d37df16e0dae9ff62417994f | 评估库 ≠ 训练库 |
| dixon-coles | e947497c7a3a41c909cc8ea11a505da7e156209b159e197327f3fe583d7dda98 | apif,jc | 德乙 | 2026-11-25 03:10:47 | 4708 | 2026-11-23 01:22:23 | mixed | 99.64% | bf385d125271a5fbc18ea68674485bfd40696339a4fe7dce067e6b78d630ef57 | 5f2eb263016233afd55c8d2b880ec21753881e49916b0ae3b4645751c5f325c1 | 评估库 ≠ 训练库 |
| dixon-coles | 8e143c93adbd4f6cef81ae9c4f71f293a368bd9fe7cf8d92bc0098225e52a29a | apif,jc | 荷乙 | 2026-11-25 03:10:47 | 3882 | 2026-11-24 05:40:20 | mixed | 99.23% | bf385d125271a5fbc18ea68674485bfd40696339a4fe7dce067e6b78d630ef57 | e1da97a554d7ef2ba40fe611a7c1c5eb962b8efba2117709bbdb6e9e96385710 | 评估库 ≠ 训练库 |

来源列为显式选择的训练语料范围；实际符合截点的训练来源及场数见 forecast.json 的 training_source_counts（null 表示旧模型未记录）。

## 纯模型与市场对照

| 比赛 ID / 编号 | 联赛 / 对阵 | 开球（北京时间） | 状态 / 排除原因 | 模型主胜 | 模型平局 | 模型客胜 | 市场主胜 | 市场平局 | 市场客胜 | SP 主胜 | SP 平局 | SP 客胜 | EV 主胜 | EV 平局 | EV 客胜 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| apif:1636301 / — | 欧罗巴 / 奈梅亨 vs 雷恩 | 2026-11-27 04:00:00 | 排除：缺少同来源同联赛的模型 | — | — | — | — | — | — | — | — | — | — | — | — |

## 比分前五与快照

