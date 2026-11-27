# T70 联网研究线离线验收

主干基线为 `550072621929dc06445ef4dddeff6120cbfe89ab`。DeepSeek 样本采用合成响应，通过 `responses` 截获请求。真实接口试跑留给审查者。[任务单](../../docs/tasks/T70-web-research-line.md) · [接口验证边界](../../docs/DATA_SOURCES.md#t70-deepseek-联网研究待单场实测)

## 实际归档只读核对

2026-11-27 北京时间 20:25:03 的竞彩成功目录来自 feeds run `45992375262-1`。同一时刻的 38 条销售记录有 37 条被保留。核对目录、比赛 CSV、预测 JSON 和已发布回执的哈希后，得到下面两条。

| 竞彩 | 比赛 ID | 最新归档 | 已发布球队预测 | 核对结果 |
| --- | --- | --- | --- | --- |
| 周五002 | `jc:2043056` | 海登海姆对纽伦堡，德乙。北京时间 11 月 28 日 01:30 开球 | 批次 `45990113179-1` 在北京时间 11 月 27 日 20:03:17 发布。主胜约 44.98%、平 24.40%、客胜 30.62% | 目录、比赛、预测和开球时间一致，符合只读候选条件 |
| 周五004 | `jc:2043058` | `matchStatus=Selling`，`sellStatus=2`，`retained=0` | 有预测 | 最新目录的销售状态及保留标记不符合条件 |

[固定数据归档](https://github.com/qw5563680/FOOTBALL/tree/458bf87d7c3727aad9268a6c1123078a4d933bd6) · [官方销售记录归档](https://github.com/qw5563680/FOOTBALL/blob/458bf87d7c3727aad9268a6c1123078a4d933bd6/state/audit/jc_catalog/2026-11.csv) · [周五002 冻结预测](https://github.com/qw5563680/FOOTBALL/blob/458bf87d7c3727aad9268a6c1123078a4d933bd6/forecasts/2026-11-28/45990113179-1/forecast.json)

## 关闭研究线的字节对照

主干和 T70 使用同一离线数据库及相同参数路径。北京时间 11 月 3 日 02:00 拟合、发布回执设为 02:01，11 月 4 日 04:00 结算和生成日报。两边各训练 84 行英超赛果、生成 2 场预测、计分 2 场。输入为 T16/T17 样本及两条合成的赛后结果观察，回放使用合成发布回执。网络被封闭。

`diff -qr` 确认 **49 个归档文件逐字节相同**。模型文件、两天 `forecast.json`、报告和冻结清单、正式记分和账本、日报、日报冻结回执均在其中。`cmp` 对照的 Actions 摘要也相同。由于模型冻结包含绝对参数路径，重放时共用同一输入和训练临时路径。[复现脚本](../../examples/t70_disabled_replay.py)

在 T70 工作树中用装有项目开发依赖的 Python 执行以下命令。

```bash
export PYTHONHASHSEED=0 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
T70_ROOT=$(mktemp -d)
T70_SCRIPT=$PWD/examples/t70_disabled_replay.py
git worktree add --detach "$T70_ROOT/base-code" 550072621929dc06445ef4dddeff6120cbfe89ab
mkdir "$T70_ROOT/scratch"
export TMPDIR="$T70_ROOT/scratch"
PYTHONPATH="$PWD/src" python "$T70_SCRIPT" --root "$T70_ROOT" seed
PYTHONPATH="$T70_ROOT/base-code/src" python "$T70_SCRIPT" --root "$T70_ROOT" replay --name baseline
PYTHONPATH="$PWD/src" python "$T70_SCRIPT" --root "$T70_ROOT" replay --name t70-off
diff -qr "$T70_ROOT/baseline/archive" "$T70_ROOT/t70-off/archive"
cmp "$T70_ROOT/baseline/hashes.json" "$T70_ROOT/t70-off/hashes.json"
cmp "$T70_ROOT/baseline/actions.md" "$T70_ROOT/t70-off/actions.md"
```

## 离线实跑

- 合成赛程 `jc:2`，周二002，从已发布预测和销售目录筛出，依次发送模拟研究、模拟结构化请求。实际冻结并读回原文、两份响应、提示词哈希和球队模型来源。日报显示「主胜 60.00%」，核对告警 0 条。设置一场上限时发出 2 次请求，其余候选没有请求。
- 没有引用的事实标为无出处。结构错误或原报告与转换结果矛盾时留作 `unparsed`，赛程字段对不上标为 `identity_mismatch`。相同历史比赛的多次引用只计一次，核对到的比分差异、未找到的赛果及竞彩赔率偏差分别提示。
- 模拟 HTTP 失败、超时、无用量、费用不足及用量超预留均在对应场次或运行清单留痕。每步判定预算后再发送请求。单步超预留时没有后续请求。临近开球的模拟中，迟于开球收到的响应没有进入冻结结果。
- 计分保留命中、覆盖、概率评分和同场三方配对分母。在同一归档中启用研究、遇到研究损坏和模拟独立记分板写入失败时，原正式记分板四份文件保持逐字节相同。

```bash
pytest -q tests/test_t70_*.py tests/test_t59_candidate_workflow.py \
  tests/test_cloud_predict_workflow.py tests/test_daily_chain_workflow.py \
  tests/test_stats_workflow.py tests/test_t91_github_api_workflow.py \
  tests/test_scoreboard_integration.py tests/test_experimental_isolation.py \
  tests/test_daily_digest.py tests/test_digest_notify_overlong.py
```

## 正式验收待核

合并后由 DeepSeek 通过 `main` 的 Football cloud feeds 手动执行 `operation=web-research`，只试一场。逐项核对服务端工具协议、引用、调用数、token、实际价格和币种、`safety_check` 是否必须。在账单误差和每日预算由所有者确认前，继续关闭每日研究。费用上限是请求准入的估算，服务端工具次数的强制上限尚未核实。正式运行信息由 Claude 补录。
