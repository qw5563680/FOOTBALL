# T38 · PR #72 审查修复

非购买建议 · BET 禁用。本轮只修代码、协议与离线测试，并用同一批已有原件衡量存储大小；没有重跑真实数据模型实验。

## 先改协议，再改实现

协议 v2 提交 `ef379df` 在实现 `530eb6be5729bdf3481a9246f8e35ef4861a8dfa` 之前，原 v1 候选结果全部为空；这是所有者批准的新增有效结果前修正。
strict_capture_time 与 backfill_replay 两表独立选窗、拟合、六列对照、配对区间及子集分析，路径与输出写明口径。
replay 表明确假设「最近归档名单在开球前已知」，技术统计用开球+3h假设；不充作严格赛前或前瞻证据，也不填补 strict 空缺。
主候选只用 missing_diff，消融去掉该项；missing_last_starters_diff 保留真实空值，仅在双方阵容完整子集附加比较。
旧结果见 [v1 记录](../2026-11-05-t38-injuries/README.md)，本轮没有覆盖旧概率、原始结果或附件。

## 同一 11 页的离线存储回放

输入仍是已有 run 41877829928、artifact 13346833909（ZIP SHA256 fc26815f7eb97eae7e14cf514900f7968dc6f90c5bd7b3cadea9f3e3fd1754ac），
不重新请求 API。原 da17cbc 恢复库只复制到新本地目录 `work/t38-revision-data/`，原库未改动。

| 项目 | 修复后实际结果 |
| --- | --- |
| 输入 | 11 页、16,585 条源记录 |
| git 跟踪 CSV | **12,720 字节，11 条数据行，含表头 12 行** |
| 审查前 CSV | 审查者记录 11,986,168 字节、12,438 行 |
| 目标 ≤300 KB/天 | 本批通过；不是对未来任意输入规模的保证 |
| 完整压缩回执 | 11 个 gzip、共 131,518 字节；随 stats-raw Release 发布，不进 git |
| match_injuries | **11,362 行**；captured 222 / backfilled 11,140；已关联 4,021（35.39%） |
| 完全重复回放 | 新增 0 行；数据库、CSV、压缩回执字节均不变 |
| 同内容跨日 | fixtures 验证只延长 last_captured_utc，不新增版本 |

表的版本键改为 provider/fixture/record_key/version，保留首次 captured_utc 及原件来源；内容变化（包括 A→B→A）保留新版本。
首末区间只是观察边界，不假设中途名单持续不变。快照成员按每次真实捕获保存，删除/空页仍可还原，不能拼接不同日名单。
无索引联赛只向 CSV 写每页计数，逐条未关联原因保存在压缩附件与原记录中，不丢失 null 身份或擅自创建球队。
technical_data.injury_receipts 描述压缩文件路径/大小/哈希；restore-rows 先校验压缩文件及 CSV 捕获时间/原件哈希绑定再替换库。
发布历史核验只允许 last_captured_utc 单调延长，其余内容和来源不能改写。旧未部署 T38 草稿表显式要求从 v1/v2 feeds 恢复并回放，
不声称兼容已经改过列的草稿库；生产既有 v1/v2 格式保持支持。

## 离线验证

- pytest -q：**1942 passed**，0 failed/error/skipped，155.822 秒；环境 anyio 弃用提示 1 条。
- ruff check src tests examples、ruff format --check src tests（233 files）、actionlint、git diff --check：通过。
- 只用 data/fixtures 测试；真实旧原件仅用于上面的明确授权存储大小回放，不用于测试或模型重新评估。
- 新增/更新：内容版本/跨日复用/乱序观察/首末边界/事务回滚；每页摘要与 gzip 校验、坏附件拒绝恢复、外层 CSV 时间与附件绑定；
  Release mock 往返、首末区间更新允许发布、篡改内容拒绝发布、空页回执未发布不能导出；双目标口径分别选择/汇总且混算拒绝；
  fixture strict 主候选 n=0 而 replay n=4，证明未互填；首发项 null 不阻断主候选且保留缺失原因。
- 正式 Dixon-Coles、T33 统计/模型、params.json、P2、BET、T28、工作流均未改动；没有使用正式 key、触发 Actions 或写非 codex 分支。

## 待 Claude 接续

真实实验等待合入及已批准的 44 次历史回填后安排。strict/replay 真实指标本轮均未重跑，不拿 fixture 数字当精度结论。
合入避开定时任务在北京时间 10:40、17:40 开始后的约 75 分钟，防止旧代码作业覆盖 v3 清单。
本地验证包 `t38-review-fixes-validation-20261105.zip`，176009 字节，SHA256 `debae09fe3103dc2a05dabf78dab63719b20c507fe0a1d8d5a4f69f48ae9aee6`；含计数、测试日志、同批次小 CSV 与压缩回执。
当前环境仍不能上传 Release 二进制资产，因此包未上传，PR 保持草稿；真实产物由 Claude 按 experiments-YYYYMM 归档，不写数据分支。
main 本目录只保存 README 与 SHA256SUMS。
