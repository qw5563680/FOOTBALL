# T12 · 500.com robots整改验收

基线：main `d4acd27`。采集：北京时间2026-11-02 03:36（UTC 2026-11-01 19:36）。

## 合规与实现

实际读取 `https://trade.500.com/robots.txt`，普通访客规则明确禁止 `/jczq/?` 和
`/zc6/index.php?expect=*`，未禁止 `/jczq/` 根路径及 `/sfc/?expect=`。
本次仅GET robots和无查询串的根页，未请求禁止路径。整改后的C500JcHistory每批只请求一次根页，
不跟随重定向；范围越界或日期覆盖未知时整批失败，不再逐编号日拼历史查询。
`cloud_feeds.py` 的sync仅调用 `fetch_results(start, end)`，默认source=jc，无c500备用查询，未改动云端路径。
SFC逻辑未改。

## 真实根页与离线回放

根页URL：`https://trade.500.com/jczq/`，HTTP 200，原件131547字节。
采集时刻UTC：`2026-11-01T19:36:06.010429+00:00`（保存响应后记录）。

| 竞彩编号日 | 北京时间开球自然日 | 场次 |
| --- | --- | ---: |
| 2026-11-01 | 2026-11-02 | 5 |
| 2026-11-02 | 2026-11-03 | 9 |
| 合计 | 2026-11-02–03 | 14 |

原件没有 `data-type=bf` 的 `sbetbtn-ok` 精确已结算选项，解析赛果为0。
将原件和真实采集时刻保存为RawStore快照，离线回放至独立SQLite，调用
`fetch_results(date(2026,11,2), date(2026,11,3), source="c500")`：
finished/cancelled/assumed/unchanged均为0，库中teams/matches/results均为0，回放期间HTTP请求0次。
对同一快照请求11月1–3日明确报超出开球自然日覆盖范围，库无新增行。
覆盖来自全部比赛行，而非仅赛果、导航日期或编号日，不能保证边界日全天比赛完整。

**验收限制：真实非零赛果入库未达成。** 当前根页只提供在售/相邻日比赛，缺少历史比分玩法结算表；
没有将赔率、未结算值或“其他”解释为比分，也未执行JS或访问禁止路径补抓。
本次完成了真实根页请求和零场回放，不能声称验证了真实非零赛果入库。
旧归档结算样本经明确标注的离线组合测试可入库0:4，证明既有精确赛果流程保留；
该测试不是本次根页真实赛果的证据。后续若允许的根页出现结算行，仍需补做非零实证。

## 检查

- `.venv/bin/pytest -q -o addopts=''`：246 passed，401条现有penaltyblog/NumPy弃用警告。
- `.venv/bin/ruff check src tests`、`.venv/bin/ruff format --check src tests`、`git diff --check`通过。
- 独立只读审查核对请求、日期边界、fixture和全部哈希；发现的空响应异常已补失败回归并修复为明确覆盖错误。
- URL字面量AST静态检查通过；`rg -F 'jczq/?' src`无结果。
- 全部抓取测试被responses拦截，仅用fixtures离线运行。
- 新增根页fixture8575字节，保留14行data属性及表标记，删除脚本、广告、导航和赔率，无合成结算数据。
  旧 `c500_20261031.html` 未修改，来源禁令已补入fixtures README。

## 持久证据与哈希

代码PR的reports目录仅提交本README和 `SHA256SUMS`，不含原件、数据库或逐场JSON。
完整HTTP原件、sidecar、headers、robots与回放计数在私有数据分支独立提交：
[data/football-feeds · 143e329](https://github.com/qw5563680/FOOTBALL/tree/143e329805a2d57dca6415c667f5a182c50385ea/acceptance/2026-11-02-t12-robots)。
逐文件SHA256见清单；`data-branch/`前缀表示上述目录内文件，`code-branch/`表示代码PR内文件。
需要保持HTTP原件原字节，建议用 `git show COMMIT:acceptance/2026-11-02-t12-robots/FILE` 导出，
避免本地Git行尾转换。SQLite为隔离验证的临时库，不上传；可由归档原件重复回放重建。
