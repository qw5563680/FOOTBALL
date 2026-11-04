# T30 离线样本

本目录所有响应均为合成测试输入，没有真实请求，不是权限或覆盖率证据。
statistics/events/lineups/players/injuries 各有一份有效响应（含 null）；
cases.json 的 errors.plan/access/rateLimit/half 变体与空 response 在测试中套用到各端点。
fixtures.json 包含 FT/AET/PEN/PST/CANC/NS；已知 ID 对照也在 cases.json。
leagues.json 里的 coverage=true 仅测试字段保存，不能写成真实结论。
half=true 的参数接受、响应结构和统计时段全部待 Claude 在 Actions 核验。
