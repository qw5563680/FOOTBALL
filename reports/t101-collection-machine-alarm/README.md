# T101 采集机闹钟离线验收

Windows 闹钟在北京时间 11:05、11:35、16:05、16:35、18:05、18:35 检查保险工作流。离线测试替换了任务计划程序、令牌读写和 HTTP，实际运行仓库的 PowerShell 脚本。

| 场景 | 离线回放结果 |
| --- | --- |
| 20 分钟内有保险检查，或仍在排队或运行 | `recent_run 200`，触发请求 0 次 |
| 20 分钟内没有检查 | `dispatched 204`，触发请求 1 次，发送给 `football-watchdog.yml` |
| 北京时间当天已尝试 6 次 | `daily_limit -`，触发请求 0 次 |
| 令牌缺失、HTTP 401、网络失败 | 各写一行 `missing_token -`、`request_failed 401` 或 `request_failed -`，进程正常退出 |
| 重装及卸载 | 重装保留当日计数及日志。卸载移除计划任务、本地脚本、令牌及日志 |

HTTP 模拟检查固定的两个 `api.github.com` 地址和禁用重定向。断言脚本纯 ASCII，终端和日志没有模拟令牌内容，日志达到上限时截断。

| Actions 读取场景 | 日报顶部输出 |
| --- | --- |
| GitHub 定时 | 自动保险检查：北京时间 2026-11-03 18:05:00，GitHub 定时触发。 |
| 采集机闹钟 | 自动保险检查：北京时间 2026-11-03 18:35:00，采集机闹钟触发。 |
| 手动 | 自动保险检查：北京时间 2026-11-03 18:36:00，手动触发。 |
| 本档没有运行 | 本档自动保险检查没有运行。 |
| Actions 记录无法读取 | 自动保险检查：未能读取 Actions 运行记录。 |

表中时间取自离线 Actions 测试样本。测试检查按当前北京日及档次筛选、以及超长通知保留顶部状态。补发测试复用冻结版本。

```bash
pytest -q tests/test_t101_collection_alarm.py tests/test_windows_collection_machine.py \
  tests/test_daily_digest.py tests/test_daily_digest_workflow.py \
  tests/test_digest_notify_overlong.py tests/test_t85_watchdog.py
```

Windows 任务注册和凭据保护在 Linux 上使用模拟组件，尚待所有者按[运行手册](../../docs/RUNNER.md#采集机闹钟t101)安装。交审后由审查者逐步核对手册。正式合入并安装后，Claude 按任务单观察三天，逐个记录闹钟触发、已有保险检查导致去重和实际补跑结果。
