# T58 胜负彩队名对照及小修验收

已实现四项任务。固定 26164 期的匹配从 9 场增至 12 场；只改变第 2、6、10 行。
旧冻结批次没有这三场预测，仍如实缺概率，没有重算或回写旧预测。
首批竞彩队名在副本核验后从 5 个待核变为 1 个；湘南海洋继续保留疑点。

## 固定输入与证据

- 代码基线：`54c36eed139b7eb2c2883c57d5a3b37160c097ee`。
- 数据基线：`data/football-feeds@e901c14b2c3aac1496832cc98cb2ab7e3bd7aeb2`。
- 验收时间：2026-11-13，北京时间；每份结果同时保存实际 UTC 和北京时间。
- 期次原件：`state/sfc/26164.json`；裁剪的原始比赛、球队行及哈希在 `data/fixtures/t58/`。
- [完整证据、日志、逐场新旧报告、队名处置与环境记录](https://github.com/qw5563680/FOOTBALL/tree/data/t58-sfc-name-table-acceptance-20261113/acceptance/2026-11-13-t58)。
- 证据目录的 `implementation-version.json` 绑定实现提交及源码哈希；本目录 `SHA256SUMS`
  中 `evidence/` 相对此目录，`repository/` 相对代码仓库根目录。

## 逐项实跑结果

| 验收项 | 结果 | 详细证据（上述证据目录内） |
| --- | --- | --- |
| 对照来源与机制 | 采集状态不是队名表；采用精确编号核准别名，14 条逐项关联固定期次、APIF 比赛和国家队身份。只限欧国联；无证据国家不猜补。 | `data/team_aliases/sfc.json`（代码仓库）；`sfc-archive/summary.json` |
| 真实三场恢复 | 第 2 行→`apif:1528973`，第 6 行→`apif:1528977`，第 10 行→`apif:1528981`；其他 11 行逐字段一致。 | `sfc-archive/mapped-after.json` |
| 错误身份拒绝 | 三场各测主客反转、时间加一分钟、未知编号附完整队名，共 9 例全部拒绝。 | `sfc-archive/rejected-identities.json` |
| 冻结保护 | 2,655 份原归档文件 SHA256 前后完全一致；18 份 26164 旧 Markdown/CSV 单列核对；恢复副本数据库 SHA256 不变。 | `sfc-archive/archive-before-sha256.json`、`archive-after-sha256.json`、`restored-database-sha256.json` |
| 未映射原因 | 缺表诊断展示原 5 行原因；补表后只剩第 1 行友谊赛/占位时间、第 14 行待桥接。没有开售证据，不宣称“未开售”。未确认发布时仍隐藏概率。CSV 不增列。 | `sfc-archive/before/report.md`、`after/report.md`；`display/pytest-focused.txt` |
| 16 项环境漂移 | 全新 Python 3.11 真实复现 16 失败 / 18 项；修复后旧 3.12、新 3.12、新 3.11 各 19 项通过。只放开汇总浮点绝对误差至 `1e-14`，原类型/结构/计数及账本/评分字节断言保留。 | `float-tests/README.md`、`test-results.json`、`fresh311-before.xml` |
| 首批候选核验 | 哥伦比亚、巴拉圭、新加坡、阿根廷仅按固定库内证据 cleared；湘南海洋（1165）与 Shonan Bellmare（607）疑似重复，仍 pending。未自动合并。 | `candidates/first-five-review/review.json`、`supporting-evidence.json` |
| 队列后续可用 | 新日报待核 5→1，核验前历史截点仍 5；再次处理原 4 场比赛新增待核 0；导出再恢复队列字节一致、身份清单一致；929 份副本冻结文件不变。 | `candidates/summary.json`、`digest-before/`、`digest-after/` |

缺表诊断在已恢复库中仅临时屏蔽 77、55、26、37、157 五个新增编号，重放同一期次，
用于展示原五行的原因；不修改代码表或原归档。新旧报告均复用原四份已冻结 forecast，
数据截止仍为 `2026-11-13T10:14:52.898118+00:00`（北京时间 18:14:52.898118）。
实际执行时刻另列，历史报告重放不冒充当时已经有新增预测。

## 复跑

```bash
PYTHONPATH=src python scripts/verify-t58-sfc-archive.py \
  --archive /path/to/e901c14-archive --output /path/to/new-sfc-evidence
PYTHONPATH=src python /path/to/evidence/candidates/replay_candidates.py \
  --archive /path/to/e901c14-archive \
  --plan /path/to/evidence/candidates/first-five-plan.json --out /path/to/new-candidate-evidence
PYTHONPATH=src pytest -q
ruff check src tests examples
ruff format --check src tests
```

本地全量测试：现有 Python 3.12 的 2,844 项全部通过（161.240 秒），全新解析依赖的
Python 3.11 的 2,844 项全部通过（168.683 秒）；均为 0 失败、0 错误、0 跳过。
`ruff check src tests examples` 和 `ruff format --check src tests` 通过（313 文件）。
日志/JUnit 在 `full/`；GitHub CI 以 PR Checks 与证据目录 `ci.json` 的实际运行结果为准。
本目录 SHA 清单绑定本地验收文件和源码；版本、CI 等交付元信息另由证据目录总 SHA 清单绑定。
大体积原件不进入代码分支；候选工具用法及计划格式见根 README。

## 影响与遗留

只改变上述三场的精确匹配、未映射原因展示、队列处置和测试比较方式。模型、参数、
BET、工作流及旧冻结预测/报告/评分/账本不变；不调用体育接口、使用正式 key 或写正式数据。
代码合并后由后续正常运行生成新批次；本次不重跑模型、不补写旧三场概率。
湘南海洋还缺 T54 要求的准确同场主客配对与已核准 APIF 身份证据，保持待核。
分支提交草稿 PR，不合并；球队匹配改动仍须 Claude Code 正式复核与所有者决定。
