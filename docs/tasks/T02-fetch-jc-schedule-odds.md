# T02 · 竞彩在售赛程与赔率抓取

## 目标
`zucai fetch jc` 能把中国竞彩网当前在售的所有足球比赛及 had/hhad/crs/ttg/hafu 赔率写入数据库，并保留快照。

## 范围
1. `src/zucai/sources/sporttery.py`：`SportteryCalculator(Source)`，接口见 `docs/DATA_SOURCES.md` 第 1 节（需验证）。
   - `fetch_raw()` 请求接口并落盘 JSON。
   - `parse(raw)` 返回 `list[JcMatch]`，`JcMatch` 为 dataclass：`match_id`（`jc:` 前缀）、`jc_num`、
     `league`、`home`、`away`、`kickoff_utc`、`odds: list[OddsQuote]`（`pool`、`line`、`selection`、`price`）。
   - 让球玩法的 `line` 取 `goalLine`，胜平负 `line=0`。比分玩法 `selection` 形如 `2:1`、`5+`（胜其他）。
2. `zucai fetch jc [--date YYYY-MM-DD]`：抓取、落盘、按 `match_id` upsert `matches`，按快照时间追加 `odds`。
   同一场比赛同一玩法同一选项在 10 分钟内重复抓取不重复写入。
3. 把一次真实响应裁剪为 ≤ 5 场比赛后放入 `data/fixtures/sporttery_calculator_YYYYMMDD.json`。
4. 测试：用 fixture 测 `parse`（场次数、玩法数、让球线、比分选项），用 `responses` 测 `fetch jc` 端到端写库。

## 不做
- 不抓历史赛果（T03）。
- 不抓 500.com。

## 验收标准
- 真实运行一次 `zucai fetch jc`，PR 描述里写明日期、入库场次和赔率行数。
- 接口字段与文档不符时以实际为准并更新 `docs/DATA_SOURCES.md`。
