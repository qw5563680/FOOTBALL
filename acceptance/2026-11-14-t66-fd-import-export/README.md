# T66 离线实跑证据

代码版本见 delivery.json。证据追加在独立 data/t66-fd-import-export-acceptance-20261114 分支，不改变 data/football-feeds 或生产数据。

- baseline/：旧 main d6a52bbb 的全量测试、52 条原有关联清单和两种根因复现（SQL 赋值及真实 FD 样本完整导入后导出失败）。
- links/、schedule/：先失败后修复的日志；中间测试断言修正也保留，最终日志带 final 或 green。
- acceptance/：e5848e5 完整恢复副本、9 份 11-04 原件逐场导入、重复导入及导出恢复；52 条关联不改，4 场新 FD = 4 行新增训练读取，2393 个旧冻结文件不变。
- inputs/：实际使用的 9 份原件、sidecar 和选择清单；所有字节与原哈希一致，未改真实采集时间。
- internal-review/：Codex 内部独立复查与复现脚本，不是 Claude 正式合并审批。corrected-stats-green.json 的 HS=25 是明示的合成纠错测试，不是新增真实体育数据。
- verification/：3331 项全量 pytest、ruff、格式检查、受测343文件字节对照；全部0失败。完整 XML/日志保留，基线3247项。

## 复查方法

1. 检出 delivery.json 指定的代码版本及固定数据 e5848e5；安装项目既有开发依赖。
2. 在本证据目录执行 `sha256sum -c SHA256SUMS`，然后在代码目录执行 `pytest -q`、`ruff check src tests`、`ruff format --check src tests`。抓取测试全部走代码仓库 data/fixtures。
3. acceptance.py / reproduce.py 保留实跑原脚本及原绝对路径。跨机器重放只调整 ROOT（代码）、ARCHIVE（e5848e5只读副本）、OUT（新证据目录）、INPUTS（本包inputs/selected-raw-manifest.json）和临时目录位置；将清单的path/sidecar_path映射到bundled_path/bundled_sidecar_path。不要把OUT或SQLite指向正式目录，不覆盖本批证据。
4. reproduce.py 必须在旧代码 d6a52bbb 下执行，新的DB路径须不存在；目标是两个相同错误。acceptance.py 在修复代码下执行，临时副本自动清理；所有HTTP均被responses拦截。
5. 失败运行43543648051附件仅核对元数据，未下载内容。11-04原件不能证明线上已推进到11-09以后。合并后由审查者按任务单重新FD回填、成功发布，再检查一次正常每日链路和15联赛训练增量。

源码没有模型或参数改动，未拟合模型，未修改原预测/日报/账本。新加入的4行只表示当前副本可读训练资料，不声称历史赛前已用过。9份整季原件中的23条原有疑似重复继续隔离，没有为了计数放宽身份校验。
