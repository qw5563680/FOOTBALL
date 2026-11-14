# T65 验收：目录真实响应修复与赛事映射提案

**部分完成。** 竞彩 406 页离线重算通过；APIF 坏日期修复通过真实裁剪样本，
但全原件另外发现五组冲突赛季，仍按任务单结构错误规则拒收。没有生成正式 APIF 成功指针。
全部新增映射仍待审查，不开启下载。没有新体育接口请求、正式数据写入、模型重跑或 PR 合并。

## 固定输入与证据

- 代码基线：`b83d2cc7d975e41f418f59aad3341a7d1574f95a`。
- 原件：[`data/football-feeds@e5848e5`](https://github.com/qw5563680/FOOTBALL/tree/e5848e5d7de83a0cd024fa3e03e08cf6db6c7ee4/state/catalogue)。
- 完整验收产物：[`data/t65-catalogue-acceptance-20261114`](https://github.com/qw5563680/FOOTBALL/tree/21d89aee54389c0881e56c2b97bfe9f4f93e1119/acceptance/2026-11-14-t65-catalogue)。
  本目录 `SHA256SUMS` 是该产物的逐文件清单；完整源码/测试字节指纹见产物 `final-code-files.json`。

## 逐条验收

| 项目 | 实跑结果与证据 |
| --- | --- |
| 1. 真实响应 | APIF 160 项相关离线测试包含 id271 错日期降级、字段/类型/重复拒绝；见 `apif/focused-tests.xml`。JC 完整406页重放得到 complete、365天、20空日、4966场、54观察赛事、113字典条目、0请求/0错误；见 `jc/final-replay-summary.json` 与 `final-cli.log`。 |
| 2. 旧行为 | 21行原核准数据、46个旧provider对象不变；APIF11+7+1+国家队、FDORG10、FD15计划有序字节相同，见 `request-plans-comparison.json`。同快照恢复14表、SQLite 66,744,320字节及SHA完全相同，19个模型历史输入与参数不变，见 `regression/comparison.json`。 |
| 3. 90行映射 | 官方56行可提案、32行有歧义、2行字典未找到；全部待审。原69候选的API结论为68候选、熊猫杯未找到；追加67条禁用候选provider及8个下级支撑联赛。357个来源引用/267个JSON指针均已核验，见 `mappings/verification.json`、`mapping-evidence.csv`。 |
| 4. 线上 | **未验收**。未触发Actions。APIF五组重复赛季继续阻止全目录入库；须明确隔离/拒绝规则后再由审查者probe，逐行核准及按T61回填。 |
| 5. 测试/文档 | 全量pytest **3367通过，0失败/错误/跳过**；ruff check与format check通过；见 `final-tests.xml`、`ruff-*.log`。README、ARCHITECTURE、DATA_SOURCES及旧配置旁说明同步。reports只有README与SHA清单，低于200KB。 |

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

内部辅助复查没有发现必须修问题，但不代替 Claude 的正式审查与所有者决定。
