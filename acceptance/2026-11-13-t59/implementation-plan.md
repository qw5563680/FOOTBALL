# T59 实现与验收计划

Spec: docs/tasks/T59-candidate-review-production-channel.md；main 77d4406；固定归档 e901c14。

- [x] 读最新规则/任务/实现，建立独立 codex/T59-candidate-review-production-channel。
- [x] 主通道：新 cloud_candidate_review 模块恢复临时库、复用原 apply，验证新旧库/队列哈希，只更新队列、新审计及清单；失败回滚，不重新导出其他资料。
- [x] 审计：state/audit/jc_candidate_reviews/<run> 下 plan/identity_inventory/review/operation.json 进入 export/restore，校验哈希、拒绝路径穿越/链接、清除旧快照之外的审计。
- [x] 工作流：10个既有输入不增；operation=candidate-review，options.plan 指向代码仓库已提交计划；独立 job，原并发写锁、原发布脚本，远端推进拒绝过期计划。只补 apis/sporttery 新操作排除条件，避免旧标签回退误启动采集；所有旧操作行为保持。
- [x] 手工 SFC：缺表按行 unmatched，14行快照完整，CSV仅输出实际match_id；身份冲突仍整批失败。
- [x] 偶发测试：查因；不可复现则记录，不加重试/跳过。
- [x] 从当前远端归档重新 inspect 首批5项，固定计划hash、明确4 distinct/湘南 unresolved，不拿旧副本直接发布。
- [x] 离线真实操作/错误hash/新候选/读回/导出恢复/历史日报，其他文件SHA不变；本地发布脚本以bare Git验证正确发布及远端竞态拒绝。
- [ ] 全量pytest、Ruff、内部代码复核、独立验收分支/小README/SHA，开草稿PR并等CI。
- [ ] 正式首次处置由审查者按任务单触发；实现者不伪装审查者、不合并、不代触发生产。

设计选择：不调用全量 export_feeds 做本次处置，以免无关资料重新序列化。仅审计持久化复用 export/restore 通用流程。
核验标准不改，工作流不使用任何体育API密钥。计划校验后远端变动时，要求重新 inspect，不将旧队列 rebase 到新数据。
验收入口：reports/t59-candidate-review-production-channel/README.md；详细记录放独立验收数据分支。
