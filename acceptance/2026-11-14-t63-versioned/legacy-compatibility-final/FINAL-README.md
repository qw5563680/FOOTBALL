# T63 最终兼容性验收

本轮最终结果位于 `final/acceptance.json`。`README.md` 和 `preparation-manifest.json` 保留等待源码冻结时的准备状态；此文件说明冻结后的实际执行，不覆盖准备记录或上一轮证据。

最终源码快照为 `a39dac36edd60c68c0fa475605edbb0d19996aea09e9d759d3afa0174a7eddd1`，共 167 个源码和参数文件，捕获前后文件一致。父 HEAD 为 `3bac25c5bcb25972f7559d6b658ea697b39fa32f`；实际执行的是包含未提交修改的独立 `current-code` 快照，不能只用父 HEAD 表示本轮代码。完整逐文件 SHA 见 `final-current-source-manifest.json`。

本轮复用 main `b83d2cc7d975e41f418f59aad3341a7d1574f95a` 已计算的 39 组基线拟合。79 个原始基线 JSON 已再次逐一核验，记录于 `baseline-reuse-sha256.json`，没有重新拟合同一基线。沿用同一 119 目标和截止时间、同一只读数据库及已验证 worker/比较器，具体绝对路径和 SHA 见准备清单，实际命令和运行环境见 `execution-manifest.json`。

最终比较全部通过：39/39 组训练 ID 和完整拟合数值文件字节相同；105/105 场 H/D/A 概率及全部 10×10 比分矩阵逐项相同；14 场无可信截止时间的记录和原因保持一致。105 场包括旧七联赛 51 场和欧战自然目标 54 场。七个旧联赛及欧战的完整参数条目都与基线相同，运行使用生产默认 read_history，不传来源选择 override。

`final/targets.json` 保存全部 119 行目标、概率、比分矩阵、模型数值版本、截止 UTC/BJT 及未生成原因；`final/targets.csv` 方便并排核验两版本概率。`final/fits.json` 和 `final/fits.csv` 保存全部 39 组读入、选行和拟合摘要，并指向原基线及本轮完整数值/训练 ID 文件。`current/summary.json` 是 worker 的原始输出，`comparison` 是原比较器的独立输出。

全列 DataFrame 摘要有 39 处差异，原因仍为新增 10 个身份和核验元数据字段。原有全部 11 列的值和顺序、attrs、实际训练行、完整拟合数值及预测元数据均一致。新旧 schema 的只读复核见 `current-schema.json`、原 `baseline-schema.json` 指针和 `final/schema-difference.json`。

新增 APIF retained 赛果通过 ResultSourceLink 关联 FD/JC 后的欧战行为，另由实现者的 8 个 fixture 证明：红阶段 7 失败、1 通过，绿阶段 8 全通过；覆盖同比分/冲突比分、FD/JC 对应关系、关系可知时刻、已移除旧锚点和过期关系。`future-import-fixture-evidence.json` 保存 fixture 与红绿日志/XML 的路径、SHA 和用例结果。固定归档比较只证明存量输入与数值兼容，不替代新增导入场景的 fixture。

数据库 SHA 始终为 `2cca04516fe2b9a7d9ba212ba95a33d523740b3bb8c583c1874fc970139a0ca4`。未复制数据库或 320 MB phase1 目录，未改旧证据，网络请求为 0；未计算赛果 Log Loss 或其他收益评分、未调参、未生成 P2。`final/evidence-sha256.json` 固定本轮文件哈希，复用外部文件另有明确指针与 SHA。
