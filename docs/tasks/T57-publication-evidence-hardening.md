# T57 发布证据加固：整体读不到时失败，历史证据落盘

来源：T55 审查（PR #115）的建议修 3、4，审查者已在真实归档副本上复现两种风险；
所有者 2026-11-13 确认开单。分支 `codex/T57-publication-evidence-hardening`，PR 标题以 `T57:` 开头。

## 背景

T55（已合并，main `f57a484`）要求复盘、记分板、纸面账户、日报只使用「发布时间早于开球」的冻结预测。
新批次在推送成功后写回执；历史批次的发布时间从数据分支完整 Git 历史里「首次加入该文件的提交时间」现场推导
（`src/zucai/forecast_publication.py` 的 `git_evidence` / `publication_times`）。审查发现两个遗留风险：

1. **整体读不到证据时静默清零**：`git_evidence` 对任何 Git 层面的失败
   （`forecast_publication.py:104` 的 `except (OSError, subprocess.SubprocessError, ValueError, UnicodeError)`，
   以及浅克隆、引用缺失、`.git` 不存在、目录不是仓库顶层等提前 `return {}` 的分支）只记一条 warning 就返回空。
   审查者把归档副本的 `.git` 移走后跑记分板：退出码 0，517 条记录全部按「发布时间未知」排除，
   正式样本 n=96 变 0，A–E 账户 0 注，日报从 619 行缩到 515 行——一份统计清零的记分板和日报会被当天正常发布。
   下次 Git 恢复后会自愈（账本字节未变），但当天的输出是错的，且没有任何 job 失败来提醒人。
2. **历史证据每次现推、不落盘**：`method=historical_first_containing_commit` 的证据不持久化。
   一旦数据分支被改写历史、或记分板/日报 job 的检出变成浅克隆（现在依赖 workflow 里的 `fetch-depth: 0`），
   全部旧批次会永久变成「发布时间未知」，等同风险 1 且无法自愈。

## 范围

1. **区分两类失败**。「Git 基础设施失败」（命令报错、非仓库、浅克隆、`published_ref` 缺失、远端配置缺失等
   `git_evidence` 现在整体返回 `{}` 的情形）与「个别文件推不出证据」分开：
   - 个别文件推不出：维持现状，仅该记录按 `publication_unknown` 排除并计数。
   - 基础设施失败，且仍有需要证据、又没有有效回执/落盘证据覆盖的记录：`zucai.scoreboard run` 与
     `zucai.daily_digest` 必须以非零退出码失败，错误信息写明原因（如「数据分支历史不可用：浅克隆」），
     不写出任何清零的 scoreboard/summary/日报文件，归档保持原样。若所有候选记录都已被回执或
     落盘证据覆盖，则无需 Git 历史，照常运行（这正是第 2 条的意义）。
   - `sfc_publication.py:157` 的同类回退与本条口径对齐（SFC 存好时间整体读不到时同样不得静默当作全部未知）。
2. **历史证据一次推导、落盘复用**。首次成功从 Git 历史推导出的 `historical_first_containing_commit`
   证据写入归档内一个独立状态文件（建议 `state/publication/forecast_history.json`，纳入 feeds 清单与
   `restore_feeds` 覆盖范围，参照 `state/alias_candidates/jc.json` 的做法）：
   - 读取时逐条校验 `candidate_sha256` 与当前原件字节一致才采用；不一致的条目作废、按未知处理。
   - 已落盘的条目不被后续推导改写（首个为准）；`post_push_confirmation` 回执优先级不变，永不被历史证据覆盖。
   - 落盘文件只增不改已有条目；写入走原子写，与现有发布事务/失败恢复路径兼容。
3. 文档：`docs/ARCHITECTURE.md` 的 T55 一节补充失败语义与落盘文件的格式、校验规则；若 workflow 行为有变
   （如允许未来去掉 `fetch-depth: 0`），同步说明，但本任务不要求改动 workflow 检出方式。

## 不做

不改模型、参数、BET；不回写冻结预测、旧评分、旧账本、既有回执；不改 `published_utc` 的口径与选版规则
（T55 已定）；不新增联网与正式 key；不改 workflow 的 cron 与 job 结构。

## 验收

1. 真实归档副本（固定数据分支快照）上模拟三种基础设施失败——`.git` 移走、浅克隆、`published_ref` 缺失：
   在尚无落盘证据时，`zucai.scoreboard run` 与 `zucai.daily_digest` 均非零退出、错误信息点名原因，
   归档内所有文件字节不变（不产出清零的记分板或日报）。
2. 同一副本上先正常跑一次（生成落盘证据），再制造上述失败：两个命令照常成功，记分板与日报输出和
   「Git 可用时」逐字节一致；篡改落盘文件中某条的 `candidate_sha256` 后，该条作废，行为回到第 1 条
   （有未覆盖记录则失败，而不是静默未知）。
3. 个别文件无证据的既有行为保持：只把一个批次标成未知的回归（T55 已有）结果不变，
   `publication_unknown` 照常计数，账本字节不变。
4. Git 正常、首次生成落盘文件的那次运行：除新增 `state/publication/` 文件与记分板/日报自身输出外，
   归档其余文件 SHA256 不变；记分板与日报数字与改动前逐字节一致（正常路径无行为变化）。
   重复运行幂等，落盘文件不再变化。
5. 全量 pytest、ruff 通过；`reports/` 新增 ≤ 200 KB；`restore_feeds` 后落盘文件可用（纳入清单校验）。
