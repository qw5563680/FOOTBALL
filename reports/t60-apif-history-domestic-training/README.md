# T60 韩职、澳超 APIF 历史训练验收

日期：2026-11-14 UTC。基线代码 `b03e00f25be95e5d74fbf03fa71d8f0bed5981a0`；
固定数据 `1657093073ea0e231cef476e1eac4e9ac58ebcb8`。只接入韩职、澳超，
不改变已有 15 国内联赛、欧战、欧国联、身份桥接、先验、P2 gate 或 BET 禁用规则。

## 固定输入与交付

- 原归档 manifest run `43529278648-1`，生成 `2026-11-13T23:46:10.162655+00:00`。
- 正常 restore 的静态库 SHA256：`1f5a99bfad007629272d2272fc0dfb10d4a11b9e0dcad775bb22f7dbc4d5f44f`。
- 最终完整参数 SHA256：`fdefd74a71126b9579b040caf9daff107b1df2ee74d80b616ad7e45e3558211f`。
  旧 17 条配置和文件内 gate 内容完整保留，只追加韩职、澳超。
- 显式清单 `data/model/apif_history_training.json` 仅含韩职、澳超，允许来源集合均为 `apif,jc`。
  不从 `data/api_football/leagues.json` 推断资格；清单内未知来源也拒绝。后续同类国内联赛由清单与参数接入。
- 网络/API 请求 0，正式 key 使用 0，无生产归档写入；没有回写冻结预测或旧账本。
- 完整证据在数据分支
  [`data/t60-apif-history-acceptance-20261114` 的 `acceptance/t60-apif-history-domestic-training/`](https://github.com/qw5563680/FOOTBALL/tree/data/t60-apif-history-acceptance-20261114/acceptance/t60-apif-history-domestic-training)。
  本目录仅 README 与 SHA256SUMS；静态库和整份回放归档不重复发布。
  本地原始工作目录 `/workspace/work/t60-evidence`；`evidence-manifest.json` 列出发布文件、大小和哈希。

## 真实历史与既有选参协议

韩职实际比赛日期支持 UTC 自然年；澳超各季从 10 月到次年 5–6 月，按既有 UTC 7 月跨年分类。
1 月 / 7 月是评估分类边界，不冒称赛事开幕日期。`data/fixtures/t60/provenance.json` 保存各季首尾与哈希。

| 联赛 | 完整历史季及 APIF 场数 | 部分季 APIF / JC | 全归档结果行 |
| --- | --- | --- | ---: |
| 韩职 | 2022 / 232；2023 / 232；2024 / 232；2025 / 232 | 2026：185 / 25 | 1138 |
| 澳超 | 2223 / 163；2324 / 169；2425 / 176；2526 / 163 | 2627：10 / 12 | 693 |

执行原 `predict.tuning.rolling_origin` 和 `backtest.evaluate`，没有新增实验协议：
T15/T16 的网格 `90,180,270,365,540,730`，赛季 S 按月 walk-forward 最小模型 Log Loss，
只接受完整相同样本，平手取较短半衰期。截点前累积训练，S+1 验证并另做 weekly 报告。
选参季的平局偏差绝对值超过 3pp 才在下一季开启既有训练内校准，不能以验证成绩回选。
选参截点 `2026-11-14T00:00:00Z`。SQL guard 拒绝任何 odds 表读取，选参/验证只传赛果与空市场对照。

| 联赛 | S → S+1 | 选择半衰期 | S+1 校准 |
| --- | --- | ---: | --- |
| 韩职 | 2023 → 2024 | 365 天 | 开 |
| 韩职 | 2024 → 2025 | 90 天 | 关 |
| 韩职 | 2025 → 2026（部分季） | **90 天** | 关 |
| 澳超 | 2324 → 2425 | 270 天 | 开 |
| 澳超 | 2425 → 2526 | 90 天 | 关 |
| 澳超 | 2526 → 2627（部分季） | **365 天** | 开 |

两者都实际完成选参；澳超的 365 天不是“未调参”回退值。先验仍为
`weakest_quartile_half_shrinkage`，不引入赔率、新先验或新的校准算法。

| 联赛 | 最近两完整验证季 | weekly n | 模型 LL | 等概率 LL | 相对改善 |
| --- | --- | ---: | ---: | ---: | ---: |
| 韩职 | 2024、2025 | 464 | 1.090127 | 1.098612 | 0.008485 |
| 澳超 | 2425、2526 | 339 | 1.095164 | 1.098612 | 0.003448 |

均不足 n≥500 与改善≥0.05，且本次无收盘赔率对照，`p2_ready=false`、**P2 实验**。
既有 gate 的 `empty_season_evidence` 在此由收盘对照为空触发，不表示赛果历史缺季。
2026 / 2627 部分季 weekly n=210 / 22，LL=1.114166 / 1.173238，仅列于全量证据，未当完整季。
全 weekly 结果无跳过、不可知标签为 0；含部分季的 assumed 训练加权占比分别 99.9735% / 99.9674%，
assumed 评估占比 95.8457% / 95.2909%，replayed 比例均 0。回填的开球后 3h 可知时间是假设，
不等于当时实际获得了赛果；没有改动原 available/captured 时间。
**以上数字仅描述历史回放，不能据此宣称模型有效；有效性只看上线后前瞻样本。**

## 旧模型逐字节回归

旧配置是 **17 个模型、19 项赛事**：15 国内模型、1 个覆盖欧冠/欧罗巴/欧协联的欧战池、1 个欧国联。
分别在基线导出的原始代码和改动后的代码调用原生 `train()`；两边都使用**同一份最终完整参数文件**，
因此基线 daily 入口拒绝新增条目的行为没有被绕过、修改或用于此对比。
旧版 `train()` 原生能读取包含新增条目的参数文件；只请求原 17 个模型。

固定输入除上述 DB / 最终参数外还有：

- 参数绝对路径 `/workspace/work/football-t60/data/model/params.json`；输出统一为 `/workspace/work/t60-evidence/same-output/`。
- 训练截点 `2026-11-06T00:00:00+00:00`，smoke since `2026-09-11T00:00:00+00:00`，原始 availability 规则。
- `PYTHONPATH=src PYTHONHASHSEED=0 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1`；Python 3.12.14，penaltyblog 1.13.1。
- 截点后的真实归档比赛，覆盖 19 项赛事、67 条预测；完整概率、比分矩阵及 metadata 一并序列化，无错误记录。

**17 pkl、17 完整 training JSON、17 完整概率 JSON 全部逐字节相同**；两个 manifest 也相同，合计 52 对。
没有删除、归一化或忽略任何元数据。`DixonColes.save` 没有额外序列化时钟；训练截点已固定。
完整结果见 `byte-comparison.json`、`before/`、`after/`；下表为两边共同的 pkl 哈希，其他哈希全部列入 SHA256SUMS。

| 旧模型 | before = after 的完整 pkl SHA256 |
| --- | --- |
| 德甲 | `152fcb6db326352b2d9c4cd28c3d4767420a863ac291e6a61d62d526a630a328` |
| 意甲 | `933e1a1b8b8a9fb4472b0a278d588fcb2eae59a2265cf7e4cca37883fd4c9374` |
| 西甲 | `6c4c5356d2eec1dbc95170efec18d5814385924f0e6e02ecb70eeb1ad352bae4` |
| 英超 | `cdc8bece4446dcdb6b274baeb6bc6148475e68b6e94e3d8311d83bf5c69e3930` |
| 法甲 | `b1b752e79840da66a0ac78776e1d8c49cdf45ff813924c4f59eb7bab62d8cf96` |
| 荷甲 | `b3a18c5cb588d4e22525caad0915523a9ba3f6a8a35fa4f7bb22d36e40f65d4e` |
| 葡超 | `8e9562099bf3cf724736f1b24fd070b675e1bf91f7e06d69f6d64b01cdbce88c` |
| 英冠 | `4680b3bb9f5f137d2c62c63707144049e44478ce35841a4a7147e8d2e4060785` |
| 瑞超 | `308980bcce9d9473644b2ff3b211a2e255570053290324efa5cb4eb89e08c420` |
| 挪超 | `b668c28b765ac23cb9b42826500cb1e28b1cdd9faf578644cc1efe3a6763fa77` |
| 法乙 | `2c72e9dbf7257f2cd101447d3b9cb605922c5084f1e682321c7db844f7f82844` |
| 日职 | `f14290b19bce6862a7a922122b26ef778ba421db7529510ab7463e8aa2a4dfe2` |
| 芬超 | `5029db29fbaf55a349b017f58a726d60d3143b61832d21af9b33a105d01fe8b2` |
| 巴甲 | `aeafa840476006a885aba442df9139d25b47e49d9d02244db45b7e285e462cd7` |
| 美职 | `a6c8de2c7f16b1eedc2b8154652fa522d5300c3b5bdf0b71bf217f9bb26c3e48` |
| 欧战 | `a404c2b4dc8cc3130bc1a95f386d3ac2f33802a612cf69f5c400d50f597a34de` |
| 欧国联 | `e21a649ac5da97e009ba46173a0165ce6ee26f571195e147ecb3226a3e119d08` |

此结论是**相同最终参数输入上的代码前后对照**。此前发布的旧参数全文件哈希为
`5482a55708eeb6b9369c3f6c7ea638927288b959809801f81c1ea56acd4d3f0d`，
新增两个条目会改变全文件 SHA，模型 provenance 随之不同；不声称与此前线上旧参数产物的文件字节相同。
没有为强求哈希一致而改变元数据算法。原归档全部文件另行核验不变。

## 实际每日入口的离线历史流程回放

固定归档已没有韩职/澳超未开赛 JC 目标，最新两联赛 JC 场次都已 finished。
据真实冻件 `forecasts/2026-11-07/42157651542-1/forecast.json`，
在截点 `2026-11-06T03:39:54.531777+00:00` 这两场曾因“缺少同来源同联赛的模型”而排除，
证明当时已记录正确球队、主客与开球时间，且已通过赛前状态检查。

完整归档复制到独立目录后，执行实际 `cloud_predict.run()`。测试 harness 只在正常 restore 校验完成后的临时 DB
将下列两场 `finished → scheduled`；保留全部比赛其他字段、赛果与 available 时间。
所有差异、原始结果及历史冻件 SHA 均见 `daily-replay-evidence.json`。没有绕过导入来源或训练守卫。
整个入口训练 19 个模型 / 21 项赛事；正式冻结结果 `status=complete`。

| 场次 | 实际开球 UTC | 训练行 / 来源 | 半衰期 | 结果 |
| --- | --- | --- | ---: | --- |
| 韩职 `jc:2042585` 金泉尚武–安养 | 2026-11-07 07:30 | 1132 = apif 1110 + jc 22；覆盖 2022–2026 | 90 天 | predicted；P2 实验 |
| 澳超 `jc:2042586` 珀斯–纽喷气机 | 2026-11-07 10:45 | 687 = apif 678 + jc 9；覆盖 2223–2627 | 365 天 | predicted；P2 实验 |

两者模型 `sources=["apif","jc"]`，`p2_ready=false`，BET false。训练最大 available 分别为
`2026-11-01T13:26:51.617793+00:00` 与 `2026-11-01T20:42:04.907539+00:00`，均严格早于截点。
本归档中 JC counterpart 的 APIF 侧要么没有入库、要么仍 scheduled 且没有结果，
读取训练视图后同时存在的 counterpart 对数为 0，linked 排除数也为 0；不虚报发生了实际去重。
单测另用真实链接身份、显式注入后来完成的 APIF 赛果，验证只保留 JC；断链、错身份和未解析身份均被原 guard 拒绝。

原归档前后 **2794 个文件（包括本地 worktree .git 指针）** SHA 完全一致。
本地复制品中的新冻件独立归档，未向生产写入。
附带技术特征实验因离线环境未配置 GitHub Release repository 而报告失败，正式冻件已成功保留；
日志完整附带，未为其注入凭据或请求网络。
**这是 2026-11-14 执行的离线历史流程回放，不是当时已经存在的预测，也不是当前未来场的线上验收。**
合并后下一次真实韩职/澳超冻结预测并进入记分板，仍待审查者验证。

## 验证与复算

- 测试先红后绿：初始 21 项有 8 项在未实现分支失败；随后补齐到 33 项 T60 测试全部通过。
  覆盖白名单/未知来源/七个 identity-only 联赛、仅配置扩展、链接去重、未链接/错误身份重叠、
  截点相等/之后/未知可知时间排除、真实赛季边界和真实身份每日入口。
- 全量 `PYTHONPATH=src python -m pytest -q`：**3029 passed，0 failed，0 skipped**（137.092 秒）；
  既有 penaltyblog/NumPy deprecation warnings 未改变测试结果。
- `ruff check src tests examples` 通过；`ruff format --check src tests` 通过。
- 完整测试只读 `data/fixtures/`；新样本 122 条约 38 KB，附原始字段与测试变形说明。
- 源静态 DB、最终参数及原归档在验证期间哈希不变；最终配置只增加两个条目，文件内 gate 与原条目相等。

证据脚本 `select_parameters.py`、`compare_existing.py`、`daily_replay.py` 固定了本次路径、截点和环境变量。
复算前按源提交恢复静态库，在本次相同依赖版本中导出基线 `src`，用最终完整参数分别运行
`compare_existing.py before` / `after`；再逐文件比较，不能修改或删减 provenance 字段。
参数选择脚本复用 T15/T16 既有函数，SQL guard 保证不读赔率；生成时间随重跑变化，应先保存最终参数再做成对比较。
`daily_replay.py` 只适用于明确标注的独立历史回放副本，不能用于生产数据回写。
