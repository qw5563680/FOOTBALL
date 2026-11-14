# T65 最终补强验收

本目录追加所有者合并PR136后批准的两处修复证据。源代码提交为
`4a7ea5775fdb771b3c51be472386c621a2ee0fd4`，任务修订提交为
`d6a52bbb15df4ed22f9c6b57ee2e0936c31ca861`。
正式报告位于代码分支 `reports/t65-catalogue-real-response-fixes-and-mappings/README.md`。
此前 `acceptance/2026-11-14-t65-catalogue/` 全部保留，895个原清单哈希重新核验通过。

## 结果与入口

- `catalogue/offline-replay/replay-summary.json`：真实e5848e5原件完整离线重放；1251赛事，8816原始赛季记录，8811规范化年份，5冲突和8806正常；6条警告。无体育请求。
- `catalogue/replay_full_apif.py`：重放脚本；原件SHA、离线latest、规划结果均随目录归档。
- `daily/summary.json` 与 `verify_daily_isolation.py`：62新增路由仅历史启用，普通每日11条不变；相同64行混合合成原件的旧/新14表、导入结果、审计与候选一致。
- `restore/comparison.json`：从固定e5848e5再次恢复，66,744,320字节SQLite的SHA、14表、19组模型输入和参数与b83d2cc基线一致；未重训模型。
- `internal-review/review.md`：额外独立脚本核查冲突组内结构校验、所有回填入口、旧指针兼容与每日坏行前后位置；不是Claude正式审批。
- `verification/full-tests-summary.json`、`full-tests.xml`：3392通过，零失败/错误/跳过；完整日志包含依赖弃用警告。
- `verification/source-commit.json`、`code-files.json`：354个源代码/测试/真实裁剪fixture文件的字节与受测Git提交一致。
- `authorization/`：实际生效的修订任务及旧交付报告。旧报告的“等待所有者”属于过程记录，已经被PR136的合并取代。

所有独立脚本将输入和输出路径写得明确。跨机器复跑时，将脚本中的 `/workspace/work/football-t65`、
`t65-fixed-archive` 和证据输出根目录分别替换为代码4a7ea577的独立checkout、数据e5848e5的只读checkout及新的空目录。
不要把输出指向生产data目录；离线重放已保留原捕获时刻并单独记录重放时间。
pytest只使用仓库fixtures；脚本读取固定归档、使用HTTP拦截器或下载器替身，没有使用正式密钥。

## 包含范围与哈希

`FILES-SHA256.json` 是本次实际发布文件的完整字节/SHA256清单（不包含清单自身）；其SHA在代码报告的
`SHA256SUMS-FINAL` 中固定。各领域原 `SHA256SUMS` 和失败日志原样保留，记录全部本地实跑过程。
八个可重新生成的小型临时SQLite不提交，见 `excluded-local-files.json` 的路径、大小、SHA和原因；
领域清单中对应这八条是本地文件证明，不是可下载文件。其业务内容已通过快照哈希/JSON和独立对照记录，
完整生产归档、原始SQLite和备份均未修改。没有密码或访问密钥。

独立目录的成功apif指针只证明重放/校验器逻辑。正式目录probe、真实账号季节权限、真实回填均未执行。
线上步骤须由审查者在代码合并后执行；T68逐赛事解决球队绑定和每日开启。本数据分支不发布生产修复。
