# P2 预测报告

> **非购买建议 · BET 禁用 · 各联赛P2历史评估门槛见逐场状态**

常规 90 分钟含补时。纯模型仅使用球队历史比分；市场 SP 和去水概率单独对照。
EV 为模型概率 × SP − 1 的点估计，EV > 0.05 仅加粗显示，不构成 BET 或购买建议。

批次：42528836513-1-2026-11-10；模式：prematch。
生成时间（北京时间）：2026-11-09 06:13:29；数据截止（北京时间）：2026-11-09 06:13:28。

## 模型与训练范围

版本为模型文件 SHA256；所有展示时间均为北京时间。assumed 占比表示使用假定赛果可知时间的训练场次占比，不代表已核实发布时间。

| 模型 | 版本 SHA256 | 来源 | 联赛 | 模型截点 | 训练场数 | 最近赛果可知时间 | 可知依据 | assumed 占比 | 训练库 SHA256 | 评估库 SHA256 | 数据库说明 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dixon-coles | 4d77302f0ec477821cf2da0922907f9df491dea95cfa60db74c5506b55cf58ed | fd,jc | 德甲 | 2026-11-09 06:10:54 | 1608 | 2026-11-08 17:53:22 | mixed | 95.71% | eee6dc59e06fe00f7ecdb4d95173ec6bb5c2de3c970c5d3ac7973d3b6a35b1d4 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | cd20ce882b16431345d12623d839a746ab248212358e66a6823b96964c08da8f | fd,jc | 意甲 | 2026-11-09 06:10:54 | 2006 | 2026-11-09 00:36:53 | mixed | 95.71% | eee6dc59e06fe00f7ecdb4d95173ec6bb5c2de3c970c5d3ac7973d3b6a35b1d4 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 0decfc17b4b586e13264eae940949b8cc386a11f63adf83d5058a92963a19686 | fd,jc | 西甲 | 2026-11-09 06:10:54 | 2013 | 2026-11-08 17:53:22 | mixed | 95.93% | eee6dc59e06fe00f7ecdb4d95173ec6bb5c2de3c970c5d3ac7973d3b6a35b1d4 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | d3c77c290081025840fab033e5e5527e6e1cf386a770d7f978b1476de673f695 | fd,jc | 英超 | 2026-11-09 06:10:54 | 1988 | 2026-11-08 17:53:22 | mixed | 96.58% | eee6dc59e06fe00f7ecdb4d95173ec6bb5c2de3c970c5d3ac7973d3b6a35b1d4 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | e77aca8b7884bfafd0005b66f8d01e68124dc6063cd0d5515d65241034a4daad | fd,jc | 法甲 | 2026-11-09 06:10:54 | 1760 | 2026-11-08 17:53:22 | mixed | 97.78% | eee6dc59e06fe00f7ecdb4d95173ec6bb5c2de3c970c5d3ac7973d3b6a35b1d4 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 08ad8f9468dcd4f60e0894b677e4c89f677099efd58c154efba725718c107651 | fd,jc | 荷甲 | 2026-11-09 06:10:54 | 1629 | 2026-11-08 17:52:48 | mixed | 97.97% | eee6dc59e06fe00f7ecdb4d95173ec6bb5c2de3c970c5d3ac7973d3b6a35b1d4 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | ff9440813b37942c50b5fa6cba347b313d4af09fdf2957e5d7f825a59707a4e6 | fd,jc | 葡超 | 2026-11-09 06:10:54 | 1616 | 2026-11-08 17:53:22 | mixed | 98.33% | eee6dc59e06fe00f7ecdb4d95173ec6bb5c2de3c970c5d3ac7973d3b6a35b1d4 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 3d6ebfd98ba4d300b2dc7c69a3fc77a87ce188417097963ffbb5c7ad850ea6d7 | fd,jc | 英冠 | 2026-11-09 06:10:54 | 2920 | 2026-11-08 17:52:48 | mixed | 98.77% | eee6dc59e06fe00f7ecdb4d95173ec6bb5c2de3c970c5d3ac7973d3b6a35b1d4 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | e4911a0c5b46b7e0f321dbe8ded0af279af6f2da4c424e7df821e6080f6afaac | fd,jc | 瑞超 | 2026-11-09 06:10:54 | 1409 | 2026-11-08 17:53:22 | mixed | 98.37% | eee6dc59e06fe00f7ecdb4d95173ec6bb5c2de3c970c5d3ac7973d3b6a35b1d4 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 3a35429a9d7d6d191b3eea7f561568a75937bd6787e04d7c031d77ef29c8e918 | fd,jc | 挪超 | 2026-11-09 06:10:54 | 1391 | 2026-11-02 22:57:58 | mixed | 98.56% | eee6dc59e06fe00f7ecdb4d95173ec6bb5c2de3c970c5d3ac7973d3b6a35b1d4 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | c5a2da0a0e963982320dbd8ded8a68a8fd2484d51b0acc38b7edd960f02ee47e | fd,jc | 法乙 | 2026-11-09 06:10:54 | 1841 | 2026-11-07 17:26:10 | mixed | 99.57% | eee6dc59e06fe00f7ecdb4d95173ec6bb5c2de3c970c5d3ac7973d3b6a35b1d4 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 8d6294b4a4d6a85b9b66c5e6bc313b0deb39848c7b105fd59cf647e61af03932 | fd,jc | 日职 | 2026-11-09 06:10:54 | 1876 | 2026-11-08 17:53:22 | mixed | 98.08% | eee6dc59e06fe00f7ecdb4d95173ec6bb5c2de3c970c5d3ac7973d3b6a35b1d4 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 07b0cb448c3089885c669501ef8ea483a491fc44bd1a8bacf01931e37c708ab0 | fd,jc | 芬超 | 2026-11-09 06:10:54 | 1015 | 2026-11-03 18:07:17 | mixed | 98.82% | eee6dc59e06fe00f7ecdb4d95173ec6bb5c2de3c970c5d3ac7973d3b6a35b1d4 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 5d2d7c639849ad88f7dd8948f32b1f5f9e1af715752c5e2208a20d57193ed5fe | fd,jc | 巴甲 | 2026-11-09 06:10:54 | 2223 | 2026-11-07 17:26:10 | mixed | 98.56% | eee6dc59e06fe00f7ecdb4d95173ec6bb5c2de3c970c5d3ac7973d3b6a35b1d4 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | 90c42c4cb5c4ef4a6572bac95f7cacfcff7c45ae997d29b2376628900b02367b | fd,jc | 美职 | 2026-11-09 06:10:54 | 2969 | 2026-11-08 17:53:22 | mixed | 99.33% | eee6dc59e06fe00f7ecdb4d95173ec6bb5c2de3c970c5d3ac7973d3b6a35b1d4 | 043ffdcc0cf337a94369fd105a3500033c9e6bbb1c8684410b525eb5fdb31e09 | 评估库 ≠ 训练库 |
| dixon-coles | ce51dc830d260061ddd0c2bd8d409fda5cc6c8bc596e45f6bd99240d0e742047 | apif,fd,fdorg,jc | 欧战 | 2026-11-09 06:10:54 | 20578 | 2026-11-09 06:08:51 | mixed | 97.04% | eee6dc59e06fe00f7ecdb4d95173ec6bb5c2de3c970c5d3ac7973d3b6a35b1d4 | 385035b903b4aa575365a8635f187e7d18bd49b5c24ab46cdfdfd5ed386ea1dd | 评估库 ≠ 训练库 |
| dixon-coles | 557123252b19f49b67f0d87ba624f717d357ede4119c963821ddb005c3fb45be | apif,jc | 欧国联 | 2026-11-09 06:10:54 | 766 | 2026-11-01 21:08:02 | mixed | 93.21% | eee6dc59e06fe00f7ecdb4d95173ec6bb5c2de3c970c5d3ac7973d3b6a35b1d4 | — | — |

来源列为显式选择的训练语料范围；实际符合截点的训练来源及场数见 forecast.json 的 training_source_counts（null 表示旧模型未记录）。

## 纯模型与市场对照

| 比赛 ID / 编号 | 联赛 / 对阵 | 开球（北京时间） | 状态 / 排除原因 | 模型主胜 | 模型平局 | 模型客胜 | 市场主胜 | 市场平局 | 市场客胜 | SP 主胜 | SP 平局 | SP 客胜 | EV 主胜 | EV 平局 | EV 客胜 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |

## 比分前五与快照

