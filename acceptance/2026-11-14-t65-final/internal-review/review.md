通过

本记录是 Codex 内部独立技术复核，只覆盖 T65 最后两处修复，不是 Claude 正式审批，也不替代所有者合并决定。审阅对象是 `/workspace/work/football-t65` 的 HEAD `5c1384c0059553ace8ee58e4c1b7566f6120a715` 上未提交改动；需求基准为所有者已合并的 `d6a52bbb15df4ed22f9c6b57ee2e0936c31ca861` 中 T65 I.4、IV.4、验收5/6。复核时本地 `origin/main` 仍指旧版，因此直接读取该确定提交，没有依据旧 tracking ref 验收。

本轮没有尚未解决的必须修项。两项独立发现的测试回归均已由日采实现者修复，并由本复核重新运行确认：

- `tests/test_t61_provider_integration.py:121` 原先仍 monkeypatch 已移除的 `apif_command.provider_entries`，导致 AttributeError。现以真实 `daily_provider_config(provider, path=临时注册表)` 注入；原每日 `1003+5` 和显式配置全量保留断言仍在。
- `tests/test_t65_reviewed_routes.py:62` 原先仍要求新增62项进入每日范围。现明确每日新增0项、回填新增62项；同时保留未核验账号权限、目录年份、5个候选禁用，并增加 `daily=false/backfill=true` 断言。

独立检查结果：

- `src/zucai/cloud_catalogue.py:211` 在分组前验证每条原始记录，重复组内缺日期字段、错误日期类型、错误年份类型、错误 coverage 类型、错误嵌套布尔值、缺赛事名称、重复赛事ID均继续整份拒收。真实fixture的5个完整赛事对象与原件逐字段相同。
- `src/zucai/cloud_catalogue.py:237` 的冲突隔离与警告逐项核验：完整原件 SHA256 `173c44cd57bc04a0df30b56105dbe9ac077cdbb123e4e1145b7f620342f5d27a`，1,251赛事、8,816原始season行，规范化8,811年份；5组冲突为397/2018、276/2018、379/2020、402/2019、542/2021。所有原始冲突记录能由警告JSON指针在原件中逐项定位；原件路径与SHA准确。8,806个非冲突season与5c旧parser对同一批无冲突记录的结果完全相等，id271旧坏日期警告完全相等。未改原件。
- `src/zucai/cloud_catalogue.py:580` 的旧成功指针兼容：用5c旧parser生成不含新计数字段的规范化目录、原件、meta及latest，再由当前 `_previous_pointers` 核验，原指针原样保留、错误列表为空。
- `src/zucai/cloud_catalogue_backfill.py:69` 对冲突status或access均排除。独立人工授权测试字典只用于离线验证五个赛事；related/all、自动全选/显式赛事选取均得到52个正常scope、0冲突scope；显式只选冲突年份拒绝。真实 `collect_catalogue_backfill` 在resume与no-resume两种入口中，经下载器spy确认只下发52个正常scope，冲突区间在下载器前拒绝。既有 `api_backfill_scopes` 也拒绝这5个未核准赛事。测试没有请求网络、没有打开业务数据库。
- `src/zucai/league_registry.py:416`、`src/zucai/sources/apif_command.py:57`、`src/zucai/cloud_aliases.py:62` 使用同一每日配置。注册表对5c的变更仅为62个新APIF路由的daily true→false；backfill保持true。对b83基线，46个旧provider对象和21行旧核准mappings逐字段相同；对5c，全部90个competition行完整相同。每日普通11项与欧国联5特殊日期路径保留，helper返回独立配置副本。
- 直接使用真实业务入口 `fetch_apif(date=...)` 与 `write_candidates`：在T14离线fixture的既有 `apif:9901` 前、后分别加入新增赛事79的AWD坏行 `apif:293001`，14张数据库表、审计CSV、业务计数及候选决定均与没有该行时相同。候选原件SHA/sidecar先逐项核验真实来源，再仅在跨不同原件的业务比较中去掉这两项。显式 `leagues_path` 日期导入可继续使用调用者配置，独立测试强制默认daily helper抛错仍成功入库，证明未偷偷改读默认配置。
- 阅读同步后的 `README.md`、`docs/ARCHITECTURE.md`、`docs/DATA_SOURCES.md`：已说明冲突隔离、每日禁用、历史开启、未核验账号权限，以及离线指针不等于正式latest更新。

固定四问：

1. 未触碰冻结 predictions、parameters、run_log 或数据分支的 forecasts/models/scoreboard/sfc/daily/state/publication；本复核只读取产品代码/固定归档，写证据目录和可删除临时测试输出。未commit/push、未触发Actions、未使用正式密钥、未请求体育API。
2. 影响的是目录五个赛事年份和62条新路由的每日开关；没有生产比赛ID被改写。离线明确比较保留 `apif:9901`，排除新增测试坏行 `apif:293001`。旧46个路由对象、21行旧映射以及模型/概率代码不变。
3. 本轮没有新增预测计算、模型输入或赛前可用性逻辑。目录可见仍是 `access=unverified`，冲突为 `access=conflict`；目录不证明账号可读。显式历史导入沿既有assumed语义；不能用这次离线重放宣称历史赛前已知。
4. 改动点有独立边界脚本与55项窄pytest覆盖。修复后的两项旧测试已重新运行。相关Ruff、format、diff检查通过。全量pytest及全项目Ruff由root整合后运行，本记录不声称全量已通过；正式目录probe仍由审查者在合并后执行。本轮不涵盖T65此前全部映射逐行审批或正式账号权限实测。

复跑命令：

```bash
cd /workspace/work/football-t65
PYTHONPATH=src /workspace/work/t48-venv/bin/python /workspace/work/t65-final-evidence/internal-review/independent_checks.py
PYTHONPATH=src /workspace/work/t48-venv/bin/python -m pytest -q tests/test_t65_catalogue_conflicts.py tests/test_t65_daily_scope.py tests/test_t61_provider_integration.py tests/test_t65_reviewed_routes.py tests/test_t61_catalogue_backfill.py
```

脚本、输入、输出与实现者结果差异：`independent_checks.py` 为本复核独立编写；输入是只读的e5848e5完整APIF原件、仓库t61/t14/t65离线fixture与git 5c/b83文件；输出为 `independent-checks.json` / `independent-checks.log`。未复制全档或大型数据库。量化结果与领域实现者最终结论一致；独立新增发现仅为上述两项旧测试遗漏，均已关闭。`scope-files.json` 记录本轮所审代码、fixture、测试的精确SHA256，允许root随后独立补全全量验证/发布文档。
