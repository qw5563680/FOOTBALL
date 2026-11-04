# T31 离线合成样本

本目录所有球队、比赛与统计均为合成数据，只用于测试，不代表真实 API 探测或球队能力。
`fixture.json` 与 `store.json` 给定一致的 UTC 开球、正式身份和已有比赛；`aliases.json` 仅用于测试 ID 关联。
`fd.csv` / `fd_missing.csv` 覆盖主联赛统计列、明确 0、空串与缺列，不包含赔率。
`statistics_full.json` 覆盖 T30 审查者确认的 16 类、xG / goals_prevented 和可选 Free Kicks。
其他四端点的基础响应复用 `../t30/` 合成原件。测试在临时目录用 RawStore 写副本及 sidecar，
再从这些副本制造空响应、errors、纠错、哈希冲突和身份冲突；不读取真实 key 或发真实网络请求。
