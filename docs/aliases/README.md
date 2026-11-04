# APIF 别名审批

T23 先用七个 identity-only 联赛核验数字球队 ID。Claude 已完成元数据 probe 41612517821
与 identity 41612781709；数据提交 `68c1c2b` 中的 130 条 `auto_verified` 已带完整证据
原样复制到正式别名文件，交 Claude 复核。候选文件不会直接参与比赛导入。
所有者随后在 [PR #34 评论 6602500381](https://github.com/qw5563680/FOOTBALL/pull/34#issuecomment-6602500381)
批准原精简表全部 41 行，现已按表中文字及指定审批来源写入正式文件；表外条目未改。
另按第 9 项原样转入欧协联 400 / 632 的运行期 auto_verified 及完整 evidence，详见 DATA_SOURCES 第 6 节。
桥接联赛尚有 20 支未核验球队，包括 Brighton 51、Nacional 225，以及 18 支「仅 2025 季」球队；
后续清单仍需涵盖它们，对后者加上「仅 2025 季」批注。原因与证据见 DATA_SOURCES 第 6 节。

完成自动核验及正式别名审查后，从数据分支的本地检出目录生成第一批剩余候选：

```bash
zucai aliases proposals --provider apif --leagues 3,848,292,119,188,244,169 --archive ../cloud-data --out docs/aliases/2026-11-03-apif-proposed.md
```

第一批涵盖欧罗巴、欧协联 2026 季与 jc 同窗的未对齐球队，以及韩职、丹超、澳超、芬超、中超的
`proposed` 球队。运行日期改变时使用对应日期的文件名。输出文件必须尚不存在，避免覆盖所有者批注。
命令校验并临时恢复归档，仅读取；不会联网，不修改归档、候选、正式别名或数据库。

清单列出 APIF ID、来源名称、国家、2025/2026 已见出场数及 jc 参考名。国家没有来源记录时显示
“未知”；旧审计没有赛季字段的场次另列，不能算成完整赛季场数。jc 参考名来自同联赛三小时窗口，
可能有多个候选，明确标“未核验”，绝不自动填入规范名。
欧战旧审计仅按 2026-07-01 至 2027-07-01 的日期窗口初筛，仍标赛季未知；
已有正式同源 ID 桥接的欧战球队不重复列入审批。数据分支中尚未转入正式文件的候选不能充当该桥接依据。

所有者填写或明确确认“建议中文规范名”后，可在 PR 回复“批准”，或划掉不批准的行。
尚未确定中文名的行继续保留 `proposed`。实现者根据这次明确批准更新正式文件，例如：

```json
{
  "name": "来源球队名",
  "canonical": "所有者确认的中文规范名",
  "status": "approved",
  "approved_by": "所有者的 GitHub 账号",
  "approved_utc": "2026-11-03T00:00:00Z",
  "approval_pr": "https://github.com/qw5563680/FOOTBALL/pull/审批所在编号"
}
```

以上为字段示例，不是有效批准。实际 `approval_pr` 必须是本仓库的数字 PR 链接；时间必须为带偏移的 UTC。
仓库正式文件的新记录必须填齐三项审批来源。显式指定的旧版离线别名文件保持兼容，不补造批准人或时间。

Claude 复核正式别名后，先跑 reconcile，再跑欧战 2021–2026 和九联赛 2025–2026 回填及同范围重复回填。
确认数据完整后可只读评估“双场一致”方案：

```bash
zucai aliases evaluate --provider apif --leagues 3,848,292,119,188,244,169 --archive ../cloud-data --out work/identity-evaluation.json
```

评估会扣除现有严格图已经能确认的球队，列出额外数量和冲突；缺少完整季证据时标
`insufficient_evidence`。即使额外解决至少 20 支且零冲突，也只作为下一任务的讨论依据，
不会写入别名或放宽生产规则。第一批批准已落地；批准后的真实合并、回填及评估待 Claude 完成。
完整性以最近的 `apif_identity_scopes.csv` 范围记录为准；旧归档或最近失败/空响应不能借用更早的成功数据。

## 第二批：欧战常客的历史身份（T29）

由 Claude 用已经检出的真实数据分支快照生成新的文件，再交所有者逐行审批：

```bash
zucai aliases proposals --scope cups-history --archive ../cloud-data --out docs/aliases/2026-11-04-apif-cups-history.md
```

文件名使用实际生成的北京日，已有文件拒绝覆盖。输出注明 `feed_manifest.json` 的 SHA256，
只读恢复归档后统计仍为 `apif:team:<id>`、欧罗巴/欧协联合计至少 10 场的 ID，合计降序、同数按数字 ID 排序。
每个 ID 仅一行，出场数分列两项赛事，名称原样取 `state/audit/apif_cup_aliases.csv`；缺失就标缺失。
赛季范围按开球日期与既有 7 月换季规则分组，不代表完整赛季。对手示例只取归档北京日所在赛季及前一季、
已经有 jc 规范身份或正式生效 APIF/FDORG 别名的对手，最多 3 个；不列同窗比赛，不把未知英文球队当规范对手。
所有“建议中文规范名”格留空，原名、对手和参考节都只是人工核对材料，不用于自动绑定。

附录 `fdorg` 列出没有正式生效同源 ID 的临时身份，原名取 `fdorg_unmatched_teams.csv`，不套 APIF 的 10 场门槛。
末尾“没有 apif 别名的 jc 欧战球队”列中文名、库内比赛数及正式 fdorg 别名；proposed 不算已绑定。
已有正式 APIF 别名或持久 APIF 绑定的规范球队不列入该参考节。
同一个 APIF ID 若已在任意联赛正式生效但仍有临时历史，也会按行集合规则出现；已有批准可直接用于安全合并，
清单本身不产生新的批准。

清单与 reconcile 摘要统一显示：2025-07-01 UTC 起 jc 欧冠/欧罗巴/欧协联参赛球队的库内比赛
少于 20、少于 60 及其中 20–59 场数量；APIF/FDORG 临时身份数、欧战出场数；本次合并、重定向、冲突跳过数。
这是任务单基线样本口径，不是当前实时销售名单；少于 60 是累计数量，包含少于 20。
库内比赛数按 match_id 计数，含所有状态；出场按球队/比赛计数，不是去重后的模型训练场数。
只读清单的本次合并/重定向/冲突跳过均为 0，不冒充已做真实合并。

所有者在 PR 按“apif ID → 中文规范名”明确批准后，实现者才写入正式文件对应欧战联赛键，
使用上方 `approved_by / approved_utc / approval_pr` 来源字段；只改获批行，表外 proposed 保持原样。
若需批准 fdorg 条目，同样先获得明确的 ID/规范名批准，再写其来源文件，不能把 APIF 数字 ID 用到 FDORG。
Claude 已在 PR #45 审查中记录：用数据快照 `e05c7b0` 生成 200 行 APIF、17 行 FDORG 清单，
任务单 8 支球队各一行；这些是审查者的真实核验记录，不是开发环境运行结果。
所有者随后在 [评论 6616192600](https://github.com/qw5563680/FOOTBALL/pull/45#issuecomment-6616192600)
逐行批准 8 支 APIF、7 支 FDORG 球队。来源名和 jc 规范名均照抄该表，approved_by=`qw5563680`、
approved_utc=`2026-11-03T21:45:38Z`（评论时间）、approval_pr=`https://github.com/qw5563680/FOOTBALL/pull/45`。
批准原文对应字段、临时历史联赛和传播前哈希见离线 `data/fixtures/t29/approved_batch.json`。

批准写入正式文件后，先执行传播，再提交正式文件与改动清单：

```bash
zucai aliases propagate --archive ../cloud-data
```

命令只读数据归档，找出同一来源 ID 有临时历史、但本联赛尚无生效别名的条目，写回仓库的
api_football.json / football_data_org.json。它保留来源 canonical/status 及全部批准字段；
只替换 evidence 为同源 ID、来源联赛/状态、写入前正式文件 SHA256，逐条输出供 PR 审查。
原始证据来源优先于已传播条目，同级按联赛名稳定选择；冲突或批准字段不全会停止，不猜中文名。
没有历史或只有 proposed 的 ID 不传播；重复执行 0 条。归档、数据库及已有 active 记录不变。
本次已明确授权的丹超/400 → 哥本哈根沿用欧协联 auto_verified，并非新增的人工批准。
本批 15 个批准 ID 先落在有历史的欧战联赛，再从已存在的本地 `e05c7b0` 快照执行传播：
APIF 560 / 575 / 611 / 656 / 1026 / 13976 另有欧协联历史，新增 6 条 approved 传播记录；
合计 21 条本批 approved，加上丹超/400 一条 auto_verified，表外未批准 ID 不变。
这是只读归档、写仓库别名的本地操作，没有执行真实 reconcile；数据归档 manifest 与 106 个受校验文件保持原字节。

Claude 复核后再运行 reconcile、重复 reconcile、归档/冻结预测校验，并核对场数改善及下一次预测的训练支持。
若日常采集的非杯赛目标联赛仍缺正式生效条目，reconcile 会以“目标联赛正式别名未传播”跳过，
保持该联赛临时历史可继续由原导入器采集；补传播后再运行，不修改导入器来绕过授权。
