# T68 A 段补强 · PR143 正式交回

实现完成，提交 Claude 正式复核；PR 保持草稿，不合并。代码版本 `29ffcffd4652bd19e5685204f1e3fc4d95f36483`，基线 main `8961ad01d6971651460759e79e6e38b627a9575e`。实跑日期2026-11-15，实际UTC/北京时间见各执行记录。

## P1–P6 结果与证据

| 审查项 | 实际处理与结果 | 证据 |
| --- | --- | --- |
| P1 重复创建 | 身份见证覆盖库内全部竞彩可信数字原件，包含固定目录之外。43个创建提案改为既有球队；300→257。乌拉圭414→321、巴西417→327、日本438→320、韩国430→318。head/schedule仅证明身份和时刻，不伪造比分。 | [43项原件复核](https://github.com/qw5563680/FOOTBALL/tree/75cd5aaffd2c736015cac1ea18f8076e200fc928/acceptance/t68-review-20261115/checks/resolved-identity-original-check.json)：64条数字对应，不用名称认队 |
| P2 开启检查 | 实际重执行当前/重复/近期导入；六类边界附件也重执行或触发完整页保护。真实账本、关系、隔离和训练选择进入恢复对照。补齐空证据、错误来源、假拒收、更新赛季和矛盾持有行反例。 | [验收总表](https://github.com/qw5563680/FOOTBALL/tree/75cd5aaffd2c736015cac1ea18f8076e200fc928/acceptance/t68-review-20261115/validation.json)、[证据契约](../../docs/experiments/T68-daily-gate-evidence.md) |
| P3 名称例外 | 精确到赛事＋官方编号＋接口编号，有原件SHA和所有者核准才生效；严格比赛与同源提案路径都检查范围。生产清单为空。 | `tests/test_t68_team_category_exceptions.py`、`tests/test_t68_final_review.py` |
| P4 可读清单 | 每赛事先给简表，再链接单队原件；同源依据独立展示。沙职5,145字节、HTML325,814字节。 | [球队入口](https://github.com/qw5563680/FOOTBALL/tree/75cd5aaffd2c736015cac1ea18f8076e200fc928/acceptance/t68-review-20261115/owner-review.md)、[沙职](https://github.com/qw5563680/FOOTBALL/tree/75cd5aaffd2c736015cac1ea18f8076e200fc928/acceptance/t68-review-20261115/owner-review/307.md) |
| P5 重复身份 | 独立导出10组数字身份冲突，不合并。含波鸿450/534、柏林赫塔、基尔、圣保利。 | [重复身份名单](https://github.com/qw5563680/FOOTBALL/tree/75cd5aaffd2c736015cac1ea18f8076e200fc928/acceptance/t68-review-20261115/existing-identity-duplicates.md) |
| P6 运行入口与回归 | sync/apis 在审计/导出前应用批准决定，逐项记录失效；空登记不改变训练选择。26套同环境新旧拟合及恢复后26套完全一致。 | [模型对照](https://github.com/qw5563680/FOOTBALL/tree/75cd5aaffd2c736015cac1ea18f8076e200fc928/acceptance/t68-review-20261115/checks/task4-model-comparison.json)、[恢复后对照](https://github.com/qw5563680/FOOTBALL/tree/75cd5aaffd2c736015cac1ea18f8076e200fc928/acceptance/t68-review-20261115/checks/task4-model-restored-comparison.json) |

固定提案A范围449个官方编号、564个赛事球队项：381项有候选、183项有原因；两次结果1,140文件逐字节一致，[对照记录](https://github.com/qw5563680/FOOTBALL/tree/75cd5aaffd2c736015cac1ea18f8076e200fc928/acceptance/t68-review-20261115/checks/final-proposals-comparison.json)。同一对应项前后均有候选的接口编号矛盾为0。独立原件指针核验1,701文件、7,438次检查（3,719个不同位置），未识别布局0。全部候选、82项类别仍待正式批准。

每日检查使用当季已存竞彩＋此前暂缓/疑似接口分母：瑞超绑定14/18、关联19/77（24＋53）；日职19/22、关联31/52（36＋16）。两者仍资料不足；目录未入库行只作说明。未打开daily。

## 实跑验证

- 全量 `pytest -q`：4,563项通过，412.946秒；运行前后全部代码哈希一致。[日志](https://github.com/qw5563680/FOOTBALL/tree/75cd5aaffd2c736015cac1ea18f8076e200fc928/acceptance/t68-review-20261115/checks/full-review-fixed.log) / [执行记录](https://github.com/qw5563680/FOOTBALL/tree/75cd5aaffd2c736015cac1ea18f8076e200fc928/acceptance/t68-review-20261115/checks/full-review-fixed.execution.json)。NumPy弃用警告保留，未伪称无警告。
- `ruff check src tests`、`ruff format --check src tests`通过；最终源提交与已验证文件哈希一致。
- 导出→恢复→再导出208个内容哈希一致；原始只读数据库SHA未变，候选、核心表、关系及审计内容一致。[记录](https://github.com/qw5563680/FOOTBALL/tree/75cd5aaffd2c736015cac1ea18f8076e200fc928/acceptance/t68-review-20261115/checks/roundtrip-summary.json)。实际记分板/串关/胜负彩账本另有原生完整性和改写/遗漏/新增反例。
- 完整真实模型库隔离14条、MLS选择4,739条，与既有同库基线相同。裁剪fixture的12条不是同一分母。
- 26套比较采用同一截止、输入、模型配置，零数值容差，逐项核对训练数据、参数、概率、比分矩阵；不代表预测有效性或命中率提高。
- 正式别名、模型参数、工作流、冻结预测和账本未改；不需要因此重跑正式预测。改动影响离线身份提案、每日开启材料检查，以及未来已批准跨日决定的生效/审计。

## 固定资料与异常说明

提案归档 `8b900f4cf0f9a1ca1afbafc9298e97b7cc7519f2`，原6,983个选择文件保持原样，另从同一提交读取原清单已声明的APIF疑似审计文件；模型输入源 `e370db1d5cabe1cb33ddbfff3029c6d802bb8992`，原库SHA `719f06b8cf800ccccc70f4eb158ecb092aa15d8da3ee8e29f98a4fca43abfb3a`。不是新生产备份。

模型截止2026-11-14 10:43:21.807840 UTC／北京18:43:21.807840；开启检查截止2026-11-14 16:30 UTC／北京11月15日00:30。没有回写历史采集/批准时刻。

旧环境模型产物与本环境最大概率差0.0003738218348070532（约0.0374个百分点、欧战）。原旧代码精确哈希保持不变，在本环境重跑后与新代码完全一致；未放宽容差。[差异明细](https://github.com/qw5563680/FOOTBALL/tree/75cd5aaffd2c736015cac1ea18f8076e200fc928/acceptance/t68-review-20261115/checks/model-environment-differences.json)。具体数值库/线程因素未进一步定位。第二次导出曾因临时磁盘满失败，原件和日志保留，无损压缩后另存`archive-2-complete`重试成功。所有失败均未当成成功凭据。

内部整分支复读的5项Important和1项展示问题已补永久反例并修正，修前报告与修后实跑同时归档；它不替代Claude正式审查。

## 尚待正式审查/所有者决定

当前gate保守阻断实际未解决的近期风险和未关联近邻；风险说明不自动解除、有效跨日回执不自动豁免daily开启。这比任务第三节“0或逐条说明”的最低文字条件更严格，明确交Claude确认，不暗中放宽。真实类别、球队绑定、Willem II等逐队例外、跨日决定仍须审核核准；连续三次真实线上采集由审查者在获准后执行。本轮没有体育HTTP、生产运行或生产写入。

## 交付

[完整证据目录](https://github.com/qw5563680/FOOTBALL/tree/75cd5aaffd2c736015cac1ea18f8076e200fc928/acceptance/t68-review-20261115) · [证据包](https://github.com/qw5563680/FOOTBALL/tree/75cd5aaffd2c736015cac1ea18f8076e200fc928/acceptance/t68-review-20261115/t68-pr143-review-evidence.zip) · [SHA256清单](https://github.com/qw5563680/FOOTBALL/tree/75cd5aaffd2c736015cac1ea18f8076e200fc928/acceptance/t68-review-20261115/SHA256SUMS)。证据提交 `75cd5aaffd2c736015cac1ea18f8076e200fc928`，追加在专用分支，原检查点保留。压缩包 40,498,108 字节、3,294个文件逐一哈希验证，无密码/密钥。SHA256：`6b3eb6706c6a112e8dcce16b73bb681b16a2d0a1bb75ed7967a8d9e5fbdf2633`。

下一步由Claude复核PR143，所有者决定是否合并及后续核准；Codex不自行合并或开启赛事。
