# T12 · 500.com 备用赛果源的 robots 合规整改

## 目标
T03 的备用赛果源 `C500JcHistory` 请求 `https://trade.500.com/jczq/?playid=271&g=2&date=YYYY-MM-DD`，
而该站 robots.txt 明确 `Disallow: /jczq/?`（2026-11-01 核对）。本任务让项目不再请求任何被禁止的路径，
同时保留备用源能力。

## 范围
1. `src/zucai/sources/c500.py`：`C500JcHistory.fetch_raw` 改为只请求根路径 `https://trade.500.com/jczq/`
   （T07 的 `C500FixtureBridge` 已采用此做法），不再带任何查询串。
   根页只含当前在售编号日及其相邻日的场次，因此该源的定位改为"近期备用"，`--source c500` 的 `--from/--to`
   超出根页覆盖范围时直接报错说明，不静默返回空。
2. `zucai fetch results --source c500` 的说明、README、`docs/DATA_SOURCES.md` 第 2 节同步改写，并写明 robots 结论。
3. `cloud_feeds` 若有调用 c500 备用查询的路径，一并改为根页或移除；当前自动归档只用 jc 官方源，需核对后在 PR 里说明。
4. 在 `tests/` 加一个静态检查：扫描 `src/zucai/sources/` 内所有 URL 字面量，断言没有 `trade.500.com/jczq/?`、
   `trade.500.com/zc6/index.php?expect=` 等 robots 禁止前缀。
5. fixtures：现有 `data/fixtures/c500_20261031.html` 是用禁止路径抓的，保留文件但在 `data/fixtures/README.md`
   注明来源与日期，新增一份从根页裁剪的样本用于测试。

## 不做
- 不接其他备用源。
- 不改 500 的 SFC 期次页逻辑（`/sfc/?expect=` 未被 robots 禁止）。

## 验收标准
- 静态检查测试通过；`grep -r "jczq/?" src` 无结果。
- 真实请求一次根页并回放入库，PR 写明覆盖的编号日范围与场次。
