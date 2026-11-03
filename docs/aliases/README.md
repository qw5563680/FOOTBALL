# APIF 别名审批

T23 先用七个 identity-only 联赛核验数字球队 ID。候选 ID 仍需 Claude 在 Actions
运行 `operation=probe probe_kind=leagues` 确认，再运行 `operation=identity`。
实现者只把该运行产出的 `auto_verified` 条目带完整证据复制到正式别名文件，交 Claude 审查。
候选文件不会直接参与比赛导入；本次实现没有新增生产别名，也没有替所有者批准任何名称。

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

Claude 复核正式别名后，再跑欧战 2021–2026 和九联赛 2025–2026 回填及同范围重复回填。
确认数据完整后可只读评估“双场一致”方案：

```bash
zucai aliases evaluate --provider apif --leagues 3,848,292,119,188,244,169 --archive ../cloud-data --out work/identity-evaluation.json
```

评估会扣除现有严格图已经能确认的球队，列出额外数量和冲突；缺少完整季证据时标
`insufficient_evidence`。即使额外解决至少 20 支且零冲突，也只作为下一任务的讨论依据，
不会写入别名或放宽生产规则。当前真实身份核验、第一批正式清单与批准后评估均待 Claude/所有者完成。
