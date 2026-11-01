# T14a · 首次真实 apis 运行的两处 partial 跟进

## 背景
T14 合并后在 PR 分支做了一次真实 `workflow_dispatch`（run 41348153330，`operation=apis`，`days=3`，`slot=late`），
数据分支写入 ec9efc4，但摘要为 partial，作业日志（已脱敏）如下：

```
WARNING:zucai.cloud_apis:2026-10-29: 缺少有效的环境凭据
INFO:zucai.sources.apif_command:APIF 请求=1 剩余配额=59 入库=15 linked_existing=5 ... 可疑=9 未对齐球队=23
INFO:zucai.sources.apif_command:APIF 请求=1 剩余配额=59 入库=11 linked_existing=3 ... 可疑=7 未对齐球队=22
INFO:zucai.sources.apif_command:APIF 请求=1 剩余配额=58 入库=0 ... 可疑=0 未对齐球队=0
WARNING:zucai.cloud_apis:2026-10-30: 响应、配置或导入校验失败 (ValueError)
```

## 目标
1. **fdorg 凭据**：apif 的 Secret 正常而 fdorg 报"缺少有效的环境凭据"，说明 GitHub Secret `FOOTBALL_DATA_ORG_TOKEN`
   的值本身有问题（为空、含空格或非 ASCII 字符）。代码侧只做一件事：`failure_reason` 把"变量未设置/为空"和
   "含非法字符"区分开（只报原因，**不报长度、不报任何字符**），便于审查方判断要不要让用户重新粘贴。
   在云端环境用环境变量里的 token 跑一次 `fetch fdorg --days 1`（最多 1 次请求）确认代码路径本身没问题。
2. **apif 2026-10-30 的 ValueError**：在云端环境用 `zucai fetch apif --date 2026-10-30`（最多 1 次请求）复现，
   找到 raise 的位置。若是"当日已配置联赛无比赛"之类的正常情况，改为 status=complete、0 行、不标 partial；
   若是真实数据问题，保留 partial，但把**我们自己代码抛出的 ValueError 文本**（中文说明，不含 URL/响应体/凭据）
   截断到 80 字放进摘要原因里。给两种情况都补 `responses` 离线测试。
3. `failure_reason` 对 requests 异常继续只给通用原因，不得泄露 URL、响应体或凭据。

## 不做
- 不改采集范围、解析、去重和配额规则。
- 不碰工作流文件，除非修复需要。
- 不重放或下载 Actions artifact。

## 验收标准
- 离线测试覆盖：空/非法凭据的两种原因文本；apif 零行日期不再 partial（或 partial 原因含我们自己的 ValueError 文本）。
- PR 描述写明 2026-10-30 的根因（一句话）、在线请求次数（fdorg ≤1、apif ≤1），凭据值不得出现。
