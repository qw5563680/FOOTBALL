# T53：默认实力披露及候选排除验收

执行日期：2026-11-12（北京时间）。基线为 `eb335c7bb30517b70d8ed90c59d1b3fe0bbf8aee`，
分支 `codex/T53-unknown-team-prior-disclosure`。本次只在隔离副本实现与检查，提交草稿 PR。

## 改动说明

未知球队的先验公式保持原样。新预测只新增 `home_unknown_team` / `away_unknown_team` 两项元数据；
单场、日报、14场、任九、二串一均用统一只读规则说明缺失一方及先验方法。
旧欧战原件有双方训练赛事记录时可据此确定侧别；只有总标记则写“缺失方未记录”。
没有历史标记的记录归“未记录”层，不冒称双方已知。

原研究概率保留；默认实力场次不进入任九模型单选、14场置信排序、二串一 F 主组任一腿及其最高排序。
旧账户中仅新批次 D/E 的排名候选增加该项排除，A/B/C 规则保持原样。
旧 F 组合当前展示可以被过滤，但不重新配组、不重写旧 F 账本；市场对照仍独立。
记分板和复盘分为默认实力、双方已知、未记录三层。旧评分/结算证据仅在内存按原件身份与哈希关联，
原总分、门槛、冻结行及旧决定字段不重写。

## 固定原件

- [固定数据提交 `1b8cc9c697b863d4c92b4e0006f2190d053e1fcb`](https://github.com/qw5563680/FOOTBALL/tree/1b8cc9c697b863d4c92b4e0006f2190d053e1fcb)。
- [历史原预测](https://github.com/qw5563680/FOOTBALL/blob/1b8cc9c697b863d4c92b4e0006f2190d053e1fcb/forecasts/2026-11-06/42024862563-1/forecast.json)，原运行 [42024862563](https://github.com/qw5563680/FOOTBALL/actions/runs/42024862563)。
- 原模型：同提交 `models/42024862563-1/欧战.pkl`，加载前核对 SHA256
  `9d09bd903a6089434403585566fd709dd488a14af568ef3034d262dc35644274`。
- [真实裁剪样本](../../data/fixtures/t53/README.md)、[完整来源哈希](../../data/fixtures/t53/provenance.json)、
  [独立重放脚本](../../scripts/verify-t53-archive.py)。开发测试不请求体育接口、不使用正式 key。

## 三条验收标准

| 标准 | 实跑证据 | 结果 |
| --- | --- | --- |
| 1. `jc:2042523`、双方已知、双方均未知侧别正确 | 原归档/原模型核对真实主未知、客未知、已知三条；另用明确合成的两个未见名称实际调用同一原模型 | 全部正确；费伦茨为未知客队，双方未知同时标主客 |
| 2. 默认实力不进主候选，已知概率与资格不变 | 本地导出基线源码，独立子进程运行同一原模型与同一组准入输入，直接比较规范 JSON 字节；另对真实26159原批次做前后报告对照 | 三条真实模型概率前后完全一致；六场已知资格/概率/三个选组一致；默认实力退出主候选及最高排序，研究概率保留 |
| 3. 旧预测/账本 SHA256 不变，全量检查通过 | 全档案前后哈希、数据库哈希、旧结算写入回归、全量 pytest/Ruff | 2,177 个原档案文件及恢复数据库不变；完整测试结果见下文 |

### 侧别实跑

| 输入 | 性质 | 正确标注 | 冻结概率处理 |
| --- | --- | --- | --- |
| `jc:2042519` 水晶宫—霍芬海姆 | 真实历史，双方已知 | 主、客均不使用默认实力 | 原概率保留；原模型与历史值也逐字节相同 |
| `jc:2042520` 布斯巴达—伯恩茅斯 | 真实历史，主队未知 | 主队布斯巴达使用默认实力 | 原概率保留，仅增加披露及候选排除 |
| `jc:2042523` AC米兰—费伦茨 | 真实历史，客队未知 | 客队费伦茨使用默认实力 | 原主胜 `0.9629169602327318` 仍按96.29%展示为研究概率 |
| `synthetic:t53-both-unknown` | 明确合成输入，实际载入原模型 | 两个未见队名均标为默认实力 | 不冒充历史比赛或当时赛前发布 |

历史 `jc:2042442` 只有默认实力总标记、没有分队训练证据，仍退出候选，说明“缺失方未记录”，不猜哪一方。
四条默认实力历史预测均来自固定归档；未找到历史双方均未知案例，因此该边界单独标成合成测试。

### 前后比较与候选

同环境、同原模型，基线与 T53 对三条真实输入的概率逐字节相同，没有设置浮点容差。
其中两个未知队案例与较早的历史冻结值存在基线就有的约 `1e-16` 浮点差，
例如 `jc:2042523` 主胜重新计算为 `0.9629169602327319`，而冻结值为 `0.9629169602327318`。
T53 前后该差完全相同；没有据此修改模型、舍入结果或替换历史值，展示/筛选始终使用原冻结概率。

六场双方已知的二串一准入控制，基线与新版的概率、资格及三个选组规范 JSON 字节完全相同，
共同 SHA256：`9ab343560f092cdfc594342cc134d31cbc9c747c26bf6e5cb5981b6e327a2139`。
仅给其中一场加入真实默认实力元数据后，它不再出现在主组或最高排序，研究概率仍保留。
该输入是合成的完整准入控制，不冒充历史比赛当时的销售资格；归档中36份旧 F 文件的实际主组没有默认实力场次，
旧组过滤另以合成冻结组回归证明：当前展示去掉受影响组，旧账仍保留原三组及原字节。

真实26159期：置信度顺序由 `[1,7,10,14,3,13,9,5,12,6]` 变为 `[10,14,3,13,9,5,12,6]`，
只移除使用默认实力的第1、7场；所有比赛的研究概率及已知队相对次序不变。
剩余合格场次不足9时如实说明，不用默认实力场次补足任九。
任选九测试的发布状态是明确的受控输入，不声称历史报告因此获得了真实赛前发布凭据。

## 复盘与冻结保护

使用原记分板截止 `2026-11-11T17:55:16.564002+00:00`，按正式入口排除55条实验评分，
再以原 `active_keys` 筛选96条正式已评分记录，分层为：**默认实力4、双方已知92、未记录0**。
四场默认实力记录为 `jc:2042442`、`jc:2042520`、`jc:2042523`、`jc:2042530`。
三组相加与原正式总数96一致，原正式汇总指标也完全相同；未记录分支另有明确删除元数据的合成回归测试。
不能把实验评分缺少正式模型元数据误计为正式“未记录”组。

旧已结 SFC 的26161、26162期均各14场双方已知，原命中数7、11保持不变；
这份归档没有已结默认实力 SFC 样例，相应分层与旧结算不变性另用明确合成回归验证。

全档案 **2,177/2,177** 文件 SHA256 一致，包含 forecasts 453、sfc 165、daily 71、scoreboard 5 个文件。
恢复的临时数据库包含35,683场比赛、35,040条赛果、1,014队；前后哈希均为
`daa369bac81ad639d37740e8d7aafd683c39f05146d070d54c08a861aa6bc37c`。
旧预测、旧评分、旧账本和旧 SFC 结算不因这次披露被重写。

## 验收程序原始摘要

以下原文来自最终 `acceptance-final-v2/summary.json`，便于直接在 PR 中复核。
独立复跑位于本地 `reviewer-final-v2/`，结果一致；早先混合实验评分的摘要已经作废。

<details>
<summary>展开最终实跑摘要</summary>

```json
{
  "archive_commit": "1b8cc9c697b863d4c92b4e0006f2190d053e1fcb",
  "archive_files_unchanged": 2177,
  "baseline_code": "eb335c7",
  "executed_utc": "2026-11-12T13:04:47.753303+00:00",
  "frozen_file_categories": {
    "daily": 71,
    "forecasts": 453,
    "scoreboard": 5,
    "sfc": 165
  },
  "model_artifact": "models/42024862563-1/欧战.pkl",
  "model_sha256": "9d09bd903a6089434403585566fd709dd488a14af568ef3034d262dc35644274",
  "note": "全量pytest/Ruff另有日志；合成双方未知及准入/发布控制不冒充真实历史比赛或线上发布。",
  "production_written": false,
  "real_model_side_cases": {
    "jc:2042519": {
      "away": "霍芬海姆",
      "base_to_patched_probability_bytes_equal": true,
      "baseline_recalculation_minus_frozen": {
        "a": 0.0,
        "d": 0.0,
        "h": 0.0
      },
      "disclosure": {
        "away": false,
        "evidence": "explicit_sides",
        "home": false,
        "method": null,
        "used": false
      },
      "frozen_model_probabilities": {
        "a": 0.2033096409219072,
        "d": 0.21473815475337638,
        "h": 0.5819522043247165
      },
      "frozen_probability_bytes_equal": true,
      "home": "水晶宫",
      "label": "",
      "model_probabilities": {
        "a": 0.2033096409219072,
        "d": 0.21473815475337638,
        "h": 0.5819522043247165
      }
    },
    "jc:2042520": {
      "away": "伯恩茅斯",
      "base_to_patched_probability_bytes_equal": true,
      "baseline_recalculation_minus_frozen": {
        "a": 1.1102230246251565e-16,
        "d": 0.0,
        "h": 0.0
      },
      "disclosure": {
        "away": false,
        "evidence": "explicit_sides",
        "home": true,
        "method": "weakest_quartile_half_shrinkage",
        "used": true
      },
      "frozen_model_probabilities": {
        "a": 0.9396618188370497,
        "d": 0.048190196954498436,
        "h": 0.012147984208451802
      },
      "frozen_probability_bytes_equal": false,
      "home": "布斯巴达",
      "label": "使用默认实力（主队 布斯巴达；先验方法：weakest_quartile_half_shrinkage）；仅保留研究概率，不进入主候选",
      "model_probabilities": {
        "a": 0.9396618188370498,
        "d": 0.048190196954498436,
        "h": 0.012147984208451802
      }
    },
    "jc:2042523": {
      "away": "费伦茨",
      "base_to_patched_probability_bytes_equal": true,
      "baseline_recalculation_minus_frozen": {
        "a": 0.0,
        "d": -3.469446951953614e-18,
        "h": 1.1102230246251565e-16
      },
      "disclosure": {
        "away": true,
        "evidence": "explicit_sides",
        "home": false,
        "method": "weakest_quartile_half_shrinkage",
        "used": true
      },
      "frozen_model_probabilities": {
        "a": 0.006366592293012874,
        "d": 0.030716447474255334,
        "h": 0.9629169602327318
      },
      "frozen_probability_bytes_equal": false,
      "home": "AC米兰",
      "label": "使用默认实力（客队 费伦茨；先验方法：weakest_quartile_half_shrinkage）；仅保留研究概率，不进入主候选",
      "model_probabilities": {
        "a": 0.006366592293012874,
        "d": 0.03071644747425533,
        "h": 0.9629169602327319
      }
    },
    "synthetic:t53-both-unknown": {
      "disclosure": {
        "away": true,
        "evidence": "explicit_sides",
        "home": true,
        "method": "weakest_quartile_half_shrinkage",
        "used": true
      },
      "kind": "synthetic_inputs_actual_archived_model",
      "label": "使用默认实力（主队 T53合成未见主队、客队 T53合成未见客队；先验方法：weakest_quartile_half_shrinkage）；仅保留研究概率，不进入主候选"
    }
  },
  "restored_counts": {
    "closing_odds": 100030,
    "matches": 35683,
    "odds": 14349,
    "results": 35040,
    "teams": 1014
  },
  "restored_database_sha256_unchanged": "daa369bac81ad639d37740e8d7aafd683c39f05146d070d54c08a861aa6bc37c",
  "score_cohort": {
    "active_keys": 110,
    "active_scored": 96,
    "as_of_utc": "2026-11-11T17:55:16.564002+00:00",
    "experimental_rows_excluded": 55,
    "production_rows": 96,
    "raw_scored_rows": 151,
    "selection": "scoreboard.command production-only rows and original audit.active_keys"
  },
  "score_strata_counts": {
    "default_strength": 4,
    "known_teams": 92,
    "unrecorded": 0
  },
  "sfc_all_research_probabilities_unchanged": true,
  "sfc_filtered_confidence_order": [
    10,
    14,
    3,
    13,
    9,
    5,
    12,
    6
  ],
  "sfc_original_confidence_order": [
    1,
    7,
    10,
    14,
    3,
    13,
    9,
    5,
    12,
    6
  ],
  "sfc_prior_sequences_excluded": [
    1,
    7
  ],
  "synthetic_qualification_control": {
    "affected_absent_from_main_groups_and_leaders": true,
    "affected_control_id": "jc:t45-1",
    "known_groups_before_and_after": 3,
    "known_match_count": 6,
    "known_projection_byte_equal": true,
    "known_projection_sha256": "9ab343560f092cdfc594342cc134d31cbc9c747c26bf6e5cb5981b6e327a2139",
    "research_probabilities_retained": true,
    "scope": "明确合成的T45准入控制；使用真实未知先验元信息，不冒充历史销售资格"
  }
}
```

</details>

## 检查命令与结果

```bash
PYTHONPATH=src python scripts/verify-t53-archive.py \
  --archive /workspace/work/t52-evidence/archive \
  --output /workspace/work/t53-evidence/acceptance-final-v2
PYTHONPATH=src python -m pytest -o addopts='' -q
ruff check src tests examples scripts/verify-t53-archive.py
ruff format --check src tests scripts/verify-t53-archive.py
```

最终版本实跑输出（退出码均为0）：

```text
pytest: 2641 passed, 6343 warnings in 130.65s (0:02:10)
ruff check: All checks passed!
ruff format --check: 290 files already formatted
```

新增59项 T53 回归。警告来自现有 penaltyblog/NumPy 弃用提示，本次未调整模型或依赖来消除警告。

检查使用 Python 3.12.14，显式 `PYTHONPATH` 指向 T53 工作区；没有安装新依赖。
另一个只读代理独立审阅改动、跑定向回归并复跑真实归档验收。
发现的旧日报缺少原件 SHA、SFC 派生字段可能清除已有默认实力证据两处问题均已修正并重新验证。
验收过程另纠正了脚本把实验评分混入正式分层的问题；最终口径与生产 `scoreboard.command` 一致。
这些都是副本验证，没有运行生产流程。

原始摘要与逐场完整文件保存在 `/workspace/work/t53-evidence/`，最终实现验收使用 `acceptance-final-v2/`；
原模型与完整输入长期保留在上方固定数据提交，不复制到代码 PR。
完整逐场中间产物只在本次工作区保留，未发布到远端，不能把本地路径当永久下载链接。
本目录已嵌入完整小型验收摘要；只提交本说明和 `SHA256SUMS`；清单中的 `repository/` 表示本次代码根目录，
`evidence/` 表示隔离的实跑产物。复核者可用固定输入及脚本重新生成全部证据。

本次没有修改先验公式、模型参数、P2门槛、BET或采集规则；没有合并或发布正式数据。
