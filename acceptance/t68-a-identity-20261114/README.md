# T68 A 段离线验收包

本分支只保存验收证据，不是正式数据分支，不修改生产。
产品代码基于 main `9d3d0540baa38ac183526d0ae7fbc7a053f43126`，最终源文件SHA在包内 `integration/a-rest-source-freeze.json`。
对应产品分支 `codex/T68-A-identity-and-quarantine`；产品PR中的报告提供最终提交版本。

实跑：4356项全量通过，Ruff通过；26套模型仅美职少指定12条待核来源，其余25套不变；恢复后26套完全一致。
207个导出内容文件一致。旧e370和后补head的afdf各两轮绑定回放：93已知/0相反、116诊断、严格0、旧候选138、再次新增0。
不含访问密钥、临时数据库、完整生产备份；原件已经存在于文中固定Git数据提交，输入清单保存路径、blob和SHA。
包含修前失败日志与各轮冻结记录，最终结论以 `integration/a-rest-final-validation.json` 为准。

- 下载：[`t68-a-evidence.zip`](./t68-a-evidence.zip)
- 校验：[`SHA256SUMS`](./SHA256SUMS)
- ZIP 内有逐文件 `FILE-MANIFEST.json` 和 `SHA256SUMS`，已逐成员校验。

正式绑定、daily开启、线上三次验收和B实现尚未完成；内部复查不代替Claude/所有者批准。

ZIP SHA256：`3e494e7d094f9d2b05beff2eb001a6d4421eb22827ab27c717b380565b5b281f`；大小：26785027字节；证据文件：709。
