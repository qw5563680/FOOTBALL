# T51 历史参考资料补充包

只补经核验的3场欧洲杯历史资料；现有16场可靠赛果保留。正式数据库、模型、预测和账本均未修改。

- 代码验收版本：43cda608c7857f1083edb4f365cf82fa428c5c5a
- 正式资料对照版本：67ffe55aecc7ca55b6b56120a54635227ea12208
- 原库版本：b0948cb558c927c357db4067eac5b37b96d0e4bb
- 校验：首次新增3，第二次0；6个月档3019行字节不变；ZIP恢复读回一致。
- 本地测试：2582通过，失败0；完整日志含原有第三方依赖弃用警告。

## 怎么看

先打开 supplement-3.csv、acceptance-summary.json 和 verification.json。每场官方链接在 supplement/supplement.jsonl 的 verification 中，完整计划在 inputs/plan.json。该CSV可用Excel打开。

## 怎样复核

解压到新目录，切换到上面的代码版本或包含同一实现的T51分支，使用已有项目Python环境：

```bash
PYTHONPATH=src python /path/to/unpacked/verify_acceptance.py
```

脚本只读取 inputs 并在解压目录验证第二遍新增0、导出恢复及固定3场事实，不联网、不用密钥、不触碰生产库。
全新首次重放可运行 README 中的独立 history_supplement 命令，将 --output 指向一个不存在的目录。

supplement目录为独立历史参考档，不能直接覆盖正式state/results、matches或球队对应关系。
历史可知时间为空；2026年取回的资料不能声称2025年的赛前预测已经使用过。
源库不提供射正、xG、角球等39项技术指标。本包不改变10场门槛或模型规则。

比赛：2024-07-05 葡萄牙—法国0:0（点球3:5另列）；07-09 西班牙—法国2:1；07-14 西班牙—英格兰2:1。三场均19:00UTC，中立场。
旧西班牙—荷兰2025-03-23保持90分钟2:2；新库3:3含加时，只记录差异。

inputs/results.csv与LICENSE-international_results来源为CC0开源库；官方网页仅引用链接和核验事实。此处不放入任何访问凭据。

[下载完整复核包](t51-history-supplement.zip) · [逐场3条表](supplement-3.csv)
