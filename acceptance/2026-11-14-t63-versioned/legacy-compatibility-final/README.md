# T63 最终兼容性验收：准备状态

本目录独立于上一轮 `legacy-compatibility`，不会修改或覆盖旧证据。目前仅准备输入和命令，等待 root 明确通知产品源码冻结；尚未捕获新源码、拟合模型或生成概率。

`preparation-manifest.json` 固定脚本、目标、只读数据库和旧证据的绝对路径与 SHA。复用 `b83d2cc7d975e41f418f59aad3341a7d1574f95a` 基线已有的 39 组拟合，79 个原始 JSON 文件逐一核验，清单见 `baseline-reuse-sha256.json`。无需重复拟合同一基线，也不复制原数据库或 phase1 大目录。

`planned-commands.json` 保留既有已验证 worker 和比较器的调用参数。源码冻结后，在本目录新建 `current-code` 小型源码/参数快照及 `final-current-source-manifest.json`，将准确快照标识填入 worker 的 `--code-sha`，再运行 `current` 和 `comparison` 新输出目录。使用生产默认来源路径，不传来源选择 override。

目标保持原有 119 行：旧七联赛 65 行（51 可预测、14 无可信截止）及欧战自然目标 54 行。截止不重选，使用同一只读数据库，检查 39 组完整拟合数值/训练 ID、105 场概率和全部 10×10 比分矩阵是否与基线精确一致。此验收不计算 Log Loss 或其他赛果评分，不调整参数，不生成 P2。

新增 APIF retained 赛果经 `ResultSourceLink` 关联 FD/JC 后的 EU legacy 行为，需要独立 future-import fixture 证据。固定归档的兼容性运行不能替代该场景；冻结后须另记实现 agent 的 fixture 路径、代码版本及结果。该证据当前待交付，不预先标记通过。
