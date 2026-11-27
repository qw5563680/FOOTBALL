# P2 预测报告

> **非购买建议 · BET 禁用 · 各联赛P2历史评估门槛见逐场状态**

常规 90 分钟含补时。纯模型仅使用球队历史比分；市场 SP 和去水概率单独对照。
EV 为模型概率 × SP − 1 的点估计，EV > 0.05 仅加粗显示，不构成 BET 或购买建议。

批次：46043284197-1-2026-11-30；模式：prematch。
生成时间（北京时间）：2026-11-28 02:02:53；数据截止（北京时间）：2026-11-28 02:02:38。

## 模型与训练范围

版本为模型文件 SHA256；所有展示时间均为北京时间。assumed 占比表示使用假定赛果可知时间的训练场次占比，不代表已核实发布时间。

| 模型 | 版本 SHA256 | 来源 | 联赛 | 模型截点 | 训练场数 | 最近赛果可知时间 | 可知依据 | assumed 占比 | 训练库 SHA256 | 评估库 SHA256 | 数据库说明 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dixon-coles | b8e575015363323517cb2dac9b31e254d4164e16cd7d5c76e6dcc3becd4fedd9 | fd,jc | 德甲 | 2026-11-28 01:45:56 | 1619 | 2026-11-23 20:08:39 | mixed | 95.06% | bceff1bb8e4a8cdadaca80bc9b0a57c60fb1f47faffef91d96d2c242c964adc4 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 41b48acb9004455a3f4f021efdfe9652a4b90020631dff10ffe2064e71b33b5e | fd,jc | 意甲 | 2026-11-28 01:45:56 | 2018 | 2026-11-24 21:34:09 | mixed | 95.14% | bceff1bb8e4a8cdadaca80bc9b0a57c60fb1f47faffef91d96d2c242c964adc4 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | e204c119388912547a31de446a396d3f7adea8d91568c46d23dec7a3adaf2bfb | fd,jc | 西甲 | 2026-11-28 01:45:56 | 2025 | 2026-11-24 21:34:09 | mixed | 95.41% | bceff1bb8e4a8cdadaca80bc9b0a57c60fb1f47faffef91d96d2c242c964adc4 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | aa2b757eb78338c72442da15e6c1fc990d45246fe10a529594d5b324fe8bc9e0 | fd,jc | 英超 | 2026-11-28 01:45:56 | 2009 | 2026-11-24 21:34:09 | mixed | 95.97% | bceff1bb8e4a8cdadaca80bc9b0a57c60fb1f47faffef91d96d2c242c964adc4 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 5844ce92ae1f089db34c0aa83b3d8a0876436a069a4c024d1ff39c8793ae8368 | fd,jc | 法甲 | 2026-11-28 01:45:56 | 1771 | 2026-11-23 20:08:39 | mixed | 97.52% | bceff1bb8e4a8cdadaca80bc9b0a57c60fb1f47faffef91d96d2c242c964adc4 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 08be65f5f49f3694c9baa9e8239948a9c3844806e12e8f8faac24aaeb48aed70 | apif,fd,fdorg,jc | 荷甲 | 2026-11-28 01:45:56 | 1646 | 2026-11-23 20:08:21 | mixed | 97.39% | bceff1bb8e4a8cdadaca80bc9b0a57c60fb1f47faffef91d96d2c242c964adc4 | — | — |
| dixon-coles | c5c8ce934f6a814f10301482e643aa7565361e031d639f1e620d132095e6f78b | fd,jc | 葡超 | 2026-11-28 01:45:56 | 1628 | 2026-11-11 04:14:29 | mixed | 98.28% | bceff1bb8e4a8cdadaca80bc9b0a57c60fb1f47faffef91d96d2c242c964adc4 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 7a0c8ce35bf1e68ab43620afe53aa67e801d4c465a430d9142ba5dce73992e10 | apif,fd,fdorg,jc | 英冠 | 2026-11-28 01:45:56 | 2964 | 2026-11-26 11:12:53 | mixed | 97.98% | bceff1bb8e4a8cdadaca80bc9b0a57c60fb1f47faffef91d96d2c242c964adc4 | — | — |
| dixon-coles | 3cc29d16c833aba53ccba1bb53e39d57bfe7c602583846a5d708f8a23172dfbd | apif,fd,jc | 瑞超 | 2026-11-28 01:45:56 | 1817 | 2026-11-24 21:34:09 | mixed | 98.29% | bceff1bb8e4a8cdadaca80bc9b0a57c60fb1f47faffef91d96d2c242c964adc4 | — | — |
| dixon-coles | 50be6885ff4ad1b5f8e3f9bee876c456860f51c140bf96589dcba2bbee860439 | apif,fd,jc | 挪超 | 2026-11-28 01:45:56 | 1835 | 2026-11-23 20:08:39 | mixed | 98.53% | bceff1bb8e4a8cdadaca80bc9b0a57c60fb1f47faffef91d96d2c242c964adc4 | — | — |
| dixon-coles | 9779a6ebe8ab06d344cd3163fc4a6e0f2d9f06a163fe9c3cbbbd0552d00bbf3a | apif,fd,jc | 法乙 | 2026-11-28 01:45:56 | 1854 | 2026-11-24 21:34:09 | mixed | 99.51% | bceff1bb8e4a8cdadaca80bc9b0a57c60fb1f47faffef91d96d2c242c964adc4 | — | — |
| dixon-coles | 1fe51e3cd49ec475994d275dafa86f6361ff13899e9a16bef6b5dca1be637435 | apif,fd,jc | 日职 | 2026-11-28 01:45:56 | 3012 | 2026-11-26 03:34:44 | mixed | 98.27% | bceff1bb8e4a8cdadaca80bc9b0a57c60fb1f47faffef91d96d2c242c964adc4 | — | — |
| dixon-coles | d792b38107cecccef29190e077e2eda1954e288aae6f0f4f5ba8a2008a6f3506 | apif,fd,jc | 芬超 | 2026-11-28 01:45:56 | 1172 | 2026-11-14 16:18:30 | mixed | 98.89% | bceff1bb8e4a8cdadaca80bc9b0a57c60fb1f47faffef91d96d2c242c964adc4 | — | — |
| dixon-coles | eb82046db94b911bb2984ba68ba65ed50cd248d491912fbf90b405100648b72b | fd,jc | 巴甲 | 2026-11-28 01:45:56 | 2247 | 2026-11-24 21:34:09 | mixed | 98.18% | bceff1bb8e4a8cdadaca80bc9b0a57c60fb1f47faffef91d96d2c242c964adc4 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 12168ec9d12744aa7e6f93b598a0edf527bf5149ab1f936395329c3820dc5930 | apif,fd,jc | 美职 | 2026-11-28 01:45:56 | 4777 | 2026-11-24 21:34:09 | mixed | 99.41% | bceff1bb8e4a8cdadaca80bc9b0a57c60fb1f47faffef91d96d2c242c964adc4 | — | — |
| dixon-coles | 4427f312573716486232fcdf514c353262c7e16e62a22859643f1201f6400e98 | apif,fd,fdorg,jc | 欧战 | 2026-11-28 01:45:56 | 28144 | 2026-11-27 11:27:26 | mixed | 97.51% | bceff1bb8e4a8cdadaca80bc9b0a57c60fb1f47faffef91d96d2c242c964adc4 | 385035b903b4aa575365a8635f187e7d18bd49b5c24ab46cdfdfd5ed386ea1dd | 评估库 ≠ 训练库 |
| dixon-coles | 9cdfa4707c697463f3884d8a05214bfb842f6d92cebb2b9b3f5415c329a8de5d | apif,jc | 欧国联 | 2026-11-28 01:45:56 | 818 | 2026-11-18 11:18:59 | mixed | 87.29% | bceff1bb8e4a8cdadaca80bc9b0a57c60fb1f47faffef91d96d2c242c964adc4 | — | — |
| dixon-coles | 6f6b6c94aa11cf7b3dcd5cc8d682aae6e051fe51423d536ba1378789a003ae9e | apif,jc | 韩职 | 2026-11-28 01:45:56 | 2237 | 2026-11-22 18:31:23 | mixed | 98.48% | bceff1bb8e4a8cdadaca80bc9b0a57c60fb1f47faffef91d96d2c242c964adc4 | 1f5a99bfad007629272d2272fc0dfb10d4a11b9e0dcad775bb22f7dbc4d5f44f | 评估库 ≠ 训练库 |
| dixon-coles | a30a657ea54e931a35a6ec8627fa7883931f2b222e6d8486e310fc6d0d544b7a | apif,jc | 澳超 | 2026-11-28 01:45:56 | 2289 | 2026-11-27 19:03:29 | mixed | 99.00% | bceff1bb8e4a8cdadaca80bc9b0a57c60fb1f47faffef91d96d2c242c964adc4 | 1f5a99bfad007629272d2272fc0dfb10d4a11b9e0dcad775bb22f7dbc4d5f44f | 评估库 ≠ 训练库 |
| dixon-coles | 60fb32d9575381efe20e9ebce0117c30209dbbfc8011b4ea8462e41da9ac6466 | apif,jc | 沙职 | 2026-11-28 01:45:56 | 2588 | 2026-11-27 22:23:54 | mixed | 99.46% | bceff1bb8e4a8cdadaca80bc9b0a57c60fb1f47faffef91d96d2c242c964adc4 | 1dd8b10efce1fc56fb85ddc988fae7e7def8c908d37df16e0dae9ff62417994f | 评估库 ≠ 训练库 |
| dixon-coles | f540c3514588bca8aef3d66cf5e320d7b97aa1dfc811c83e3fdc77c81f1f5358 | apif,jc | 德乙 | 2026-11-28 01:45:56 | 4708 | 2026-11-23 01:22:23 | mixed | 99.64% | bceff1bb8e4a8cdadaca80bc9b0a57c60fb1f47faffef91d96d2c242c964adc4 | 5f2eb263016233afd55c8d2b880ec21753881e49916b0ae3b4645751c5f325c1 | 评估库 ≠ 训练库 |
| dixon-coles | 2dbbc39469850b99c3790344c5c577c2e6335e5f5378cbee1936f9ddc8cf7bac | apif,jc | 荷乙 | 2026-11-28 01:45:56 | 3882 | 2026-11-24 05:40:20 | mixed | 99.23% | bceff1bb8e4a8cdadaca80bc9b0a57c60fb1f47faffef91d96d2c242c964adc4 | e1da97a554d7ef2ba40fe611a7c1c5eb962b8efba2117709bbdb6e9e96385710 | 评估库 ≠ 训练库 |

来源列为显式选择的训练语料范围；实际符合截点的训练来源及场数见 forecast.json 的 training_source_counts（null 表示旧模型未记录）。

## 纯模型与市场对照

| 比赛 ID / 编号 | 联赛 / 对阵 | 开球（北京时间） | 状态 / 排除原因 | 模型主胜 | 模型平局 | 模型客胜 | 市场主胜 | 市场平局 | 市场客胜 | SP 主胜 | SP 平局 | SP 客胜 | EV 主胜 | EV 平局 | EV 客胜 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| jc:2043126 / 周日017 | 英超 / 阿森纳 vs 曼城 | 2026-11-30 00:30:00 | 已预测 · 可用（旧规，考核中） · 仅历史证据 | 46.06% | 29.24% | 24.70% | 45.48% | 26.20% | 28.32% | 1.93 | 3.35 | 3.1 | -11.10% | -2.06% | -23.42% |
| jc:2043131 / 周日022 | 葡超 / 波尔图 vs 里斯本 | 2026-11-30 04:30:00 | 已预测 · 可用（旧规，考核中） · 仅历史证据 | 41.59% | 27.97% | 30.44% | 50.15% | 25.81% | 24.04% | 1.75 | 3.4 | 3.65 | -27.21% | -4.90% | **11.09%** |

## 比分前五与快照

- jc:2043126：1:1（13.89%）、1:0（11.23%）、0:0（9.57%）、2:1（8.97%）、2:0（8.85%）；市场快照（北京时间）：2026-11-27 21:35:19；模型：dixon-coles / aa2b757eb78338c72442da15e6c1fc990d45246fe10a529594d5b324fe8bc9e0。
- jc:2043131：1:1（13.26%）、1:0（10.99%）、0:1（9.19%）、2:1（8.46%）、0:0（7.73%）；市场快照（北京时间）：2026-11-27 21:35:19；模型：dixon-coles / c5c8ce934f6a814f10301482e643aa7565361e031d639f1e620d132095e6f78b。

半全场（实验，纯推导量；不参与任何下注建议，BET 禁用）：

- 阿森纳 vs 曼城（jc:2043126）：胜/胜 27.62%；胜/平 5.25%；胜/负 1.45%；平/胜 16.12%；平/平 18.73%；平/负 9.53%；负/胜 2.31%；负/平 5.25%；负/负 13.73%；半场进球占比 0.443535（联赛，2009 场）。
- 波尔图 vs 里斯本（jc:2043131）：胜/胜 24.37%；胜/平 5.48%；胜/负 1.75%；平/胜 14.99%；平/平 17.01%；平/负 11.59%；负/胜 2.23%；负/平 5.48%；负/负 17.10%；半场进球占比 0.442486（联赛，1628 场）。

## 球队＋市场（实验）

- jc:2043126：45.48% / 26.20% / 28.32%，市场权重 100.00%。
- jc:2043131：50.15% / 25.81% / 24.04%，市场权重 100.00%。
