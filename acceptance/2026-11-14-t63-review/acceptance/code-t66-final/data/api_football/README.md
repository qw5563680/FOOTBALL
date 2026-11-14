# 旧比赛配置的用途

`leagues.json` 不再作为比赛赛程、赛果采集的默认路由表；统一以
[`../league_map.json`](../league_map.json) 为准。这里保留原值用于旧版本兼容和离线回归，
不能在此添加新赛事来开启采集。

技术统计/伤停等兼容调用仍可能通过旧 `LEAGUES` 常量显式读取本文件；
技术接口授权与赛程赛果授权分开，T65 不扩大技术范围。不要删除或批量同步候选映射。
`test_legacy_provider_values_and_modes_remain_exact` 检查原有路由配置与注册表相同，
所有新增候选均保持禁止每日/回填。
