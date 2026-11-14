# T63 固定归档验收证据

代码 d891f3b；基线 b83d2cc；固定数据 e5848e5；协议 b19dc7c 已经 Claude 审批。

- 最终实验汇总和 CSV：experiment/paired-v1-final/；原概率、模型与逐场训练原件：experiment/paired-v1/。
- 概率实验原汇总中的“至少一方上一场在训练”漏计已在 final 目录更正，未修改原概率或重新拟合；请使用 final 汇总。
- 模型兼容性：model-compatibility/comparison.json；baseline、current-cutoff-final、post-reverification-cutoff-final 三组。
- 规范化记录重新核验、导出/恢复：normalized-integration/。这不是 HTTP 原件重放。export-manifest-only.json 仅为导出索引，不是完整可独立恢复的归档。重现时从 e5848e5 原档按 verify_archive_copy.py 生成新副本。
- pytest-cutoff-final.xml/log：3421 项通过；原件与生产文件未修改。

本证据未包含临时数据库、密码或访问密钥。输入原档仍在固定数据提交；预测结果只属离线实验，未发布生产。
