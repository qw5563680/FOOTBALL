# T23 离线样本

identity.json 是人工构造的联赛元数据和球队/赛程测试输入，不能充当候选 ID、权限或生产别名的真实证据。
HTTP fixture 使用既有 t20 原件裁剪样本的结构并替换为这些合成 ID 与日期；所有请求由 responses 拦截。

evaluation.json 是两个合成赛季的身份字段与 jc 对照：同队两场一致，但原严格图也能核验，额外解决数应为 0。
测试会在副本中加入时间窗口竞争、中文身份冲突及不完整范围；这些变体只用于离线评估，不写入生产别名。

apif_league_probes.csv 是 Claude 的 Actions run 41612517821 产生的 7 行真实元数据审计副本，
来自数据提交 d12e1ff（同时包含在 68c1c2b）。stage1_provenance.json 记录数据提交、run ID、
原审计/候选文件哈希，以及按中文联赛分组的 130 条正式别名规范化 JSON 哈希，验证 evidence 原样保留。
CSV 副本仅归一换行；来源字节哈希与副本哈希分别记录。这些文件可离线验证，不会触发 API 请求。

reconcile.json 是存量身份合并的纯合成样本：两个临时 ID 跨欧罗巴/欧协联及一个国内联赛，
同时包含未批准候选和 fdorg 临时行。fixture 中的 approved 仅用于测试，不是所有者对生产条目的批准。

stage3_cup_aliases.csv 从真实数据提交 `1d0152677977cd807dd4f49ff3985c8ed49319b5`
的 `state/audit/apif_cup_aliases.csv`（825 行）裁剪出全部符合第 9 项的两行：
欧协联 400 / 632，status=auto_verified 且 evidence 含 jc counterpart_id。
CSV 字段及 JSON evidence 原样保留，包括 seen_utc、sidecar；只裁剪行，不清洗证据。
stage3_approval_table.md 是评论 6601053083 第 4 节原文，stage3_approval.md 是批准评论
6602500381 原文；历史表内的“待确认”字样保留，批准以后一条评论及所有者本次指令为准。
stage3_provenance.json 登记数据提交、run ID、原 CSV 哈希、评论链接与裁剪副本哈希。
离线测试核对正式文件中两条自动核验的完整 evidence，以及 41 条批准记录的原文名称与审批字段。
