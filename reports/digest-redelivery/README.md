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

## 二审离线回放

| 场景 | 原提交 f43e9a0e | 修复后的结果 |
|---|---|---|
| 评论接口 HTTP 403，月报 Issue 正文可写 | 月报目录更新 0 次，冻结链接未挂出 | 分别模拟评论列表 GET、新建 POST 和编辑 PATCH 的 403。三次均成功更新 Issue 101 正文，附上 frozen.md 的直接链接，保留人工说明及旧日期链接。每次记录实际失败阶段与 HTTP 403，成功评论回执均为 0。 |
| 同日存在正文无法关联任何归档的机器人评论 201 | 可补发 A，补发 B 时因两条评论同时存在而中止 | 补发 A 新建 202；补发 B 更新 202；正常下一班 C 更新 202。三版各有一份成功回执，旧评论 201 保持原文。重试同版 A 或 B 追加评论 0 条；C 发出后再补 A、B 也保留 C 正文。 |

三版连续运行一共写入评论 3 次，分别为 POST、PATCH、PATCH，最终保留 2 条评论。另测 A 由正常班次首发时也保留无法关联的旧评论。所有 GitHub 响应均由离线测试提供。

```sh
pytest -q -o addopts= tests/test_digest_notify.py tests/test_digest_history.py tests/test_daily_digest_workflow.py --tb=short
pytest -q -o addopts= -n 4 --dist=worksteal -o tmp_path_retention_policy=failed --basetemp=/tmp/second-review-220
```

定向 70 项、全量 4973 项通过。Ruff、格式与 Git 空白检查通过。
