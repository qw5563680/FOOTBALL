# T52 失效编号对照阻断国家队映射（当日预测与任九被静默清空）

来源：2026-11-12 独立复核（ChatGPT）发现、Claude 临时副本复现。所有者确认优先级最高。

## 现象与证据

- 2026-11-11 竞彩采集恢复后，胜负彩 26163 期前 6 场（北京 11-13 开球的欧国联）被写入
  `mapping_evidence.basis = provider_fixture_and_team_ids`，指向 `jc:2042692`–`jc:2042697`；
  这些 jc 比赛在 `state/matches.jsonl` 中不存在（`grep -c '"jc:2042693"'` 为 0）。
- `src/zucai/sources/sfc.py` 中国家队通道受 `if fixture.league == NATIONS_LEAGUE and not bridge:` 限制，
  有对照即不再尝试 `national_team_ids_and_exact_kickoff`；`refresh_snapshot` 每次把旧对照原样喂回。
- `src/zucai/cloud_predict.py::open_national_fixtures` 只收 `mapping == "matched"` 的行，
  于是北京 11-13 日批次 `input.match_ids = []`，日报显示「该批次无未开球比赛」；
  同期任九不足 9 场、复式因 3^6=729 注超 64 注上限不生成。
- 这 6 场在 run 42798481499 / 42863020080 / 42950468933 的 11-13 批次中均有模型概率。
- 复现：`restore_feeds` 数据分支快照后对 26163 期调 `refresh_snapshot` 得 8/14；
  仅清空这 6 行失效对照后再调得 14/14（apif:1528958/59/61/62/63、apif:1545613），
  均经国家队编号、主客方向与精确开球时间校验。

## 范围

- 对照指向的比赛在库中不存在时，不得压制已验证的国家队通道；降级原因写入证据字段，原对照保留备查。
- 查明并修复 2026-11-11 采集为何只写清单/对照而未建立对应 jc 比赛记录（或说明这是预期并让映射正确处理）。
- 日报在「无未开球比赛」与「有比赛但未映射/未预测」之间给出可区分的文字。
- 用 26163 期真实归档做离线 fixtures（裁剪后放 `data/fixtures/`）。

## 不做

不放宽任何身份校验、不做模糊匹配或名称别名；不改模型、参数、BET；不回写或重算已冻结的预测、报告与账本；
不用正式 key 联网。

## 验收

1. 26163 期原件离线重放：14/14 映射，前 6 场证据基准为 `national_team_ids_and_exact_kickoff`。
2. 同一重放中北京 2026-11-13 日批次 `input.match_ids` 非空且含上述 6 个 apif 比赛。
3. 原已映射的 8 场 `match_id` 与模型概率逐字节不变。
4. 构造一条指向库中另一场真实比赛的错误对照，必须仍拒绝映射并报冲突。
5. 已冻结历史文件 SHA256 不变；全量 pytest、ruff 通过。
