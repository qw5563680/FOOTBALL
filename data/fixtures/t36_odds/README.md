# 日报赔率时间离线样本

- `cases.json`：合成的正常更新、全部跳过、部分更新、旧归档缺时间、非法时间、原价去重、采集失败、其他 run 场景。完整日报测试结合 T25 冻结预测与账本样本；不连接网络。
- `closed_real_trimmed.json`：从 Actions run [42242081711](https://github.com/qw5563680/FOOTBALL/actions/runs/42242081711) 的 `sporttery-source-42242081711-1` artifact 裁剪。仅留首个营业日的前两场，保留两场原始字段、顶层响应状态与 vtoolsConfig；移除其他比赛和无关全站列表。不是完整 48 场列表。两行仍含 had 数字，但 sellStatus=2、单关/过关开关为0，必须保持跳过。

原件：`raw/sporttery-calculator/2026-11-06/calculator_152605030882Z_a312705024c445f0b95d69f47b93a8a4.json`。
原件捕获 UTC：`2026-11-06T15:26:05.030882Z`；HTTP 200。
原件 SHA256：`d9e14edf6ffc1b18753a221bf69598de9d06061b611ae1bf9803dc8780a70b68`。
本样本仅验证在售筛选不被放宽，不推断停止销售的原因或重新开售时间。
