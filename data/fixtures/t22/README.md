# T22 离线样本

quota_cases.json 是人工构造的配额与 /status 响应样本，用于测试 7500 次付费档、旧格式和回退。
它不是正式账号的响应，不能作为套餐权限或 /status 是否计费的实测依据。

`paid_season_probes.csv` 和 `paid_status_probes.csv` 是 Claude 在 Actions 运行
41597368373、41597683219 后发布到数据分支的审计记录副本，分别含 30 条赛季观察、4 条 status 观察。
只将 CRLF 换成 LF，字段值保持原样；未加入 API 全量响应。来源提交、原始路径与副本 SHA256
见 `provenance.json`。测试用它们核对权限配置，并验证旧八列 status 审计升级时保留原有证据。
这些真实记录通过 GitHub 读取，开发环境没有向 API 发请求。
probe 测试另外复用 t10、t14 的离线 fixtures；通过修改参数构造不同赛季，不在测试中联网。
