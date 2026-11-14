# T65 验收：真实目录修复、官方名称对照与映射

**代码和离线验收已完成，线上验收待审查者执行。** 完整 APIF 原件保留全部 1,251 个赛事，
坏日期与五组冲突年份可追溯隔离；竞彩 406 页重放得到 complete。
62 个新增核准路由只开放历史范围，每日导入及候选队列保持旧范围。
未使用正式密钥、未请求体育接口、未执行回填、未改生产数据或合并 PR。

## 版本与证据

- 主分支行为基线：`b83d2cc7d975e41f418f59aad3341a7d1574f95a`。
- 范围依据：[修订任务单](https://github.com/qw5563680/FOOTBALL/blob/d6a52bbb15df4ed22f9c6b57ee2e0936c31ca861/docs/tasks/T65-catalogue-real-response-fixes-and-mappings.md)。
  所有者已合并 [PR #136](https://github.com/qw5563680/FOOTBALL/pull/136)，批准冲突年份隔离与每日/历史范围分开；此前“等待决定”已被该决定取代。
- 固定原件：[`data/football-feeds@e5848e5`](https://github.com/qw5563680/FOOTBALL/tree/e5848e5d7de83a0cd024fa3e03e08cf6db6c7ee4/state/catalogue)。
- 当前实现：`4a7ea5775fdb771b3c51be472386c621a2ee0fd4`；草稿 [PR #135](https://github.com/qw5563680/FOOTBALL/pull/135)。
- [最终追加证据](https://github.com/qw5563680/FOOTBALL/tree/2f0114dc65a9c5478334b6fe024d11b301d549ae/acceptance/2026-11-14-t65-final)：
  完整重放、同输入前后对照、恢复哈希、内部复查、全量测试及逐文件清单。
  本目录 `SHA256SUMS-FINAL` 固定完整证据清单哈希，清单再固定每个发布文件。
- [前期证据原样保留](https://github.com/qw5563680/FOOTBALL/tree/90898b8312502b1420561eb44b0c25a6ff118cb2/acceptance/2026-11-14-t65-catalogue)：
  竞彩重放、90 行映射、原审查与旧失败记录。本目录原 `SHA256SUMS` 仍对应这批文件。
  旧报告与失败记录只作过程证据，不代表最终行为；最终验收以本报告为准。

## 七条验收逐项对照

| 任务标准 | 实跑结果 | 证据（相对相应证据根目录） |
| --- | --- | --- |
| 1. 真实响应与结构校验 | APIF id271 日期错误降级为警告；缺字段、错误类型、重复赛事ID及非JSON仍整份拒收。JC 406页、365天、20个空日重放 complete，4,966场、54观察赛事、113字典项、0错误、0请求。 | 最终 `catalogue/offline-replay/replay-summary.json`、`internal-review/independent-checks.json`；前期 `jc/final-replay-summary.json`、`jc/final-cli.log`。 |
| 2. 旧行为 | 旧46个provider对象、21行既有核准记录、90个官方对照对象逐字节不变。普通APIF每日仍11条，身份7条/索引1条/欧国联特殊路径及FDORG10、FD15保留。重新恢复SQLite SHA与基线一致，14表及19组模型输入/参数一致。 | 最终 `daily/summary.json`、`restore/comparison.json`；旧请求计划见前期 `regression/comparison.json`。 |
| 3. 90行映射 | 每行保留官方与接口结论及原件指针。API结论为18原核准、53新核准、18候选、1未找到；62个新增provider身份已按审查清单核准。40/62/71/850/916继续禁用。 | 前期 `mappings/`、`review-followup/reviewed-route-changes.json`、`route-verification.json`；最终 `daily/summary.json`。 |
| 4. 线上 | **尚未执行。** 独立离线目录的 `latest.json` 已含 `apif` 且通过正式指针验证器；生产 `latest.json` 未改。合并后由审查者运行probe，确认APIF指针与JC complete，再按T61规则分批回填。 | 最终 `catalogue/offline-replay/replay-summary.json`；无本次生产运行链接，不用离线结果冒充线上验收。 |
| 5. 每日链路 | 同一64行合成日期原件（旧J1一行、新62赛事FT行、新79一行AWD），旧main与当前代码的14表逻辑快照、导入结果、审计CSV、候选JSON逐字节相同；旧候选仍为2条。新增拒收行放前/后均不影响旧结果。 | 最终 `daily/summary.json`、`daily/verify_daily_isolation.py`、`internal-review/independent-checks.json`。 |
| 6. 重复年份 | 五组真实冲突全部保留原值、路径、SHA及JSON指针；五赛事其余52个年份仍可规划，冲突年份0个；显式冲突年份及resume/no-resume边界均拒绝。 | 最终 `catalogue/offline-replay/`、`internal-review/independent-checks.json`。这五赛事不在核准总表范围，52条验证只注入测试规划权限，不代表实际核准或可下载。 |
| 7. 全量测试/文档 | **3392 passed，0失败/错误/跳过**；ruff check及format check通过，345个Python文件格式合规，diff check通过。真实裁剪样本39,824字节；测试不联网。 | 最终 `verification/full-tests.xml`、`full-tests-summary.json`、`ruff-*.log`；README、ARCHITECTURE、DATA_SOURCES和旧配置旁说明已同步。reports仅README与SHA清单，低于200KB。 |

## 目录修复的具体结果

固定 APIF 原件为 3,090,059 字节，SHA256
`173c44cd57bc04a0df30b56105dbe9ac077cdbb123e4e1145b7f620342f5d27a`。
8,816 条原始赛季记录规范化为 **8,811 个年份：8,806 正常、5 冲突**。
六条警告是 id271 坏日期与 **397/2018、276/2018、379/2020、402/2019、542/2021** 五组冲突。
不从冲突记录中任选日期或覆盖率，不把冲突年份写成账号可读。
独立复查确认8,806个正常赛季与旧解析器逐项相同；重复组内七类结构变异仍整份拒收，旧规范化目录指针保持兼容。

当前已核准注册表生成696个离线规划范围，只验证规划逻辑；没有请求或导入这些范围。
目录列出年份不证明账号权限，`access_status=unverified/seasons_available=[]` 保持。

## 每日隔离与数据保留

62条新身份为 `backfill=true/daily=false`；普通每日11条、历史73条。
日期导入与候选生成共用 `daily_provider_config()`，历史导入和显式配置兼容路径保留。
不同原件的业务结果可以一致，但其真实原件SHA必须不同；没有抹除SHA来强求候选证据文件一致。
本验收的“逐字节一致”指**同一混合原件在旧/新代码之间**；清洁原件与混合原件另核对业务内容及各自真实来源。
所有日期混合输入均标明为合成测试，不宣称接口实测、球队已绑定或历史赛季已可读。

从固定归档重新恢复的SQLite为66,744,320字节，SHA256
`1f5a99bfad007629272d2272fc0dfb10d4a11b9e0dcad775bb22f7dbc4d5f44f`，与基线完全一致。
包含1,040球队、38,282比赛、37,617赛果、19,731赔率和100,030收盘赔率记录。
全部14表及19组模型输入/参数比较一致；本次没有模型重训或概率计算。
小型临时SQLite不发布，保留可重现脚本、规范化快照哈希和排除清单；生产备份及正式归档不变。

## 映射与后续事项

官方对照有56行可提案、32行语义歧义、2行字典未找到；32个受影响行归并为14类所有者决定，
主要涉及附加赛分级、J1百年构想、亚冠改制、世预赛分区、泛化国际赛/杯赛与正赛/预选赛名称差异。
这些是映射内容的保留候选，不再是上述两处代码修复的授权阻塞。
67个新provider使用唯一中文项目名，避免FA Cup、Super Cup同名混入同一赛事。
321条国内支撑目录及23条其他成人国家队目录仅作后续原始候选，未批量核准。

本分支只改目录、离线重放、映射与采集范围选择；没有修改模型、参数、历史预测、记分板或账本。
**正式模型不需要因T65重跑。** 新赛事每日开启交由T68逐赛事处理球队绑定。
内部复查及测试不代替Claude正式审查，PR继续保持草稿、未合并。
剩余工作由Claude复审和所有者决定合并；合并后审查者执行线上目录验收和获准的分批回填。
