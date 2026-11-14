# PR133 建议2/3：审计与候选状态可观测性

工作树 `/workspace/work/football-t63`。离线执行，无网络请求、无凭据、无提交推送。

## 审计CSV

仅输出比分/开球冲突与身份未核实的行；consistent 和 unmatched（含无候选与无赛果）只计入汇总。
`rows` 为实际 CSV 数据行数，新增 `comparison_rows` 为全部比较行数；匹配、身份、来源与分类规则未改。

真实输入由 `/workspace/work/t65-fixed-archive` 恢复到本目录 `e584-restored/data/zucai.db`，原归档只读。
`e584-audit.json` 保存所有计数和SHA256。旧输出由同一 `_audit_rows` 全量结果序列化，无规则替换。

| 项目 | 结果 |
| --- | ---: |
| 全部比较 | 28,624 |
| 旧CSV数据行 / bytes | 28,624 / 7,013,704 |
| 新CSV数据行 / bytes | 49 / 12,801 |
| matched / conflicts | 0 / 0 |
| unmatched / unverified | 28,575 / 49 |
| no_apif_candidate | 28,336 |
| no_comparison_candidate | 233 |
| apif_result_missing | 6 |
| counterpart_missing | 17 |
| temporary_team_identity | 32 |

审计前后数据库SHA256相同：`2cca04516fe2b9a7d9ba212ba95a33d523740b3bb8c583c1874fc970139a0ca4`。

## 候选状态

当前 run 新增 `source_version_status`，记录 complete / partial / failed、阶段、固定公开原因码、异常类别、失败联赛、记录时间和是否真实冻结附件。
成功/部分训练失败/全部训练失败与真实冻结的 source_versions 同一事务记录；外层训练/预测/冻结异常另记终态，不伪造附件。
异常文本不进入新状态元数据，避免将任意路径/URL/凭据复制到摘要；完整诊断仍在日志。
`source_version_files` 校验状态与实际附件、联赛结果一致，旧归档无新字段仍兼容；终态不可覆盖。
`prediction_source_lines` 同时服务 GITHUB_STEP_SUMMARY 与实际日报。

`fault-injection/` 保存使用既有 t16/t17 fixtures 的四次实际离线运行；每例含归档、摘要与日报，外层失败例另实际恢复到数据库。
冻结故障在真实 candidate 文件写出后的验证阶段注入，覆盖原附件回滚与失败状态落盘。三种外层失败均无candidate文件，partial有6份真实候选附件。
四例均断言原主发布文件逐字节保持；详细状态与主文件哈希见 `fault-injection-results.json`。

## 测试

先修改测试并观察失败：`audit-red.log`（5项预期失败）、`candidate-red.log`（4项缺少状态失败）、`status-red.log`（5项缺少记录/验证能力失败）。
`verification.log`：9个相关文件226项通过，涵盖候选、审计、归档、训练、定时预测、日报、发布时间与纸面版本边界。
`fault-injection.log`：4 passed；持久故障产物在最后一次注入运行后由 `collect_fault_evidence.py` 汇总。
8个改动文件 ruff check 与 ruff format --check 通过。全量集成测试日志见 `full-pytest.log`，最终结果由父任务汇总。
环境已有 penaltyblog / NumPy 2.5 DeprecationWarning；离线原实验 release 路径缺配置时沿原有行为记录失败，主发布仍保留。

## 改动文件

- `src/zucai/source_results_audit.py`
- `src/zucai/cloud_predict.py`
- `src/zucai/source_version_archive.py`
- `src/zucai/digest_views.py`
- `tests/test_t63_source_audit.py`
- `tests/test_cloud_predict.py`
- `tests/test_t63_source_version_archive.py`
- 新增 `tests/test_t63_candidate_observability.py`

未修改来源选择、拟合参数、旧主模型、旧冻结报告与旧账本。若底层磁盘或manifest不可写，状态写入只能记录日志，不能保证持久化；仍不会因此回滚主发布。
真实线上连续三次每日链路及每场双版本冻结仍需审查者执行，离线故障注入不替代线上验收。
