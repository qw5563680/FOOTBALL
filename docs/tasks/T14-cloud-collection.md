# T14 · 四个数据源的定时采集上云

## 目标
让数据每天自动增长，不依赖任何人手动跑命令。四个来源分两条腿：
1. **API 腿**（`ubuntu-latest`，GitHub 托管机）：fd、fdorg、apif 的日常增量，key 来自 GitHub Secrets。
2. **竞彩腿**（自托管运行器，标签来自仓库变量 `FOOTBALL_FEEDS_RUNNER`）：沿用现有 `cloud_feeds sync`，因为竞彩官网对 GitHub 托管机返回 HTTP 567。
两条腿都从 `data/football-feeds` 分支恢复、导入、再把结果写回该分支，串行执行不互相覆盖。

## 范围
1. **工作流拆分** `.github/workflows/football-feeds.yml`：
   - job `apis`：`schedule` 每天 UTC 02:40 与 09:40（北京 10:40 / 17:40），`runs-on: ubuntu-latest`，无需仓库变量即可运行；
     `workflow_dispatch` 可单独触发并指定 `days`。
   - job `sporttery`：保持现有 `sync` 逻辑与 `vars.FOOTBALL_FEEDS_RUNNER` 门控；运行器可能是 Windows，步骤用 `shell: bash`（Git Bash）并用 `actions/setup-python@v5` 装 3.12；
     无运行器时整个 job 跳过而不是失败。
   - `verify` 模式保持现状，用于托管机校验归档。
   - 两个 job 和手动触发共用 `concurrency: write-football-feed-data`，不取消进行中任务；写回分支前先 `git pull --rebase`，冲突则失败并保留本次原件为 artifact。
2. **API 腿每次运行的内容**（`python -m zucai.cloud_feeds apis --archive ../cloud-data --days N`）：
   - fdorg：`--competition PL,ELC,PD,SA,BL1,FL1,DED,PPL,BSA --days N`（默认 3，7 天窗口一次请求）；
   - apif：`--date` 取 UTC 今天、昨天、前天三天（免费档只开放最近三天），每天 1 次请求；
     配额文件 `state/api_football_quota.json` 存在数据分支里，每次恢复、使用、写回；
   - fd：只在每周一的早班跑，拉已配置且已核验联赛的**当季**文件（`2627` 等，具体赛季代码由 `leagues.json` 或当前日期推导），带 `--assume-known-after 3h`；
   - 所有源的拒绝行、可疑重复、未对齐球队 CSV 追加写回数据分支 `state/audit/<source>_*.csv`；
   - 任一来源失败不阻断其他来源，但该次运行标记 `partial` 并在 Actions 摘要列出失败原因；网络错误保留原件为 artifact（90 天）。
3. **归档扩展** `src/zucai/cloud_feeds.py`：
   - `TABLES` 增加 `closing_odds`；`export_feeds` / `restore_feeds` 按 `source` 覆盖 jc、fd、fdorg、apif 全部行；
     月度 CSV 按开球月份分文件，`feed_manifest.json` 记录每个来源的行数与哈希；
   - 恢复后要能直接 `zucai train --source jc,fd,fdorg,apif`；
   - 新增 `restore_feeds` 的回归：从现有分支归档恢复 → 导入一份离线 fixture（每个来源一个）→ 导出 → 再恢复，表内容逐行一致。
4. **别名证据积累**：每次 API 腿运行后，对 fdorg 与 apif 各跑一次离线身份核验（T09 `fdorg_verify`、T10 `apif_verify`，输入为本次原件与恢复后的 jc 数据），
   把新产生的 `auto_verified` 候选写到数据分支 `state/alias_candidates/<source>.json`（含证据），**不改 main 上的正式别名文件**；
   正式别名由审查方定期从候选合并到 main。README 写明这条流程。
5. **文档**：README 的「云端采集」改写为两条腿的说明；`docs/ARCHITECTURE.md` 的模块表加 `cloud_feeds apis` 模式；
   新增 `docs/RUNNER.md`：在 Windows 电脑上注册自托管运行器的步骤（下载、`config.cmd --unattended --labels football-local`、
   `--runasservice`）、需要哪些软件（Git、运行器自带 Node，Python 由 setup-python 安装）、如何停用；
   仓库变量 `FOOTBALL_FEEDS_RUNNER` 应设为 `self-hosted,football-local`。
6. **测试**：`cloud_feeds apis` 用离线 fixture 和 `responses` 走通恢复 → 三源导入 → 导出 → 写回目录的全链路；
   配额文件跨运行持久化；fd 仅周一执行的判断；partial 标记；`closing_odds` 往返。

## 不做
- 不改任何来源的解析和去重逻辑。
- 不自动合并别名到 main。
- 不做自动训练和预测（那是 T15）。
- 不在工作流里硬编码任何 key；只读 `secrets.FOOTBALL_DATA_ORG_TOKEN`、`secrets.API_FOOTBALL_KEY`。

## 验收标准
- 真实触发一次 `workflow_dispatch` 的 `apis`：fdorg 与 apif 各成功一次，数据分支出现新提交，Actions 摘要给出各来源入库/链接/拒绝计数和 apif 剩余配额。
- 用 `verify` 模式在托管机恢复新归档并通过校验。
- `sporttery` job 在没有运行器时显示 skipped，不报错。
- PR 描述写明真实运行的 run ID、各来源计数、数据分支提交号。验收产物只提交 README 与哈希清单。
