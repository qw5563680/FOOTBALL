# T09 · football-data.org v4 离线样本

`synthetic_` 开头的文件是手工构造的测试数据。比赛和球队 ID、队名、日期、比分均为合成。
`synthetic_season.json` 含常规结束、加时、点球、延期、判罚和缺少常规时间比分的场次。
英超示例里的加时与点球行专门验证 v4 的常规时间比分字段。

`synthetic_window.json` 覆盖英超和西甲，UTC 日期窗口包含起日、排除止日。
`synthetic_http_error.json` 提供离线传输的错误响应体。真实采集验收另行记录来源和原件哈希。

## 真实裁剪样本（北京时间 2026-11-02）

以下 `live_` 文件来自本账户在云端的带凭据真实 GET，均在提交前重新检查原始字节及 JSON 解码值没有 token 回显；没有将 token 写进 provenance。它们是裁剪件，不是完整赛季：

- `live_pl_season.json`：PL 2026 整季响应，保留一场常规结束和一场预定比赛。
- `live_elc_extra_time.json`：ELC 2024 历史响应，含真实 `fdorg:536448`，`fullTime=1:1` 而 `regularTime=0:1`；测试必须取后者。它不是合成英超加时场景。
- `live_window.json`：`PL,PD,SA` 的 9 天窗口 `[2026-10-28, 2026-11-06)`；原件 200，保留两场不同状态比赛。

`provenance.json` 记录每个裁剪件 SHA256、原件 SHA256、原始 URL/HTTP/UTC、原件行数及保留 ID。日期、球队 ID、比分未改写。原始大 JSON 仍不进仓库。

上文 `synthetic_*` 继续保留，用于真实裁剪件未覆盖的异常、延期、缺失比分和限流边界；不能将这些合成行计作实采样本。测试全部离线，不使用运行环境真实凭据联网。
