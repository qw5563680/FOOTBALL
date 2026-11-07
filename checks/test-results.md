# 已执行检查

- `/workspace/work/t48-venv/bin/python -m pytest -q`：退出码0，全套通过。
- 同一代码树 `pytest --collect-only -o addopts='' -q`：2533项。
- 既有 penaltyblog/NumPy DeprecationWarning；没有新增失败。
- `ruff check src tests experiments/core39`：All checks passed。
- `ruff format --check src tests experiments/core39`：全部已格式化。
- 实际执行 extract_archive.py 与 build_report.py；原件SHA、身份、时间、比分、数值、空值、39项定义、780链接、工作簿重新打开断言均通过。
- pytest是原业务基线回归；新报告脚本另外用实际归档断言验收，不把旧单元测试当作新接口实测。
- 本轮体育接口调用0，未触发生产任务。
