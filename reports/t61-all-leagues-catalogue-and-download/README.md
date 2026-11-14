# T61 总目录、90项映射与下载通道验收

基线 main `b03e00f25be95e5d74fbf03fa71d8f0bed5981a0`；固定数据
`1657093073ea0e231cef476e1eac4e9ac58ebcb8`；执行日期2026-11-14（UTC与北京时间）。
这是离线实现与验收，不是全部90项已经联网补齐。

## 已实现

- APIF `/leagues`、FDORG `/v4/competitions` 各一次的只读目录probe；FD核实DIV静态目录。
  Actions hosted专用，归档原件、真实UTC/BJT、SHA、解析、服务分项失败，拒绝秘密回显。
- 90行来源名称全部无损映射；21行已有核准路线、69行仍候选。A/B/C范围可配置，
  `scope=all` 也不能绕过核准。当前没有将新的未知接口ID猜成 verified。
- JC过去12完整北京时间月逐日分页，按真实leagueId/名称计数，精确交叉对照90行。
  self-hosted `football-local` 手动执行；不同别名、名称碰撞、未见赛事均交所有者决定。
- 所有默认比赛适配器、每日采集和新目录回填读显式注册表，保留旧来源优先、未知球队来源ID、
  unmatched审计与核验流程。D2、候选、仅身份、仅索引不能进入普通历史下载。
- 回填从目录明确年份和已有历史成功证据生成计划；实际失败/分页计请求，默认每批最多500，
  APIF每日留500请求。成功后游标可续跑，原件SHA不符不能跳过；重新处理不重复新增。
- 目录证据与游标随feeds manifest保存/恢复，旧快照恢复会清除较晚证据，路径和符号链接校验。
  技术统计保持原19项范围，不因新增比赛映射而扩大采集。

## 实际覆盖与详细证据

独立证据分支：
[`data/t61-catalogue-acceptance-20261114`](https://github.com/qw5563680/FOOTBALL/tree/data/t61-catalogue-acceptance-20261114/acceptance/t61-all-leagues-catalogue-and-download)。

- [90项CSV覆盖表](https://github.com/qw5563680/FOOTBALL/blob/data/t61-catalogue-acceptance-20261114/acceptance/t61-all-leagues-catalogue-and-download/coverage/coverage.csv)
- [可读覆盖表](https://github.com/qw5563680/FOOTBALL/blob/data/t61-catalogue-acceptance-20261114/acceptance/t61-all-leagues-catalogue-and-download/coverage/coverage.md)
- `coverage/coverage.json`：来源分项、原库实际赛季/年份/场数、缺口；复现脚本 `scripts/verify-t61-coverage.py`。
- `regression/comparison.json`：14张表、17个旧模型训练输入、源归档SHA、恢复及离线每日耗时。
- `pytest.xml` / `pytest.log` / Ruff日志、`public-policies/`、逐项离线测试日志；本目录SHA清单用于校验。

原库38,282场比赛、37,617条赛果、1,040个球队，90行中38行有对应项目赛事标签；
其中17行有直接APIF来源赛季证据。相同项目标签不代表总表类别能唯一分开，
例如日职与J1百年仍保留归属歧义。来源赛季与日历年份分列，未知年份不猜。
本次新增真实比赛 **0**，没有新增正式体育API调用；覆盖表不把合成测试数据计算为新增。

## 固定归档前后核对

固定1657093只读恢复，基线和新代码各用独立数据库；14张表完整排序序列化字节和schema均相同，
DB完整文件SHA相同，17个旧模型的 `read_history` 输入哈希相同，`params.json` 不改。
原归档2,794个文件（含git工作树指针）、723,431,423 bytes前后哈希相同。
原有赛果、身份、关联、预测数据未改；没有改生产冻结或旧账本。

| 指标 | 基线 | T61副本 | 解释 |
| --- | ---: | ---: | --- |
| 数据库 bytes | 66,744,320 | 66,744,320 | 同一归档，无真实新增 |
| restore_feeds 秒 | 5.651 | 5.381 | 最终修复后同机器各一次观察，不宣称性能提升 |
| 小型离线每日API链秒 | 0.145 | 0.185 | 同5个mock请求，结果字节相同 |
| 真实每日链路秒 | 未实测 | 未实测 | 由审查者线上验收 |

离线每日链仅包含 t14 合成FD文件、FDORG窗口、三个APIF日页和归档步骤，不能代表完整生产耗时。
精确输入源码/配置哈希、逐表hash、17个训练输入hash和重现脚本都在 `regression/`。

独立辅助检查发现并已修复：FDORG无代码目录行阻断无关已核准赛事；新增ID身份审计/别名复核仍查旧表；
比赛范围动态扩张导致技术采集范围意外扩张。新增合成ID1001实跑下载、来源身份保留、审计、
断点、导出恢复、零请求续跑与零新增重放；旧联赛与正式别名文件未改。
辅助检查不代替Claude Code正式审查。

## 验收边界与线上交接

1. 完整离线pytest：**3213 passed、0 failed / skipped**（146.900秒），另最终新增路径专项7项通过；
   Ruff check src tests examples、format --check src tests通过。结果见同分支XML/日志；候选拒绝、预算、部分失败续跑、重复0新增、
   原件归档恢复、JC分页/歧义以及旧工作流路由均有实际运行测试。
2. 目录列出不代表账号可读。FDORG总目录只有currentSeason，不从numberOfAvailableSeasons倒推历史年；
   已有成功season_probes可补明确历史年。FD静态DIV未列历史范围，catalogue_seasons须核实后通过PR补齐。
3. 审查者先各跑一次API目录及JC交叉目录probe，核对原件，所有者决定两份目录对不上的赛事；
   审查者通过PR核准新增A/B/C映射。不要把候选状态直接批量改成verified。
4. 按核准路线分批运行 `backfill`，options中 `catalogue=true`；按真实权限补足每个可读季。
   失败/缺年份分别处理。游标表示该快照已经处理，进行中赛季全季刷新使用resume=false。
5. 合并后连续三次正常每日链路成功，查新联赛当日赛果入库，记录新增场数/DB体积/恢复及每日耗时。
   这些线上步骤尚未执行，不能把本次离线测试写成线上完成。

T60与T61从同一main分别开发；本PR不含T60参数或模型改动。两者的README/架构/来源文档均有新增说明，
后合并者应先合入最新main并保留两边内容，按原要求不rebase、不强推。
