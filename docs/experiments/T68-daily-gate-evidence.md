# T68 每日开启检查：离线证据输入

本工具只读。`ready_for_review` 只表示证据可以交审，不是绑定批准、线上权限确认或开启 daily。
所有日期与分母固定到 `--cutoff`；30 日以此前完整北京时间自然日结束，12 月目录不扩展。

```bash
PYTHONPATH=src python -m zucai.identity_daily_gate \
  --archive /path/to/frozen-archive --cutoff 2026-11-14T16:30:00Z \
  --codes 98,113 --evidence /path/to/review/evidence.json \
  --fd-raw-dir /path/to/saved-fd-originals --out work/t68-daily-review.json
```

`--fd-raw-dir` 可省略。FD CSV 与同名 `.meta.json` 必须是已保存原件；其哈希、URL、抓取时刻、
行位置独立展示，不能冒充冻结清单成员。没有该输入时，已存 FD 指针记“原件未核实”，不算坏指针或 APIF 关联。
已存关系沿用同赛事、准确主客规范身份、开球差不超过三小时和90分钟比分一致的规则；
这与 Task1 新身份提案的更严格时刻要求分开。输出保留时差及 Task1 的组件诊断。
绑定使用官方数字 ID → 已证实项目 ID → 原有核准 APIF 条目，不以官方展示名认队。
`matches.linked` 按不同竞彩场次计数；`linked_by_source` 分来源计数，同场可有两个来源，不能相加冒充不同场数。

## 根文档与引用

`evidence.json` 是按 APIF 赛事编号组织的对象。每个赛事项包含：

- `provider_code`：字符串编号，与所查赛事相同。
- `archive_sha256`：冻结 `feed_manifest.json` 的 SHA256。
- `cutoff_utc`：与命令截止时刻相同的带时区字符串。
- 下述 `current_season_replay`、`recent_replay`、`risk_review`、`persistence`；有未绑定队时另需 `unbound_explanations`。

所有文件引用形如 `{"path":"relative/file.json","sha256":"<64 hex>"}`，路径相对 evidence.json 所在目录。
不接受绝对路径、越界或符号链接。原件还需同名 sidecar，校验 HTTP、来源 URL、哈希及真实抓取时刻。
API 日期原件须在该 UTC 查询日与目标北京时间窗口交集结束后抓到，且不晚于截止；
JC 补充页须在对应北京时间整日结束后抓到。不能用覆盖日前的空页证明没有比赛。

## 当季与最近30日重放

`current_season_replay` 包含：

- `report`：真实采集报告；所查当季 scope 必须完整，原件 SHA 对上，`parsed_matches` 与真实不同 fixture 数一致，
  所有 `apif_ingest.COUNT_KEYS` 字段明确为非负整数。暂缓、疑似重复和拒收不能默认为零。
- `before_manifest`、`after_manifest`：本次重放前后不同目录的实际导出清单。
- `replay_options`：同一保存运行的调用记录引用（见下）。在内存恢复 before，离线调用未修改的
  `import_apif_games`，逐项核对全部计数及 after 的核心表、source link/identity 记录。
  `linked_existing` 不等于状态变更：已吸收到 JC 的旧关系重放仍计 linked_existing，unchanged 可为零。
  `assumed` 等是补充计数，不能把所有计数相加当作不同场次数。
- `held_before`：此前完整暂缓 CSV；`held_before_manifest` 必须就是上述某份已物化导出，清单列出的哈希与 CSV 相同。
  只有表头必须有这个独立归档依据，不能再附一份自造小清单为自己证明零暂缓。
- 可选 `held_explanations`：逐 fixture 的 `fixture_id`、`reason`、`disposition: "still_held"`、`evidence`。
  这些条目仍未解决，继续阻止开启。其他季排除必须由相同编号及 SHA 的已核验原件证明。

`replay_options` JSON 必填 `run_id`、`provider_code`、`season`、`source_sha256`、
`before_manifest_sha256`、`after_manifest_sha256`、`cutoff_utc`、`formal_aliases_sha256`、
`retain_results`（布尔）、`assume_known_after_seconds`（原调用秒数或明确 null）、`imported_utc`（带时区），
以及 `invocation_evidence`（保存的原调用记录引用）。后者须匹配 run/scope/source/options/import 时刻。
审查者从已保存的运行日志/调用记录整理 companion，不能从原件抓取时间猜 imported_utc，
也不能把今天的默认值回填为历史配置；没有记录即 insufficient_evidence。
当前 fetch_apif/backfill 的真实调用使用 retain_results=True；False 仅适用于有记录的历史吸收模式。
imported_utc 不得早于源抓取或 before 导出，不得晚于 after 导出或截止；选项不豁免可知时刻校验。
不会联网、重写原归档或修改导入器；这是相同源/配置/状态的可复现核验，不是生产重放。

`recent_replay` 包含 `mode`（默认 `dates`）、`originals`（API 原件引用列表）、`official_originals`
（固定12月目录以外的每日 JC 补充页）、`results`、`before_manifest`、`after_manifest`。
日期模式包含窗口两端 UTC 查询日；重复或额外日期也拒绝。原件不能漏掉归档已知 fixture。
`results` JSON 包含 `fixture_ids`、`official_match_ids`、`raw_sha256`、`inserted`、`risk_ids` 和 `outcomes`。
`outcomes` 每个不同 fixture 恰好一项：`fixture_id` 与 `outcome`（inserted/updated/unchanged/linked/cancelled/suspected/rejected）。
计数和非风险结果须符合实际前后导出，物化后的赛事、主客项目 ID、时刻和90分钟比分还须符合原件。
`held_unbound` 不能作为成功重放结果；新增数不能只引用汇总声明。

`equivalent` 模式继续要求同一窗口的完整真实赛季分母、相同 fixture 集合，以及 `boundary_cases`
文件中的未绑定、同队同刻、反转、拒收、分页、跨日六类实际边界证据，不能只换少量容易成功的样本。
`risk_review` 是逐 fixture 的列表引用；每项须有 `fixture_id`、非空 `reason`、
`disposition`（quarantined/different_match/same_match）及 `review_evidence` 引用。
有风险 fixture 就不能提交空审阅列表。

## 未绑定原因与持久化

`unbound_explanations` 是列表引用，每项为 `official_team_id`、非空 `reason`、非空 `evidence` 引用。
编号必须属于该赛事未绑定集合，不能重复。解释只满足覆盖披露，不增加绑定数；
当前赛季原件及近30日 JC（含补充页、新官方编号、历史球队重新参赛）仍未绑定的队继续阻止开启，
不据历史解释创造审查例外。当前参与者另列分母，不改变固定12个月球队/场次分母。
Task1 的 identity conflict/ambiguous/current snapshot 矛盾独立阻止开启，保留原始证据。
已有 FD 关系的900秒时差诊断继续按既有±3小时唯一性核验，不能混作新身份提案批准。
已存 pointer 与有效 explicit link 合成同一关系图；同一连通组出现两个同源不同场次即阻止计数。
历史未关联比赛逐场披露具体指针缺口；关系冲突、一对多及未解决的当季暂缓仍阻止开启。

`persistence` 包含：

- `repeat_report`：第二次真实报告；总新增为零，并重新核验当季 scope 的字段、原件和逐 ID 状态。
- `replay_options`：第二次运行自己的调用记录，与自己的 before/after 绑定；不能复用第一次时刻或清单。
- `before_manifest`、`after_manifest`、`restored_manifest`：三个不同目录、不同清单文件的实际导出。
  第一份清单须与冻结归档相同。检查项目 schema、逐表行数、实际来源清单、审计/关系/隔离状态文件及内容。
- `before_repeat`、`after_repeat`、`restored`：三份 JSON 快照引用，字段恰为下面五项。

快照字段按导出内容复算：`coverage` 为赛事→来源→比赛数；`relations` 为排序后的已存 counterpart 对；
`quarantine` 为清单中 quarantine/cross_day 文件的路径→SHA；`training_selection` 为排序后的比赛 ID；
`immutable_sha256` 为非空不可变产物路径→SHA，并实际读取每个导出中的对应文件。
前三个库存与真实内容相符，三份快照完全相同；不能用三个相同的空 JSON 代替。

历史选择检查在内存数据库恢复实际核心表和相关可选关系/身份记录，使用本赛事实际来源集合与同一截止，
运行已有 `read_history` / `select_training_history`（含可知时刻、来源及隔离规则）。
跨日回执只从对应导出读取且须列入清单，并由现有 `load_decisions` 对照当前已提交的登记表及当前比赛快照；
不伪造或恢复历史批准，回执应用时刻仍受选择截止约束。不落盘数据库、不拟合模型、不证明预测概率或模型批准。
输出 `history_selection` 明列赛事、来源集合、截止与选择 ID。

测试中的完整入口包明确是离线合成 fixture：不替代真实线上采集/重放证据。

## PR143 审查补强后的字段与口径（2026-11-15）

- 球队仍核对固定 12 个月的官方编号。场次分母是 `matches.stored_current_jc` 加未被可靠同场关联吸收的
  `matches.pending_extra`；原归档中的 APIF `held_unbound` 和 `suspected_duplicates` 均须保留。
  `catalogue_never_stored_ids` 只作说明，不能拿它凑分母。当前赛季日期校验 `apif-raw.json`、`apif-meta.json`
  及 feed manifest，不把赛季编号直接当公历年。更新赛季尚未核对时仍阻断。
- `unbound_explanations` 每项须有 `official_team_id`、`reason_code=no_unique_approved_scoped_binding`、
  足以阅读的 `reason`（至少 8 个字符）及该官方队的已认证原件 `evidence`。可变球队表或任意短文本不能作原件证明。
- `recent_replay.replay_options` 是有路径/SHA的 JSON：`provider_code`、`retain_results`、
  `assume_known_after_seconds`（null 或正整数秒）、`imported_utc`、`formal_aliases_sha256`、
  `before_manifest_sha256`、`after_manifest_sha256`。每份日期原件按真实捕获时间重新解析并导入临时数据库，
  核对所有表、关系与逐场风险；自报拒收不能替代实际结果，导入不能早于捕获，导出不能晚于验收截止。
- 首轮和重复轮需要不同 `run_id`，重复轮从首轮保存状态开始。第二轮报告重新运行相同导入器核实。
  `immutable_sha256` 必须完整对应预测归档、`data/runs/` 和独立账本的真实文件，不接受 `state/matches.jsonl`
  等可变业务表替代。恢复后的所有核心表、来源统计、关系、隔离清单和训练选择都重新比对。
- 每个接口比赛的全部持有行都要同向、同赛事、时刻和常规赛果一致；同场另存但无关系的跨来源近邻单列待核。
  零绑定或零有效关联不会被文字说明变成就绪。所有输出仍为审查材料，不写每日开关。

最终复读补强：`equivalent.boundary_cases` 恰好六类，`fixture_ids` 必须覆盖该原件内目标赛事的全部编号，
包含风险对双方；`outcome` 只接受 `verified`，并非凭此文字通过。每项另附 `replay`：
`before_manifest`、`after_manifest`、`results`、`replay_options`（普通近期重放同一契约）。
`results` 含逐编号 `outcomes` 和 `inserted`。检查器在临时内存库重新执行每一份边界原件，
核对真实暂缓/疑似/拒收状态、前后保存表和关系；边界样本中的真实暂缓可以证明保护生效，不能算作补齐。
分页不完整的样本必须由与普通日期重放相同的完整页保护拒绝；全部编号结果为 `partial_pages_rejected`，
新增为零、两个独立保存目录的内容完全不变，此类样本不进入导入器。

冻结库存还包含实际 `scoreboard/`、`sfc/` 目录：先验证记分板清单、串关账本和胜负彩结算的原生校验，
再将整个目录逐文件纳入前后对照。账本被改写、漏存或多出文件均不能声称恢复相同。

保守限制明确披露：风险说明满足披露要求，不代表疑似关系已经解决；当前实现仍对实际近期疑似/拒收
以及未建立关系的跨来源近邻阻断。跨日回执会改变隔离判断，但本次不将其自动当作每日开启豁免。
这比任务第三节“0或逐条说明”的最低文字条件更严格，须由正式审查确认后续开启材料的接受口径；
本 PR 不批准任何真实决定，也不打开任何 daily 开关。
