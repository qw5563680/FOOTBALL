# T22 · API-Football 付费档接入：配额守卫、赛季范围与真实探测规则

## 背景
2026-11-02 UTC 23:01–23:10 之间，API-Football 免费账号被平台停用，所有请求返回
`errors.access: "Your account is suspended"`。平台客服给出的常见原因里，本项目命中「共享网络环境」：
请求全部来自 GitHub 托管 runner（共享、动态的云 IP）和开发用的云环境，平台的反滥用保护会连带触发，
与自身请求量无关（11-02 全天约 30 次，远低于 100）。所有者于 2026-11-03（北京时间）开通付费订阅，
按平台说明付费订阅自动解除免费账号的这类停用。

开通后在 T20 分支补跑 `backfill --source apif --leagues 3,848`（run 41592640152），六个赛季全部失败，
原因是 **`zucai.sources.apif_quota.DailyQuota` 把免费档的 100 次/日写死了**：
`observe()` 对 `x-ratelimit-requests-remaining` 只接受 0–100，超出即抛 `ValueError("API-Football 每日剩余配额响应头无效")`，
且发生在归档响应之前；`_read()` 的 remaining 校验、`before_request()` 的 `used >= 100` 硬上限、
`remaining` 缺失时的 `100 - used` 回退、`--reserve` 的 0–99 取值范围也都以 100 为前提。
付费档的剩余额度在数千量级，于是每一次请求都被本地守卫判为「配额状态无效」并丢弃响应。
在本任务完成前，欧罗巴 / 欧协联和九个亚洲北欧联赛的 apif 采集全部中断。

## 目标
1. 配额守卫适配任意套餐：每日上限以服务端为准，不再写死 100；守卫仍然保守、持久、可离线测试。
2. 在付费档下重新实测 3 / 848 的整季权限，把 2025、2026（以及 2021 若开放）整季回填进数据分支，补上 T20 留下的当季缺口；
   当季比赛与 jc 已有的 2026 欧战证据重叠，同一来源球队 ID 在历史赛季同样生效，对齐率应显著高于 T20 的 4–6%。
3. 补齐 T20 验收中因停用未完成的两项：apif 重复回填新增 0；每日增量 3 次日期请求全部成功。
4. 把这次事故的教训写成规则：正式 key 只在 GitHub Secrets，任何开发环境不得用正式 key 发真实请求。

## 范围
1. **配额守卫**（`src/zucai/sources/apif_quota.py`、`src/zucai/sources/api_football.py`）：
   - 每日上限 `limit_day` 来自响应头 `x-ratelimit-requests-limit`；若 `/status` 端点不计入配额（需验证并写进 DATA_SOURCES），
     每次运行开头调用一次，记录 `plan`、`limit_day`、`current` 到配额文件；两者都缺失时回退 100 并在摘要里标明「回退」。
   - `observe()`、`_read()`、`before_request()` 的所有 100 改为 `limit_day`；`remaining` 缺失时回退 `limit_day - used`；
     `--reserve` 允许 0 到 `limit_day - 1`，默认仍为 10；本地日请求硬上限改为可配置（默认等于 `limit_day`，
     环境变量或参数可下调以保护预算）。
   - 配额文件 `state/api_football_quota.json` 新增 `limit_day`、`plan`、`observed_utc` 字段；旧格式文件可读（缺字段按回退处理）；
     `verify` 对新字段的校验同步更新。响应头仍先于归档检查，但校验失败时**不丢响应**：归档后再报错，便于事后诊断。
   - 请求间隔保持 6.1 秒；不提高每分钟频率。
2. **赛季权限实测与配置**（`data/api_football/leagues.json`、`src/zucai/cloud_api_backfill.py`）：
   - 新增 `workflow_dispatch` 操作 `probe`（输入 `source=apif`、`leagues`、`seasons`，默认 `3,848` 与 `2021-2026`）：
     逐季只请求第一页，不入库，把 `http`、`errors`、`results`、`paging`、原件 SHA256 写入 artifact 和
     `state/audit/apif_season_probes.csv`。这是项目里**唯一允许**的真实权限探测途径，由审查者触发。
   - 探测结果回填到 `leagues.json` 的 `seasons_available` / `season_probes` / `current_season_accessible`，
     `verified_utc` 更新；九个国内联赛（98、292、188、113、103、244、119、253、169）同样探测 2025、2026。
   - `cloud_api_backfill` 的允许范围继续由 `seasons_available` 决定，不再把 2022–2024 当作上限。
3. **回填**：复用 T20 的 `backfill --source apif`，先 3 / 848 的 2025、2026（以及探测开放的 2021），
   再九个国内联赛的 2025、2026；沿用 T20 的身份核验（jc 当季证据可识别历史赛季的同一来源 ID）、jc > fd > fdorg > apif 优先级
   与幂等要求。PR 描述按赛事 × 赛季列入库场数、拒绝、疑似重复、对齐率（回填前 / 后）。
4. **每日增量**（`src/zucai/cloud_apis.py`）：三次日期请求保持不变；摘要新增 `plan` 与 `limit_day`，
   任何 `errors.plan` / `errors.access` 仍按 partial 处理并原样写出错误文案。
5. **规则与文档**：
   - `AGENTS.md`「任务规则」新增一条：正式 API key 只存在于 GitHub Actions Secrets；任何开发环境（含 Codex、Claude 的云端会话）
     不得用正式 key 发真实请求；权限探测只能通过 `operation=probe` 由审查者触发；测试一律离线。
   - `docs/DATA_SOURCES.md` 第 6 节：记录停用事件（时间线、平台给出的原因、恢复方式）、付费档生效日期、`/status` 实测、
     新的赛季权限表、配额守卫新规则。
   - `README.md`「云端采集」：配额说明改为按套餐、`probe` 操作用法；`docs/ARCHITECTURE.md`：配额文件字段。
6. **测试**（离线）：`limit_day=7500` 响应头下的 `observe` / `before_request`；响应头缺失回退；旧格式配额文件兼容；
   `reserve` 边界；`probe` 操作的范围生成与 CSV 输出；`errors.plan` 与 `errors.access` 的 partial 路径；`verify` 新字段。

## 不做
- 不训练模型、不改 `params.json` 与 P2 规则（T21）。
- 不改 fdorg / fd 的任何逻辑；不新增 3 / 848 和既有九个联赛之外的 apif 联赛；不访问赔率、事件、阵容、统计端点。
- 不提高请求频率（保持 6.1 秒间隔），不删除 `--reserve` 守卫。
- 不在开发环境发真实请求；不把 key 写进任何文件、日志或 PR。
- 不提交数据库、原件或全量 JSON 到 main；`reports/` 不新增内容。

## 验收标准
- 真实运行（审查者在 PR 分支触发）：`operation=apis days=3`：apif 三次日期请求全部成功，配额文件出现 `limit_day`、`plan`，
  摘要无 `errors.plan` / `errors.access`；`operation=probe`：3 / 848 的 2021–2026 与九个国内联赛 2025–2026 的权限表；
  `operation=backfill --source apif`：各赛事各赛季入库场数、对齐率、数据分支提交号；同范围第二次回填新增 0。
- T20 验收表里「相同范围再次回填（apif）」与「每日增量（apif）」两项以本任务的运行结果补齐，并回帖到 PR #30。
- `pytest -q` 全绿、`ruff check src tests examples` 与 `ruff format --check src tests` 通过；测试不联网。
- `docs/DATA_SOURCES.md`、`README.md`、`docs/ARCHITECTURE.md`、`AGENTS.md` 同步更新。
