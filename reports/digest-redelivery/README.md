# T-20261123-03 日报通知补发验收

月报目录按日期与冻结生成时间列出每一版，直接打开数据分支的 `daily/<日期>/<版本>.md`。读取时核验该版正文 SHA-256。仅有旧评论的日期仍使用原评论链接。补发读取指定版本的正文，原始生成时间和文件字节保持一致。

## 操作入口

在 Football cloud feeds 选择 `workflow_dispatch → digest`。要补发某个归档版本，在 options 中填写这两个已有归档值：

```json
{"digest_date":"2026-11-23","digest_version":"45039288240-1"}
```

这是操作示例。本次验收只在本地运行，GitHub HTTP 全部由测试拦截。补发跳过渲染及 Markdown 推送，直接按日期和版本查找，核对正文哈希，再读取已有成功回执。默认 digest 仍生成当天日报。单独补发旧月报告会保留现有月份的置顶和关闭状态。补发尚未通知的旧版本时，会保留当天已通知的新版本及下一班的比较基准。

## 模拟 403、恢复、重复补发

离线夹具日期为 2026-11-03，版本为 first。模拟结果如下。

| 步骤 | 实际结果 |
| --- | --- |
| 第一次发表评论 | `publish_comment` 收到 HTTP 403。记录请求 ID `FACE:1234`、剩余额度 `0`、重置值 `1793760000`、重试间隔 `60` 秒。无成功回执。 |
| 两天后补发 | 模拟 Issue 101、评论 201，成功回执时间 11 月 5 日 18:00:01 北京时间。冻结生成时间仍为 11 月 3 日 18:00:00 北京时间。 |
| 对同一版本再补发 | 新发表或修改评论 0 次，月报正文修改 0 次。成功回执、冻结 Markdown 和版本 JSON 逐字节相同。 |
| 独立跨月用例 | 新版原评论 201 保留。未送达的旧版新发为评论 202，月报仍展示新版，下一班也仍以新版为基准。 |

该版 Markdown SHA-256 为 `a08550b461c5d053795ebb65f0b925fc8278d67ce9cae7612d7aac2530ce2b93`。失败记录保存在 `daily/<日期>/notification-failures/<版本>/<尝试编号>.json`，成功后仍可查；成功回执沿用 `<版本>.notification.json`。测试在响应中加入模拟敏感正文与 Cookie，断言它们及模拟凭据均未进入失败记录。

## 执行的检查

```sh
pytest -q -o addopts= tests/test_digest_notify.py tests/test_digest_history.py tests/test_daily_digest_workflow.py --basetemp=/tmp/p2-focused-final --tb=line
pytest -q -o addopts= -n 4 --dist=worksteal -o tmp_path_retention_policy=failed --basetemp=/tmp/p2-final
ruff check src tests examples
ruff format --check src tests
git diff --check
```

定向测试 65 项通过。还在空 Git 仓库用工作流的两个路径表达式运行了 `git status --porcelain`，确认能发现首次产生的失败记录。

全量 4968 passed，10336 条依赖警告。Ruff 检查、格式核验及 diff 空白检查全部通过。
