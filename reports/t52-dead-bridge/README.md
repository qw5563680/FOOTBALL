# T52：失效编号对照的离线修复验收

执行日期：2026-11-12。代码基线：`41395e5`；实现分支：
`codex/T52-dead-bridge-national-mapping`。本次只做隔离副本验收和草稿 PR，未发布正式数据。

## 修复及原因

26163 期前六场的 500 对照给出了竞彩 ID，但对应比赛没有进入竞彩比赛库。
在固定原档案的三次观察中，这六场均为 `sellStatus=2`，跳过原因为
`sell_status_not_1`；清单照存、非售卖比赛不入库，符合原采集规则。
编号对照来自另一条采集路径，本身不能证明数据库中存在对应比赛。

修复让“欧国联对照目标不存在”重新使用既有的国家队来源 ID、主客方向、联赛和
精确开球时间校验。保留原对照和 `bridge_fallback_reason=target_match_missing`。
目标真实存在但身份不对仍拒绝；对照自身的联赛/时间冲突也拒绝，后续采集不能悄悄替换原对照。
日报根据截止时刻已归档的清单/期次，区分空表与仍有比赛未展示，覆盖部分有预测的情况。
原期次快照的映射状态会明确标成“原始快照”，胜负彩专用概率仍在原有章节展示。

## 固定输入与可追溯位置

- 数据：[不可变提交 `1b8cc9c697b863d4c92b4e0006f2190d053e1fcb`](https://github.com/qw5563680/FOOTBALL/tree/1b8cc9c697b863d4c92b4e0006f2190d053e1fcb)，原运行 [43067904858](https://github.com/qw5563680/FOOTBALL/actions/runs/43067904858)。
- 期次：[原始 26163.json](https://github.com/qw5563680/FOOTBALL/blob/1b8cc9c697b863d4c92b4e0006f2190d053e1fcb/state/sfc/26163.json)。
- 原有八场：[原始 11-14 批次](https://github.com/qw5563680/FOOTBALL/blob/1b8cc9c697b863d4c92b4e0006f2190d053e1fcb/forecasts/2026-11-14/43067904858-1/forecast.json)。
- 原模型：同提交的 `models/43067904858-1/欧国联.pkl`；加载前检查 SHA256
  `7c4efd54a7c4063f0179a597c4145afef1e0be00f3b2b5ffe850a8f75e642836`。
- 历史截止：`2026-11-11T17:43:58.323712+00:00`（北京时间 11-12 01:43:58）；
  本次最终重放生成时刻：`2026-11-12T00:56:05.136289+00:00`，标记 `historical_replay`。
- [真实裁剪样本说明](../../data/fixtures/t52/README.md)、[全部来源哈希](../../data/fixtures/t52/provenance.json)、
  [可复跑验收脚本](../../scripts/verify-t52-archive.py)。参数文件哈希与原批次相同，未重新训练。

## 五条验收结果

同一全归档、同一历史截止及原模型，在基线 `41395e5` 上实跑得到 **8/14**：
前六场 `match_id` 均为空，北京 11-13 批次 `input.match_ids=[]`、预测 0 场。
记录见工作区 `base-41395e5-replay.json`；下面是修复后的结果。

| 任务单标准 | 实跑结果 | 核验方法 |
| --- | --- | --- |
| 1. 原件重放 14/14，前六场使用严格国家队依据 | **通过：14/14**；前六场均为 `national_team_ids_and_exact_kickoff` | 全归档恢复后执行 `refresh_snapshot`，逐行断言原桥接保留、目标确实不存在 |
| 2. 北京 11-13 批次包含六个 APIF ID | **通过：输入 6 场，实际模型预测 6 场** | 调用 `open_national_fixtures` 和真实 `predict_batch`；另一次 14 场批次全部 predicted |
| 3. 原八场 ID、模型概率逐字节不变 | **通过：8/8** | 原归档与实际模型重放的 ID/概率转成相同规范的 UTF-8 JSON 后直接比较；不四舍五入、不设容差 |
| 4. 指向真实错误比赛仍拒绝 | **通过** | 将第一场桥接改指全库确实存在的 `apif:1528968`，结果 `ambiguous`、`match_id=null`、`reason=conflicting_identity` |
| 5. 冻结历史 SHA256 不变，全量检查通过 | 输入档案 **2,177/2,177** 文件不变，恢复数据库也不变；全量检查结果见下文 | 遍历全部原件比较前后 SHA256；冻结分类包含 forecasts 453、sfc 165、daily 71、scoreboard 5 个文件 |

北京 11-13 批次实际输入：

```json
["apif:1528958", "apif:1528959", "apif:1528961", "apif:1528962", "apif:1528963", "apif:1545613"]
```

八场比较文件的共同 SHA256：
`fea67bdcb1f4b5de6d0c56ae905825df146d39857ddb750366843d84dc55cc4f`。
恢复后数据库包含 35,683 场比赛、35,040 条赛果、1,014 队；预测前后数据库 SHA256 均为
`daa369bac81ad639d37740e8d7aafd683c39f05146d070d54c08a861aa6bc37c`。

## 实跑逐场摘录

以下从 `acceptance-final/mapped-26163.json` 原值提取；14 行均为 matched，
证据基准均为 `national_team_ids_and_exact_kickoff`。主客名称保留500原始简称。

| 序号 | 主队 | 客队 | 修后 match_id | 本次变化 |
| --- | --- | --- | --- | --- |
| 1 | 亚美尼 | 塞浦路 | `apif:1528959` | 失效对照回退成功 |
| 2 | 土耳其 | 比利时 | `apif:1528958` | 失效对照回退成功 |
| 3 | 阿尔巴 | 芬兰 | `apif:1528963` | 失效对照回退成功 |
| 4 | 捷克 | 西班牙 | `apif:1528961` | 失效对照回退成功 |
| 5 | 英格兰 | 克罗地 | `apif:1528962` | 失效对照回退成功 |
| 6 | 黑山 | 拉脱维 | `apif:1545613` | 失效对照回退成功 |
| 7 | 摩尔多 | 哈萨克 | `apif:1528968` | 原ID保持不变 |
| 8 | 保加利 | 冰岛 | `apif:1528969` | 原ID保持不变 |
| 9 | 卢森堡 | 爱沙尼 | `apif:1545614` | 原ID保持不变 |
| 10 | 荷兰 | 希腊 | `apif:1528966` | 原ID保持不变 |
| 11 | 苏格兰 | 北马其顿 | `apif:1545659` | 原ID保持不变 |
| 12 | 塞尔维 | 德国 | `apif:1528965` | 原ID保持不变 |
| 13 | 斯洛文 | 瑞士 | `apif:1545654` | 原ID保持不变 |
| 14 | 斯洛伐 | 法罗 | `apif:1528967` | 原ID保持不变 |

验收程序的原始摘要：

```json
{
  "actual_replay_generated_utc": "2026-11-12T00:56:05.136289+00:00",
  "all14_predicted": 14,
  "archive_commit": "1b8cc9c697b863d4c92b4e0006f2190d053e1fcb",
  "archive_files_unchanged": 2177,
  "eight_probability_sha256": "fea67bdcb1f4b5de6d0c56ae905825df146d39857ddb750366843d84dc55cc4f",
  "frozen_file_categories": {
    "daily": 71,
    "forecasts": 453,
    "scoreboard": 5,
    "sfc": 165
  },
  "historical_as_of_utc": "2026-11-11T17:43:58.323712+00:00",
  "issue_snapshot_unchanged": true,
  "mapped": 14,
  "mode": "historical_replay",
  "model_artifact": "models/43067904858-1/欧国联.pkl",
  "model_sha256": "7c4efd54a7c4063f0179a597c4145afef1e0be00f3b2b5ffe850a8f75e642836",
  "note": "五条标准中的全量pytest/ruff由单独检查日志证明；本文件证明真实归档重放、概率逐字节比较、冲突拒绝和全部原档案SHA不变。",
  "nov13_input_match_ids": [
    "apif:1528958",
    "apif:1528959",
    "apif:1528961",
    "apif:1528962",
    "apif:1528963",
    "apif:1545613"
  ],
  "nov13_predicted": 6,
  "original_eight_byte_equal": true,
  "params_sha256": "5482a55708eeb6b9369c3f6c7ea638927288b959809801f81c1ea56acd4d3f0d",
  "probability_comparison": "UTF-8 canonical JSON of exact original match_id/model_probabilities versus actual model replay; no rounding or tolerance",
  "production_written": false,
  "restored_counts": {
    "closing_odds": 100030,
    "matches": 35683,
    "odds": 14349,
    "results": 35040,
    "teams": 1014
  },
  "restored_database_sha256_unchanged": "daa369bac81ad639d37740e8d7aafd683c39f05146d070d54c08a861aa6bc37c",
  "total": 14,
  "wrong_bridge_candidate": "apif:1528968",
  "wrong_existing_bridge_rejected": true
}
```

## 检查命令与记录

所有测试只读本地 fixture/已保存原档案，不联网取体育数据，不使用正式 API key。
本次解释器为 `/workspace/work/t48-venv/bin/python`，显式设置 `PYTHONPATH=src` 指向 T52 worktree。

```bash
PYTHONPATH=src python scripts/verify-t52-archive.py \
  --archive /workspace/work/t52-evidence/archive \
  --output /workspace/work/t52-evidence/acceptance-final
PYTHONPATH=src python -m pytest -o addopts='' -q
ruff check src tests examples scripts/verify-t52-archive.py
ruff format --check src tests scripts/verify-t52-archive.py
```

实跑输出（所有命令退出码为 0）：

```text
pytest: 2582 passed, 6341 warnings in 125.00s (0:02:04)
ruff check: All checks passed!
ruff format --check: 284 files already formatted
从 Git 暂存区重新导出代码及 fixture: 30 项 T52 回归测试通过
```

pytest 警告来自现有 penaltyblog/NumPy 弃用提示。本次未改依赖或模型来消除警告。
对原始 `26163.json` 设置专门的 `.gitattributes -text`，防止 Git 换行转换破坏原件 SHA256；
全部真实 fixture 的暂存 blob 哈希与来源清单一致，并从暂存区导出副本再次运行 T52 测试。
新增 30 项测试覆盖严格匹配、真实归档、日报空表/部分覆盖与截止时间；
修前 RED 记录和修后 GREEN 记录一并保留。另一个只读代理已重新执行真实模型验收、全量测试并检查全部 diff，
发现的原桥接权威性保留、部分覆盖提示两处问题均已修正，未留必须修项。

实际原归档还单独生成了一份临时日报（`digest-actual/2026-11-12.md`）：11-13 小节显示
“竞彩目录有 6 场未开球比赛，但本批未提供对应竞彩预测”及
“原始胜负彩期次快照有 6 场未开球比赛（26163 期：未映射 6 场、已映射 0 场）”，
不再显示“该批次无未开球比赛”。这是对保留的**旧冻结批次**的准确描述，
没有把离线新增的六场预测冒充已经发布。生成后全部 2,177 个输入哈希仍相同。
该解压副本没有 `.git`，既有 SFC 展示逻辑提示无法确认存好时间；本次不据此声称发布验收通过。

完整逐场输出与日志保存在本次工作区 `/workspace/work/t52-evidence/`；
`acceptance-final/` 是实现验收，`reviewer-acceptance/` 是独立复跑。
输入原件及模型长期保留在上方不可变数据提交，代码 PR 不重复上传大文件。
验收产物、实现文件及日志的 SHA256 见同目录 `SHA256SUMS`；复核者可从固定输入重新生成全部逐场结果。
清单中 `repository/` 代表本 PR 的代码根目录，`evidence/` 代表隔离验收产物；
二者已整理为本次工作区 `/workspace/work/T52-acceptance-evidence/` 交接副本。
这些本地日志不属于生产归档，正式审查仍以固定原件、提交代码和独立复跑为准。

## 边界

这是隔离的历史资料重放，不代表正式线上已经部署或发布了新增预测。
没有修改模型、参数、BET、身份校验要求；没有新别名、模糊匹配或市场数据入模。
没有回写任何已冻结预测、日报、胜负彩报告或账本，也没有触发生产运行。
后续由仓库既定审查流程审查草稿 PR，仓库所有者决定是否合并。
