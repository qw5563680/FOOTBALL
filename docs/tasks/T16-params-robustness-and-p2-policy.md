# T16 · T15 审查跟进：参数文件健壮性、CLI 输出与 P2 门槛策略

## 背景
[T15 审查记录](../reviews/2026-11-02-t15-review.md) 未发现必须修的缺陷，但列出五条建议修和两条需要所有者决策的事项。
本任务单把它们合成一个 PR 处理：五条建议修是明确的代码改动；两条决策项由所有者在开工前勾选选项，
实现者只做被勾选的那一项。原任务单 T15 「不做」里提到的「定时自动训练」顺延为 T17，并依赖本任务。

## 目标
1. `tune` 的诊断开关不能悄悄改写正式参数文件；配置文件缺键时给出可读错误。
2. 参数文件的定位不依赖可编辑安装；回落到默认值时必须留下日志。
3. `zucai train` 默认输出保持可读，逐场记录按需输出。
4. 所有者就 P2 门槛与评估库 SHA256 的绑定方式、日职赛季规则做出决定并落地。

## 范围
1. **强制校准诊断的写入保护**（`src/zucai/predict/tuning.py`，CLI `tune`）：
   `--draw-calibration` / `--no-draw-calibration` 显式给出时，若 `--params-file` 未指定或解析后等于默认
   `data/model/params.json`，抛 `ValueError("诊断模式不能写入默认参数文件，请指定 --params-file")`，不写文件。
   未强制开关的正常滚动选参行为不变。
2. **配置条目校验**（`src/zucai/predict/parameters.py` `read_parameters`）：
   每个联赛条目必须含 `half_life_days`（正有限数）、`selection_as_of_utc`（带时区）；
   `p2_ready=true` 的条目还必须含 `generated_utc`、`database_sha256`、`training_policy`、`sources`、
   `unknown_team_prior`、`draw_calibration`、`p2_evidence.as_of_utc`、`p2_evidence.seasons`。
   缺键或类型错误抛 `ValueError`，错误文本点名联赛与缺失键；`resolve_parameters`、`model_readiness`
   不再可能抛 `KeyError`。CLI 现有的 `except (ValueError, OSError)` 因此能正常接住。
3. **参数文件定位**（`src/zucai/predict/parameters.py` `PARAMS_PATH`）：
   解析顺序改为 环境变量 `ZUCAI_PARAMS` → 当前工作目录下 `data/model/params.json` → 现有的源码相对路径（兼容）。
   文件不存在而回落到 90 天默认时，用 `logging` 打一条 WARNING，内容含尝试过的路径。
   `train` / `predict` 返回的 `parameter_provenance` 增加 `params_path` 字段。README 的 `ZUCAI_DB` 说明旁补一行 `ZUCAI_PARAMS`。
4. **`train` 输出瘦身**（`src/zucai/cli.py`、`src/zucai/predict/command.py`）：
   `zucai train` 新增 `--predictions-out <path>`；给出时把 `backtest.predictions`（及 `strict_backtest.predictions`）
   写到该文件（JSON Lines），stdout 的 JSON 中对应字段替换为 `{"path": ..., "rows": n}`；
   未给出时 stdout 不再包含逐场记录，仅保留汇总、折信息和各赛季指标。`evaluate()` 的返回值不变，
   供 `tune` 与测试继续使用。README 快速开始相应补一行。
5. **CLI 定义顺序**（`src/zucai/cli.py` `build_parser`）：把 `tune` 子命令的参数块整体移到 `train`
   全部参数定义之后，行为不变；用现有 `test_cli.py` 的 `--help` / 解析测试保证无回归。
6. **决策项 A · P2 可用与评估库 SHA256 的绑定**（`src/zucai/predict/parameters.py` `model_readiness`）。
   现状：线上模型的 `database_sha256` 必须等于 tune 时静态库的哈希，任何新数据重训都会退回「P2 实验」，
   直到重跑数小时的完整 tune。所有者勾选其一：
   - [ ] **A1（审查者推荐）放宽为策略绑定**：去掉 SHA256 相等要求，改为要求
     相同来源集合、相同训练策略、半衰期 / 先验方法 / 校准开关与配置一致、证据季为预测时最新两完整季、
     模型训练截点不早于配置的 `selection_as_of_utc`。模型元数据同时记录 `training_database_sha256` 与
     配置里的 `evaluation_database_sha256`，T06 报告在模型表里并列显示两者，不同即标注「评估库 ≠ 训练库」。
   - [ ] **A2 保持强绑定**：代码不变；T17 定时训练任务每次先对该联赛重跑 tune 再训练，
     把 tune 的运行时间与频率写进 T17 任务单。
   无论选哪一项，BET 始终禁用，`p2_ready` 的门槛数值不变。
7. **决策项 B · 日职赛季规则**（`src/zucai/predict/seasons.py`）。
   现状：日职按自然年切季。J.League 已宣布自 2026-27 赛季起改为跨年赛制（8 月开始），2026 年上半年为过渡赛制
   （**需验证**：开工时以 J.League 官方公告或 football-data.co.uk 的 JPN 文件实际日期为准，把确认结果写进
   `docs/DATA_SOURCES.md`）。所有者勾选其一：
   - [ ] **B1（审查者推荐）按联赛给出分段规则**：`seasons.py` 增加可按联赛配置的边界表；日职 2025 及之前按自然年，
     2026 年 1–7 月的过渡赛制记为独立标签 `2026T` 且永不计入 P2 完整季，2026 年 8 月起按 8 月换季记为 `2627`。
     `latest_complete_seasons` 对日职跳过 `2026T`。补赛季边界测试。
   - [ ] **B2 暂不处理**：在 `params.json` 的日职条目和 README 里注明「2026 年起赛季规则待定，不进入 P2 评估」，
     并把 B1 延到 2027 年数据可用时再做。
8. **测试**：1 的拒写路径与正常路径；2 的每个缺键场景至少一例，并确认不再出现 `KeyError`；
   3 的三级定位与 WARNING（用 `caplog`）；4 的 stdout 不含逐场记录、JSONL 行数与汇总 n 一致；
   6、7 按勾选项补对应测试（A1 需覆盖「训练库 ≠ 评估库仍可 P2 可用」与「来源不同则不可用」；B1 需覆盖三段边界）。
9. **文档**：README 评估一节更新 A/B 结论与新开关；`docs/ARCHITECTURE.md` T15 一节同步；
   `docs/DATA_SOURCES.md` 记录日职赛制验证结果。

## 不做
- 不重跑 15 联赛真实评估，不改 `data/model/params.json` 里已发布的半衰期与 `p2_ready` 判定。
- 不改门槛数值（n ≥ 500、0.05、0.05），不改先验与校准算法。
- 不做定时自动训练（T17）。
- 不改数据库 schema、采集、去重与可知时间规则。
- 不启用 BET。

## 验收标准
- PR 描述列出所有者对 A、B 的勾选结果；未勾选则 PR 不得合并。
- `zucai tune --league 英超 --draw-calibration`（不带 `--params-file`）退出码 2，`data/model/params.json` 字节不变。
- 用只含 `half_life_days` 的条目跑 `zucai train`，得到中文 `ValueError` 文本而非 traceback。
- `zucai train` 默认 stdout 不含 `predictions` 数组；`--predictions-out` 产出的 JSONL 行数等于 `backtest.model.n`。
- `pytest -q` 全绿、`ruff check src tests examples` 与 `ruff format --check src tests` 通过。
- `reports/` 不新增内容。
