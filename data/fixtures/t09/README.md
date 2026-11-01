# T09 · football-data.org v4 离线样本

`synthetic_` 开头的文件是手工构造的测试数据。比赛和球队 ID、队名、日期、比分均为合成。
`synthetic_season.json` 含常规结束、加时、点球、延期、判罚和缺少常规时间比分的场次。
英超示例里的加时与点球行专门验证 v4 的常规时间比分字段。

`synthetic_window.json` 覆盖英超和西甲，UTC 日期窗口包含起日、排除止日。
`synthetic_http_error.json` 提供离线传输的错误响应体。真实采集验收另行记录来源和原件哈希。
