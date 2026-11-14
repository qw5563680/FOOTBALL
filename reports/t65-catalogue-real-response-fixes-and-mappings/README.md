# T65 验收：目录真实响应修复与赛事映射提案

**部分完成。** 竞彩 406 页离线重算通过；APIF 坏日期修复通过真实裁剪样本，
但全原件另外发现五组冲突赛季，仍按任务单结构错误规则拒收。没有生成正式 APIF 成功指针。
Claude 审查后已核准62个新增 provider 身份，并修复中文赛事名唯一性；真实账号权限仍未知。
没有新体育接口请求、正式数据写入、模型重跑或 PR 合并。

## 固定输入与证据

- 代码基线：`b83d2cc7d975e41f418f59aad3341a7d1574f95a`。
- 原件：[`data/football-feeds@e5848e5`](https://github.com/qw5563680/FOOTBALL/tree/e5848e5d7de83a0cd024fa3e03e08cf6db6c7ee4/state/catalogue)。
- 完整验收产物：[`data/t65-catalogue-acceptance-20261114`](https://github.com/qw5563680/FOOTBALL/tree/c063ed83a13e62ec46c70493a424e6086907d9ba/acceptance/2026-11-14-t65-catalogue)。
  原始验收保留；审查后补充证据见同一数据分支的 `review-followup/`（精确提交见上述链接）。
  本目录 `SHA256SUMS` 是完整产物的逐文件清单；当前源码/测试字节指纹见 `review-followup/final-code-files.json`。
- 实现代码：`f281cfe70dad1a5f0ccb6ceb052c4739b8af0adb`；草稿 PR [#135](https://github.com/qw5563680/FOOTBALL/pull/135)。

## 逐条验收

| 项目 | 实跑结果与证据 |
| --- | --- |
| 1. 真实响应 | APIF 160 项相关离线测试包含 id271 错日期降级、字段/类型/重复拒绝；见 `apif/focused-tests.xml`。JC 完整406页重放得到 complete、365天、20空日、4966场、54观察赛事、113字典条目、0请求/0错误；见 `jc/final-replay-summary.json` 与 `final-cli.log`。 |
| 2. 旧行为 | 旧21整行、46个provider对象及90个官方对照对象逐字节不变。旧APIF11+7+1+国家队、FDORG10、FD15子集保持，新增62个经审查身份使普通APIF每日/回填路由变为73=旧11+新62；见 `review-followup/route-verification.json`。最终代码重新恢复SQLite仍为66,744,320字节及SHA `1f5a99bf…`，与main完全相同，见 `review-followup/restore-comparison.json`。原19个模型输入与参数比较见 `regression/comparison.json`。 |
| 3. 90行映射 | 官方56可提案、32歧义、2字典未找到的证据保留；14类归属问题继续待所有者决定。67个新增provider使用唯一中文名，审查原文列62个唯一ID已核准，另40/62/71/850/916仍禁用候选。53个明确一对一总表/API映射随审查核准，API结论18原核准/53新核准/18候选/1未找到。357来源引用/267原件指针核对保留，当前变更及复核见 `review-followup/reviewed-route-changes.json` 与 `route-verification.json`。 |
| 4. 线上 | **未验收**。未触发Actions。APIF五组重复赛季继续阻止全目录入库；须明确隔离/拒绝规则后再由审查者probe，逐行核准及按T61回填。 |
| 5. 测试/文档 | 最终全量pytest **3381通过，0失败/错误/跳过**；ruff check与format check通过；见 `review-followup/full-tests.xml`、`ruff-*.log`。初版3367测试记录原样保留。README、ARCHITECTURE、DATA_SOURCES及旧配置旁说明同步。reports只有README与SHA清单，低于200KB。 |

官方待决定清单由旧65条缩为32个受影响行，按同一问题归为14项决定，见 `mappings/owner-decisions.json`。
主要是附加赛分级、J1百年构想、亚冠改制、世预赛分区、泛化国际赛/杯赛，以及总表正赛和预选赛名称不一致。
无比赛观察与接口无数据分开，不用相似名称自动批准。321条国内支撑目录和23条其他成人国家队目录
仅列为后续原始候选（可能混入女足/青年/地区，需要后续男子一线队及赛事级别核查），没有批量加入批准范围。

## APIF 新发现的明确阻塞

原件包含 **397/2018、276/2018、379/2020、402/2019、542/2021** 五组同赛事同年份的不同记录。
此前 id271 日期错误先报错，掩盖了这些问题。`apif/duplicate-season-evidence.json` 保存十份原值及原始字节片段SHA。
逐赛事诊断能解析1246个赛事不等于整份目录通过，不能以丢弃五组的方式伪装成功。
目前保留严格拒收，是否增加可追溯隔离处理已经向所有者提出；未收到决定前不放宽。

## 操作影响

改动只涉及目录解析、离线重放、候选映射、证据保存和说明；不改变比赛赛果、已发预测、参数、账本、投注开关。
正式模型不需要为T65重跑。离线重放使用独立输出根目录；原件、捕获UTC和旧预测字节均保留。
新输出独立记 `replayed_utc`，不会声称历史预测已经看到后来取回的数据。
旧APIF配置的技术接口兼容读取仍在，文档没有误写成所有旧配置完全不再被读取。

Claude 对初版 `9032724` 有条件通过，两个必须修中：中文路由重名已在 `f281cfe` 修复并全量测试；
重复年份隔离方案明确等待所有者确认，仍未执行。审查文字写60个新身份，但其明细有62个唯一ID，
本实现按明确ID清单逐个核准；这不把目录赛季当成账号权限，也不替所有者决定14类赛事归属。
