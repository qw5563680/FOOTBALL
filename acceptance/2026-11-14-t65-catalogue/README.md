# T65 固定原件离线验收

本目录仅是独立验收证据，不是正式 feeds，不发布目录指针、不改比赛或模型。
固定代码基线 `b83d2cc7d975e41f418f59aad3341a7d1574f95a`；固定数据
`e5848e5d7de83a0cd024fa3e03e08cf6db6c7ee4`。
提交时的完整源码/测试字节见 `final-code-files.json`；实现分支
`codex/T65-catalogue-real-response-fixes-and-mappings`。测试时间同时记录于 JUnit XML 的 UTC timestamp。

## 结果和限制

- `final-tests.xml`：3367 项全量测试通过，0 失败/错误/跳过。ruff check、format check 通过。
- `apif/baseline-reproduction.json`：原 parser 因 id271/2026 的反向日期拒绝原件。
  `apif/full-replay-summary.json`：日期修复有效，但全原件继续因五组重复赛季拒收。
  `apif/duplicate-season-evidence.json` 保存 397/2018、276/2018、379/2020、402/2019、542/2021
  两个冲突版本的原值、原始字节位置/SHA 与规范化对象 SHA。这是未解决阻塞，不是假成功目录。
- `jc/final-replay-summary.json`：406 页、365 天、12 个月、4966 场、54 个观察赛事、
  113 条官方字典、20 个真实空日，complete、0 请求、0 错误。
  `jc/final-replay/` 包含完整重算结果及逐字节保留的原件/sidecar；原捕获时间与 replayed_utc 分开。
- `mappings/mapping-evidence.csv`：90 行官方/API 对照；`mappings/verification.json` 核验
  357 个来源引用、267 个 JSON 指针、双方场数及日期。69 个原候选中 68 个有接口候选，熊猫杯未找到。
  官方对照 56 行可提案、32 行有语义歧义、2 行未找到字典项；所有新增映射仍待独立审查。
- `mappings/owner-decisions.json` 将 32 个受影响行归为 14 项实际决定；不把无比赛观察的行误列为决定。
  `next-task-supporting-scope.*` 只是后续未核验清单，可能含女足、青年或地区赛事，不等于核准范围。
- `request-plans-comparison.json`：APIF 11+7+1+国家队、FDORG 10、FD 15 的有序配置和每日/回填计划字节一致。
- `regression/comparison.json`：同一原件恢复后 14 个数据库表完全一致；SQLite 66,744,320 字节且 SHA 相同；
  19 个模型的 read_history 输入和参数未改；小型离线每日 fixture 的请求与结果一致。
  原数据快照全部文件前后 SHA 相同。这里没有重新训练或评价预测质量。
- `internal-review.json` 是辅助内部复查，不代替 Claude 正式审查、真实权限 probe 或所有者合并。

## 复现

在实现分支安装项目的开发依赖，检出固定数据提交到独立只读目录后运行：

```bash
python -m zucai.catalogue_replay \
  --source-dir ../cloud-data/state/catalogue/43539673769-1 \
  --out-dir ../new-t65-replay --run-id t65-independent-review-1
pytest -q
ruff check src tests
ruff format --check src tests
```

`mappings/build_proposals.py`、`verify_proposals.py`、`regression/reproduce.py`、`worker.py`、
`request-plans.py` 保留实现时的具体路径和固定版本；其他机器先调整路径到对应独立检出，不能替换版本。
只用归档/fixtures，开发环境不提供正式 API 密钥。`SHA256SUMS` 与 `manifest.json` 可逐文件核对。

全目录 APIF 成功指针仍未验收；五组重复赛季如何处置等待明确范围决定。其后仍由审查者执行真实目录
probe、逐行批准映射并按 T61 规则回填，本次没有触发任何线上运行。
