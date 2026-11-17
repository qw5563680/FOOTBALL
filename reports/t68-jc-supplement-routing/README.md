# T68 第八节第 12 项：jc-supplement 路由补丁

范围依据：所有者本轮补丁要求、`docs/tasks/T68-new-competition-team-identity.md` 第八节第 12 项。
基线：`aa6c980bcb3a708e774a69a931e5ca8e43617a65`。
分支：`deepseek/T68-jc-supplement-routing`（按所有者指定）。

## 问题与修改

所有者提供的真实运行 44113033182 暴露 `jc-supplement` 同时进入 `apis` 的问题。
本轮从 main 工作流确认：`jc-catalogue` 已接管该类型，`apis` 的排除条件却只列出旧两种类型。

- `.github/workflows/football-feeds.yml`：`apis.if` 补排 `jc-supplement`；输入说明补列该类型。
- `tests/test_t61_workflow.py`：从工作流 `jc-catalogue.if` 动态提取所有 `probe_kind`，
  要求 `apis.if` 逐个显式排除，并实际求值确认仅 `jc-catalogue` 启动。
  覆盖 main、旧 run 标签、旧 verify 标签三个上下文，不写死类型清单。
- 其余 8 个作业与基线逐字相同；`apis` 内除作业级条件外逐字相同。

## 离线验收

1. 修复前：新增测试三个上下文全部失败，原始错误为
   `AssertionError: apis missing exclusions: {'jc-supplement'}`。
2. 修复后：`pytest -q -o addopts= tests/test_t61_workflow.py tests/test_stats_workflow.py tests/test_t68_jc_supplement.py`
   得到 `117 passed in 7.57s`。
3. 未来类型漏排验证：临时在工作流 `jc-catalogue.if` 中加入 `jc-future-offline-test`，
   不改 `apis.if`，三个上下文全部失败并指出该新类型；随后恢复原文件。
   这证明以后在同一条件中追加类型、漏写排除时，测试会失败。
4. 全量离线测试：`4692 passed, 10329 warnings in 594.02s (0:09:54)`，退出码 0。
   命令：`PYTHONPATH=src OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 pytest -q -o addopts= -o tmp_path_retention_policy=failed --basetemp=work/t68-jc-routing/pytest`。
   警告为现有 penaltyblog / NumPy 弃用提示，未隐藏。
5. `ruff check src tests examples`：`All checks passed!`。
   `ruff format --check src tests`：`427 files already formatted`。

测试沿用 `tests/conftest.py` 的会话级 HTTP 禁止访问保护，仅使用离线样本。
环境磁盘空间有限，全量运行使用 pytest 内置 `tmp_path_retention_policy=failed`，
只清理通过用例的临时目录，不改变测试收集、断言或跳过条件；BLAS/OMP 设为单线程。
完整命令及日志保留在本次工作区 `work/t68-jc-routing/`。

## 交回边界

本次只做离线验证，不使用正式体育密钥，不触发 Actions，不改其他作业、正式数据或模型。
提交带 `[skip ci]`，避免创建 PR 自动运行 CI；本报告不能代替审查者复审。
无需重跑模型。草稿 PR 交回复审，未合并。
