# T03 · 历史赛果回填

## 目标
`zucai fetch results --days N` 能回填最近 N 天竞彩已完赛比赛的全场/半场比分，并把尚未入库的历史比赛补进 `matches`。

## 范围
1. `src/zucai/sources/sporttery.py` 增加 `SportteryResults(Source)`，接口见 `docs/DATA_SOURCES.md`（需验证），支持分页。
2. `zucai fetch results [--days 30 | --from YYYY-MM-DD --to YYYY-MM-DD]`：
   - 逐日请求，限速 ≥ 1 秒；
   - 比赛不存在时先 upsert `matches`（`status=finished`），再写 `results`；
   - 取消/延期的比赛 `status=cancelled`，不写 `results`。
3. 备用来源：`src/zucai/sources/c500.py` 的 `C500JcHistory(Source)` 解析 `trade.500.com/jczq/?date=`
   页面（需验证，注意 GBK 编码），`zucai fetch results --source c500`。
4. fixtures：两个来源各放一天的裁剪样本。
5. 测试：解析样本、分页逻辑、cancelled 处理、`c500` 的编码处理。

## 不做
- 不抓欧赔/亚盘历史。
- 不做队名跨来源对齐以外的任何事；对齐只调用 T01 的 `upsert_team`。

## 验收标准
- 真实回填 ≥ 60 天数据，PR 描述写明场次数和未匹配队名数量。
- `select count(*) from results` 与 `matches where status='finished'` 数量一致。

## 赛果可知时间 `available_utc` 的约定（路线 B，2026-11-01 定稿）

每条 `results` 必须显式写 `available_utc`，并新增 `results.available_source` 列标明依据，取值三选一：

| 取值 | 含义 | 何时使用 |
| --- | --- | --- |
| `published` | 来源页面或接口给出的发布时间 | 来源可核实时优先 |
| `captured` | 原始快照的 UTC 采集时刻 | 每日增量抓取，采集时刻即近似公布时刻 |
| `assumed` | `kickoff_utc + 固定延时` | 仅历史批量回填，且必须显式开关 |

规则：
1. `zucai fetch results` 默认不带开关时，缺发布时间一律记 `captured`。
2. 历史回填加显式开关 `--assume-known-after 3h`（延时可调，默认关闭）时，`available_utc = kickoff_utc + 延时`，`available_source = 'assumed'`；
   命令结束打印本次写入的 `assumed` 行数。
3. 已有 `published` 或 `captured` 的行，回填不得降级为 `assumed`；比分更正时若没有新的依据时间，沿用 T05 的触发器记更正时刻，`available_source = 'captured'`。
4. 加列要走 `init_db` 的"缺列即 ALTER"路径，旧行 `available_source` 为 NULL 时视同 `captured`，并同步更新 `docs/ARCHITECTURE.md` 数据模型。
5. 本节取代本文上方任何"固定延时只用于示例"的限制：固定延时允许用于真实数据回填，但必须带 `assumed` 标记，且 T05 回测报告必须披露其占比。
