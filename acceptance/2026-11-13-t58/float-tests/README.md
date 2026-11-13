# T58 浮点比较验收（2026-11-13）

只改测试中的两个 `probability` 汇总比较，不改生产程序、模型或冻结文件。

## 实跑结果

| 环境 | 修复前 | 修复后 |
| --- | --- | --- |
| 现有 `/workspace/work/t48-venv`，Python 3.12.14 | 18 passed | 19 passed |
| 独立重建 `/workspace/work/t58-fresh-venv`，Python 3.12.14 | 18 passed | 19 passed |
| 独立重建 `/workspace/work/t58-fresh311-venv`，Python 3.11.17 | **16 failed, 2 passed** | **19 passed** |

新增的第 19 例检查仅浮点末位差可以通过，真实数值变化、整数计数、布尔类型、模型名、列表顺序及缺键均被拒绝。

`*-before.xml` 和 `*-after.xml` 提供每例测试结果；对应 `.log` 保存原输出。
`*-pip-freeze.txt` 保存三套环境实际依赖；`fresh*-install.log` 保存独立重建日志。
GitHub Actions CI **尚未执行**；现有 workflow 是 Python 3.12，但本地 3.12 通过不等于 GitHub CI 通过。

## 复现原因与边界

`probe_summary_drift.py` 使用仓库合成 fixture，连续调用正式记分板入口两次。
`fresh311-drift.json` 逐字段显示六处 `market.calibration[3].mean_p` 从
`0.3598130841121496` 变成 `0.35981308411214946`，绝对差 `1.1102230246251565e-16`；
`fresh-drift.json` 在 Python 3.12.14 无差异。

评分 JSON 按键排序后重读，h/d/a 的浮点求和顺序改变；不同 Python 版本的 `sum` 算法会表现出末位差。
新 helper 对浮点汇总使用 `abs=1e-14, rel=0`；类型、键、列表顺序和非浮点值仍完全相同，非有限浮点也拒绝。
原来的 `accounts` 比较、账本字节、原评分前缀、重跑评分完整字节和坏文件原字节全部保持精确断言。

## 文件和命令

修改 `tests/test_experimental_isolation.py`、`tests/test_experimental_integration.py`；
新增 `tests/scoreboard_summary_support.py`。这三文件通过 Ruff lint 与 format check，见 `ruff-*.log`。

在 `/workspace/work/football-t58` 执行；同一命令分别使用上表三套 Python，`LABEL` 为
`existing`、`fresh`、`fresh311`，修复前后分别为 `before`、`after`：

```sh
PYTHONPATH="$PWD/src" /workspace/work/t58-fresh311-venv/bin/python -m pytest -q tests/test_experimental_isolation.py tests/test_experimental_integration.py --junitxml=/workspace/work/t58-evidence/float-tests/fresh311-after.xml > /workspace/work/t58-evidence/float-tests/fresh311-after.log 2>&1
PYTHONPATH="$PWD/src:$PWD/tests" /workspace/work/t58-fresh311-venv/bin/python /workspace/work/t58-evidence/float-tests/probe_summary_drift.py > /workspace/work/t58-evidence/float-tests/fresh311-drift.json
/workspace/work/t48-venv/bin/ruff check tests/test_experimental_isolation.py tests/test_experimental_integration.py tests/scoreboard_summary_support.py
/workspace/work/t48-venv/bin/ruff format --check tests/test_experimental_isolation.py tests/test_experimental_integration.py tests/scoreboard_summary_support.py
```

重建命令（仅开发依赖安装联网；全部测试使用离线 fixture，无体育数据请求）：

```sh
python -m venv /workspace/work/t58-fresh-venv
/workspace/work/t58-fresh-venv/bin/python -m pip install -e '.[dev]'
uv python install 3.11
uv venv --python 3.11 --seed /workspace/work/t58-fresh311-venv
/workspace/work/t58-fresh311-venv/bin/python -m pip install -e '.[dev]'
```

完整测试和 PR CI 由主任务另行执行；本目录仅对 T58 第三节负责，不声称正式预测有效或已通过独立审查。
