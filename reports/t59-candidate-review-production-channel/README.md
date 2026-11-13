# T59 候选核验正式处置通道验收

代码与离线验收完成；**线上验收尚未执行，待审查者触发**。本次没有修改正式数据，
没有合并 PR，没有调用体育接口、重跑模型或改写冻结产物。
固定副本中，哥伦比亚、巴拉圭、新加坡、阿根廷从 pending 变为 cleared；湘南海洋继续 pending。
这里的 cleared 仅代表在固定身份库中未发现重复，不是批准新别名或宣称模型适用。

## 固定输入和证据位置

- 实现基线：main `77d440669f2fb441ddc1850104050c48f7363d11`。
- 归档基线：`data/football-feeds@e901c14b2c3aac1496832cc98cb2ab7e3bd7aeb2`。
- 重新 inspect：2026-11-13 22:29:44.727402 北京时间 / 14:29:44.727402 UTC；
  1,024 个球队身份、35,713 场比赛。没有直接发布 T58 的旧副本。
- 计划：[`data/jc_candidate_reviews/2026-11-13-first-five.json`](../../data/jc_candidate_reviews/2026-11-13-first-five.json)。
  队列 SHA256 `667d3ad4debfd6be4b395fcd097c676067dc2afa28a4ac962d5f4bfc46f39f17`；
  身份清单 SHA256 `8059e7862c4d8f07ee0944f990e282192a3821d0378f3e93251e9527e10f328f`。
- [完整证据、日志、前后队列和日报](https://github.com/qw5563680/FOOTBALL/tree/data/t59-candidate-review-acceptance-20261113/acceptance/2026-11-13-t59)。
  详细文件在独立验收数据分支，正式 `data/football-feeds` 未写入。
- 本目录 `SHA256SUMS` 的 `evidence/` 相对上述验收目录，`repository/` 相对代码根目录。
  该目录的 `implementation-version.json` 绑定实现提交和文件哈希，`ci.json` 另记真实 CI 状态。

## 逐项实跑

| 任务验收项 | 结果 | 详细证据（上述验收目录内） |
| --- | --- | --- |
| 一.1：正式通道的本地等价操作 | 通过真实归档副本运行 CLI，4 条 cleared、湘南海洋 pending；四个审计文件全部进入清单 | `realcopy/summary.json`、`realcopy/apply/`、`realcopy/prepared/` |
| 一.1：恢复和历史截点 | 恢复、导出、再次恢复后，数据库 SHA256 相同，队列及四份审计逐字节相同；新日报 1 个，原日报截止 2026-11-13 18:26:22.717365 北京时间 / 10:26:22.717365 UTC 重放仍 5 个 | `realcopy/summary.json`、`realcopy/digest-before/`、`digest-after/`、`digest-historical/` |
| 一.2：错误计划拒绝 | 篡改 queue_sha256、队列新增合成候选，两次 CLI 都退出 1；两次均核对 2,655 个文件字节不变 | `realcopy/bad-hash/`、`realcopy/new-candidate/` |
| 一.3：正式发布与后续日报 | **未执行；由审查者触发并留下 Actions 证据，不能用离线结果替代** | 见下方执行步骤；此处没有生产成功链接 |
| 一.4：无关归档不变 | 原归档 2,655 个文件全部未改；处置副本只改变队列和清单，新增四审计；其余 2,653 个文件不变，其中 929 个冻结预测、报告、评分、账本等路径另核对 | `realcopy/source.SHA256SUMS`、`realcopy/prepared.SHA256SUMS`、`realcopy/summary.json` |
| 一.4：工作流及发布边界 | 新操作独立 job，体育请求 0；旧 cron、旧 job 步骤与依赖保持。计划已过期、远端在 fetch 前/首次 push 时前进均拒绝；核验模式不迁移旧归档属性 | `workflow/legacy-preservation.json`、`workflow/`、`channel/`；`tests/test_t59_candidate_workflow.py`、`tests/test_t59_review_publish.py` |
| 二：手工缺表按行降级 | T07 页面离线变体含未核准编号，快照保留 14 行，CSV 13 行，缺表证据归档；错误队名、既有绑定或比赛元信息冲突仍拒绝；缺两队时保留双方缺口 | `sfc-manual/`；`tests/test_t59_sfc_manual.py` |
| 三：可选偶发测试 | 未复现，根因未确认，未改该测试、未加重试或跳过；单项、插桩、Python 3.11 整文件 29 项通过 | `flaky/investigation.json`、`flaky/trace-result.json`、`flaky/python311.xml` |

数据库恢复前后 SHA256：`24b487130cc65594d510e239d906100758b812c448d3f8e645ad7ec46f54b645`。
首轮验收脚本误选早于既存日报的清单时间，正确触发禁止倒退检查；改用实际旧日报截点后通过，
另保留更早 10:10:46 UTC 的待核行校验仍为 5。没有更改日报防倒退规则或原报告。
首次日志保存在 `realcopy/initial-cutoff-rejected.log`。

最终全量测试、Ruff 和 CI 的实跑汇总在下方“最终检查”；详细 JUnit/日志在 `full/`。
内部复核曾发现“未知一队遮住另一队冲突”和“发布顺带迁移属性”的问题，均补回归后修复；
过程与复核结论保留在 `internal-review.md`，不替代 Claude 的独立正式审查。

## 复跑和正式验收

```bash
PYTHONPATH=src python scripts/verify-t59-candidate-review.py \
  --archive /path/to/e901c14-archive \
  --plan data/jc_candidate_reviews/2026-11-13-first-five.json \
  --output /path/to/new-t59-evidence
PYTHONPATH=src pytest -q
ruff check src tests examples
ruff format --check src tests
```

1. 审查者确认代码和当前归档后，在 Football cloud feeds 选择已审查的代码分支，
   `operation=candidate-review`，`options={"plan":"data/jc_candidate_reviews/2026-11-13-first-five.json"}`。
   其他输入默认即可；这个操作不运行采集、预测、记分板或日报。
2. 程序检查所选计划已提交且字节一致，再从最新归档恢复并核对两个哈希。
   如果候选或身份库已变，必须重新 inspect、核对证据、提交新计划。不能改哈希凑通过，
   也不能把本次验收副本或恢复包直接推到正式数据。
3. 通过后调用既有发布脚本；数据分支已前进时立即停止，不能 rebase 旧队列。
   核对发布步骤、`candidate-review-<run>-<attempt>` artifact、数据分支队列/四审计/清单，
   记录正式提交和 Actions 链接。失败日志明确区分“准备完成”和“发布成功”。
4. 下一次正常日报读取新队列，应显示 1 个待核；另用旧截点确认仍 5 个。
   实际 Actions 和日报链接回填本验收后，才能称线上验收完成。

## 影响与遗留

改动限于候选处置入口、审计导出恢复、发布基线保护、手工缺表降级、回归测试与说明。
首批影响的是 5 个队名提示，涉及来源观察 `jc:2042736`、`jc:2042726`、`jc:2042735`、`jc:2042751`；
球队 ID、比赛对应关系和模型输出不变。手工降级仅影响未核准编号的快照行，不为其创建身份。
不需要重跑模型，不补写过去的概率或比分，不调整 BET。

线上首次处置仍待任务单指定的审查者执行；湘南海洋的 1165 与 Shonan Bellmare 的 607
仍缺 T54 要求的可靠来源绑定、精确同场配对及明确批准，不能自动合并。
可选偶发测试缺少原始失败堆栈，继续保留“未确认”状态。

## 最终检查

- Python 3.12 全量 `2996` 项通过，0 失败、0 错误、0 跳过，耗时 143.752 秒。
- `ruff check src tests examples scripts/verify-t59-candidate-review.py` 通过；
  `ruff format --check src tests scripts/verify-t59-candidate-review.py` 通过。
- 全量测试同时覆盖原 shallow 用例；没有调整该用例或增加重试。
- GitHub CI 以本 PR Checks 与证据目录 `ci.json` 的实际结果为准；CI 不代表生产验收。
