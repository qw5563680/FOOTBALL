# T63 旧七联赛与欧战池兼容性验收

本目录只核对固定输入、固定截止时间下的生产模型兼容性，不计算 Log Loss、收益或其他赛果评分，不调整参数，不生成 P2。原 phase1 证据与固定原档保持不变。

`final/acceptance.json` 是最终验收摘要；`final/targets.csv` 提供 119 行目标及两版本概率、截止时间 UTC/BJT、模型版本、未生成原因；`final/fits.csv` 提供 39 组训练 ID、参数与完整拟合数值的 SHA。原始 100 格比分矩阵和预测元数据保存在 `baseline/summary.json` 与 `current/summary.json`，完整拟合数值和训练 ID 分别保存在两目录内的对应 JSON。

结果为 39/39 组拟合逐项一致，105/105 场概率、完整比分矩阵与预测元数据逐项一致，14 场无可信赛前截止时间的记录及原因一致。比较使用序列化后的完整浮点数与文件字节，不设容差。六项比较器 fixture 测试覆盖概率末位变化、训练 ID、参数、遗漏目标与截止变化。

| 群体 | 目标 | 生成预测 | 未生成 |
| --- | ---: | ---: | ---: |
| 旧七联赛 | 65 | 51 | 14 |
| 欧冠、欧罗巴、欧协联自然目标 | 54 | 54 | 0 |

旧七联赛指英超、西甲、意甲、德甲、法甲、葡超、巴甲。截止时间直接沿用 `census-v1/targets.json`，没有重选；欧战池使用原档 UTC 2026-11-14 至 11-28 的全部 54 场自然赛程，统一截止 UTC 2026-11-14 00:00（北京时间 08:00）。此处基线是 main 原有 `read_history`、参数及模型路径；不应用 phase1 实验的 first-containing-commit 可知时间替换规则。

基线源码为 `b83d2cc7d975e41f418f59aad3341a7d1574f95a`，独立路径 `/workspace/work/t63-evidence/regression/baseline-code` 的 157 个 Python 文件均已对该 Git 提交逐字节核对。当前版本来自工作树的冻结快照 `current-code`，源码摘要为 `b30354411759a2c51ea3b3e2668f703087696eaa6f4ee7f634495dc856e1a273`，当时 HEAD 为 `3bac25c5bcb25972f7559d6b658ea697b39fa32f`。这是工作树快照验收，不把 HEAD 当作全部已提交实现；最终提交应再审计其与快照的差异。七个旧联赛完整参数条目均与基线相同。

两次执行均通过生产默认来源路径读入，不传 source-version/source-selection override。`read_history` 新增 10 个身份和核验元数据列，使 39 组全列摘要不同；原有全部 11 列的值与顺序、DataFrame attrs、实际选入训练的行和所有模型数值完全一致。新增列清单及只读复核见 `final/schema-difference.json`、`baseline-schema.json`、`current-schema.json`。

复用只读数据库 `/workspace/work/t63-evidence/final-fixed-input/feeds.db`，SHA-256 为 `2cca04516fe2b9a7d9ba212ba95a33d523740b3bb8c583c1874fc970139a0ca4`。teams/matches/results 逐行与固定 `e5848e5d7de83a0cd024fa3e03e08cf6db6c7ee4` 原档核对一致。数据库通过 SQLite `mode=ro&immutable=1` 和 `PRAGMA query_only` 读入；两次执行及后续 schema 复核前后 SHA 相同。程序阻断网络入口，体育网络请求为 0。本目录不复制原数据库，也不复制 320 MB phase1 目录。

复核使用 `/workspace/work/t48-venv/bin/python`；环境版本在 `runtime.json`。`baseline-executed-worker.py` 保留基线实际执行脚本；当前 `compatibility_worker.py` 与其仅有 kwargs 字典的等价写法整理。两个 worker 的准确 SHA 均在最终摘要。所有命令均应设置 `PYTHONDONTWRITEBYTECODE=1`，重跑输出必须使用新目录。

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/workspace/work/t63-evidence/regression/baseline-code/src \
  /workspace/work/t48-venv/bin/python compatibility_worker.py \
  --database /workspace/work/t63-evidence/final-fixed-input/feeds.db \
  --params /workspace/work/t63-evidence/regression/baseline-code/data/model/params.json \
  --targets targets.json --output baseline-rerun --arm baseline \
  --code-sha b83d2cc7d975e41f418f59aad3341a7d1574f95a

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/current-code/src" \
  /workspace/work/t48-venv/bin/python compatibility_worker.py \
  --database /workspace/work/t63-evidence/final-fixed-input/feeds.db \
  --params current-code/data/model/params.json --targets targets.json \
  --output current-rerun --arm current --code-sha "$(cat current-code-identifier.txt)"

PYTHONDONTWRITEBYTECODE=1 /workspace/work/t48-venv/bin/python compare_compatibility.py \
  --baseline baseline-rerun --current current-rerun --output comparison-rerun
```

`final/evidence-sha256.json` 固定本次证据文件哈希；`input-manifest.json` 和 `current-source-manifest.json` 保留完整输入与源码清单。
