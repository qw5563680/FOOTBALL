# T14a · partial 原因跟进

基线 main `3ecf128`；分支 `codex/T14a-apis-partial`，PR base 为 main。

## 根因与处理

2026-10-30 的云端复现返回 HTTP 200 + `errors.plan`（账户日期访问限制），
在 `src/zucai/sources/api_football.py` 的 `ApiFootball._document()` API 错误检查处抛出 ValueError；
保留 partial，摘要显示我们自己的固定中文校验说明，截断至80字。
没有下载或重放 Actions artifact，因此不把本次响应当作首次运行原件的逐字节证据。

合法空响应及筛选后无已配置联赛比赛原本就返回零行成功；新增离线用例锁定 complete，
没有放宽解析、分页、身份或配额校验。fdorg 请求前区分空凭据与非法字符；不输出凭据值、长度或字符。
requests 异常优先使用通用原因；未知 ValueError 不回显文本。

## 在线验证（北京时间 2026-11-02）

使用云端环境凭据，由 CLI 运行，额外发送保护阻止第二次请求；无重试、无重定向。

| 来源 | 参数 | GET 次数 | 结果 | 原件 SHA256 |
| --- | --- | ---: | --- | --- |
| fdorg | `fetch fdorg --competition PL,ELC,PD,SA,BL1,FL1,DED,PPL,BSA --days 1` | 1 | HTTP 200、73 场、CLI 退出0 | `ac8ca409ee0bd9f8c2b23e8ffbc4d4ed1e0fe06473c7711faf2e8de6b708d03e` |
| apif | `fetch apif --date 2026-10-30` | 1 | HTTP 200、errors.plan、CLI 退出1 | `fe7d86b0f2d524ef4d784069a9a5c39a683ca50a688b463a598ac952a38b62a0` |

原件采集 UTC 分别为 2026-11-01T20:53:16.370436Z、2026-11-01T20:51:51.310057Z，
只保存在采集环境的忽略目录 `work/online/raw/`，不提交原件、响应体或数据库。

## 离线验证

- `pytest -o addopts='' -q`：663 passed；778 条既有 penaltyblog/NumPy 弃用警告。
- `ruff check src tests`、`ruff format --check src tests`、`git diff --check` 通过。
- responses 覆盖空/非法凭据、空日期、非配置联赛日期、API 权限错误、结构错误和 HTTP 401。
- 额外覆盖 requests 异常（含 JSONDecodeError）的通用原因、未知 ValueError 脱敏、80字截断。
- 未更改采集范围、解析条件、去重、配额或工作流；未下载 Actions artifact。
