# T10 · API-Football v3 离线样本

所有测试离线，不读取运行环境真实 key 发请求。

真实裁剪（北京时间2026-11-02，原件UTC2026-11-01）：

- `live_season.json`：韩职2024整季原件232场中保留2场，包含真实AET，fulltime=2:1而goals=3:1。
- `live_date.json`：UTC 2026-11-01日原件1,187场中保留13场，包含真实AET/PST/AWD/NS/FT。
  状态边界行有未配置联赛，普通解析应过滤；专门状态测试显式传入这些真实联赛配置。
- `live_status_variant.json`：MLS2023原件526场中保留2场，核验真实Canc/CANC大小写差异。

ID、球队、日期和比分未改写。只留fixture身份/时间/状态、联赛、两队、goals与score。
`results` 改为裁剪行数，`cropped_from_results` 记录原件数，均≤20场。
`provenance.json` 记录原URL、HTTP、读取UTC、原件和裁剪件SHA256及保留ID。
每份原件和裁剪件均通过原始字节及完整JSON解码后的key回显检查；不记录key或请求头。

本轮已配置联赛的历史整季没有PST/AWD，不能伪造为真实历史行；这些真实状态放在日裁剪件中。
`synthetic_season.json` 明确为合成样本，在一个季样本中覆盖AET/PST/AWD等状态；
`synthetic_page1/2.json` 是构造的两页响应，真实27个整季均单页，不能称已实测多页。
`synthetic_ingest.json`、`synthetic_aliases.json`、`synthetic_errors.json`、
`synthetic_non_json.txt` 是入库、三状态、权限错误和不安全正文的离线构造数据。
