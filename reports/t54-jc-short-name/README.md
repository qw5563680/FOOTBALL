# T54：竞彩缩写重复身份修复验收

本次只修复任务单中的竞彩身份路径，在独立数据库副本验收。正式数据、冻结预测、日报和账本均未改动，未调用体育接口、未使用正式 key、未触发生产运行。

## 固定版本与复现

- 代码基线：`dbee34e355ef4fc14320d1a6d2f65565f9f42a8d`；实现版本见本文件所在 T54 提交，逐文件代码哈希和最终提交号另存完整证据的 `implementation-version.json`。
- [原始数据快照](https://github.com/qw5563680/FOOTBALL/tree/b492f4d7c501fd8cdbd2694d760f2f13892f796c)：`data/football-feeds@b492f4d7c501fd8cdbd2694d760f2f13892f796c`。
- 原期次：26163；[冻结批次](https://github.com/qw5563680/FOOTBALL/blob/b492f4d7c501fd8cdbd2694d760f2f13892f796c/sfc/26163/history/43198864630-1/sfc.json)：`43198864630-1`。
- 原预测截止：UTC `2026-11-12T10:33:56.067019+00:00`（北京时间 18:33:56）。本次实际重放：UTC `2026-11-12T14:46:33.671905+00:00`（北京时间 22:46:33），标记为历史重放。
- 原模型：`models/43198864630-1/欧国联.pkl`，SHA256 `e7e9c5c882c9e510b5cbda7bd32f1e0700056dd3668f806d6d1010eda2fe1365`。
- 参数 SHA256：`5482a55708eeb6b9369c3f6c7ea638927288b959809801f81c1ea56acd4d3f0d`。

[完整证据与检查日志](https://github.com/qw5563680/FOOTBALL/tree/data/t54-jc-short-name-acceptance-20261112/acceptance/2026-11-12-t54)在独立资料分支；[汇总 JSON](https://github.com/qw5563680/FOOTBALL/blob/data/t54-jc-short-name-acceptance-20261112/acceptance/2026-11-12-t54/summary.json)、[修复审计](https://github.com/qw5563680/FOOTBALL/blob/data/t54-jc-short-name-acceptance-20261112/acceptance/2026-11-12-t54/audits/jc_identity_merges.csv)及本目录 `SHA256SUMS` 可逐项复核。主分支仅存本说明和哈希清单。

```bash
# 将上述固定数据提交完整解压到 /path/to/fixed-archive；输出目录必须不存在。
PYTHONPATH=src python scripts/verify-t54-archive.py \
  --archive /path/to/fixed-archive --output /path/to/new-output
pytest -q
ruff check src tests
ruff format --check src tests
```

本地使用 Python 3.12.14，显式 `PYTHONPATH=src` 指向本分支；pytest 附加 `--junitxml` 保存计数。原件哈希及裁剪说明见 `data/fixtures/t54/provenance.json`。全量模型不放入单元测试，测试不联网。

## 四条验收结果

| 任务标准 | 实跑结果 | 可复查证据 |
| --- | --- | --- |
| 1. 原快照重放 14/14，第 3 场真实身份，其余 13 场不变 | 原来 13/14，第 3 场 `conflicting_identity`；修复后 14/14。`jc:2042694` 主队由 1156 绑定到 1149，与 `apif:1528963` 的主客、赛事、精确开球一致，并记录 counterpart。独立报告保留原 13 场完整预测；同环境修复前后实际重算的 13 场也逐字节相同 | `baseline-26163.json`、`mapped-26163.json`、`reconciliation.json`、`isolated-replay-sfc-report.json`、两组 thirteen 字节文件 |
| 2. 未批准缩写不自动并队 | 人工构造未知缩写，采集后独立建队并写 `state/alias_candidates/jc.json`；已有长队名不被前缀匹配。赛程和官方赛果均覆盖。待审写入失败或数据库提交失败整体回滚 | `checks/ingest-red.txt`、`checks/ingest-regressions-final.txt`；15 项 T54 输入测试、86 项相关测试通过 |
| 3. 七支待核球队逐一登记 | 7/7 有来源、原始行、创建提交与结论。Russia 源名已核验；规范身份及是否重复均保守登记待核，未擅自合并 | `team-investigation.json`、`team-investigation.md`，下表 |
| 4. 冻结历史 SHA256 不变，全量检查通过 | 全部 2369 个源文件哈希不变；包含 forecasts 510、sfc 189、daily 80、scoreboard 5、models 1192 个文件。副本非目标比赛、其他数据表和原期次快照不变。pytest 2728 项通过、0 失败、0 跳过；Ruff 及格式检查通过（297 文件） | `archive-before-sha256.json` / `archive-after-sha256.json`、`summary.json`、`checks/final-pytest.xml` / `.log`、`checks/ruff-check.log`、`checks/ruff-format.log` |

定向修复重复执行、冲突拒绝、审计回滚和既有关联回归另有 160 项通过；手动 `reconcile` 入口、导出恢复及旧快照恢复清理有 17 项相关测试通过。新候选文件纳入 feeds 清单及 SHA 校验，篡改后拒绝恢复且不破坏已有库。

## 关于概率逐字节对照

第 3 场新计算的主/平/客概率为 `0.2152155012489874 / 0.3430383230350625 / 0.4417461757159501`。旧批次该场为身份错误导致的 excluded，不能冒充旧批次已经预测成功。

两项严格比较均通过，不采用四舍五入或浮点容差：

1. 原冻结报告的其余 13 场与独立重放报告比较 `seq/match_id/probabilities`，SHA256 均为 `c1e8779c5771c19159fe1088f5e34dd6e790e246ea440e566bf8738b527dbdf3`；13 场完整冻结预测原样复用，只补算第 3 场。
2. 同一当前环境、原模型、原截止，在校正前后分别实际计算其余 13 场，SHA256 均为 `186aa827df3e19148260e2baf9c989897587dd82a73fc21b70ebfd7267275c8f`。这证明本次身份修改没有改变其他场次计算。

同时保留全 14 场重新计算的诊断，**不声称跨环境全量重算与原冻结数字逐字节相同**：`jc:2042697` 和 `jc:2042696` 存在约 `1e-16` 的浮点末位差异，修复前已经如此。例如前者客胜 `0.8098960421885126 → 0.8098960421885125`，后者主胜 `0.8238339434795725 → 0.8238339434795726`。全部原值、重算值和运行时版本见 `archived-versus-recomputed-diagnostic.json`；没有调整模型、参数或原冻结记录消除差异。

## 七队登记与范围限制

| 库内编号 / 名称 | 归档来源 | 结论 |
| --- | --- | --- |
| 1153 / `apif:team:4` | APIF 欧国联 ID 4，审计原名 Russia，16 场历史记录 | 源名 Russia 已核验；规范中文名和跨来源身份待核 |
| 1154 / 布罗姆利 | `jc:2042685` | 待核；只有竞彩原名及比赛，不能认定是全新实体 |
| 1155 / 伯顿 | `jc:2042684` | 待核；缺少批准的同队映射 |
| 1157 / 巴恩斯利 | `jc:2042691` | 待核；`551/Barnsley` 仅是复核线索，未按译名合并 |
| 1158 / 奥德汉姆 | `jc:2042691` | 待核；缺少批准的同队映射 |
| 1159 / 谢周三 | `jc:2042690` | 待核；`562/Sheffield Weds` 仅是复核线索，未按译名合并 |
| 1160 / 阿克宁顿 | `jc:2042690` | 待核；缺少批准的同队映射 |

任务单“同批新建”描述与原档不完全一致：1153 首次归档于 11 月 8 日，1154/1155 于 11 月 11 日，1157—1160 于 11 月 12 日；六支 JC 俱乐部首次归档的比赛均已结束。登记保留实际创建时间，不改任务单或旧记录。

本轮采用任务允许的“等价绑定”方案：只校正已获批的 `jc:2042694`，保留 1156 旧 Team 行，未批量改名。未来新采集先查明确别名；现有正式数据仍须审查者合并后通过手动 `reconcile` 路径校正并独立验收。这里的结果仅证明本分支离线实现，不声称生产已修好。
