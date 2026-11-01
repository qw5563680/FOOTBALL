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
