# T68 方案与 A 段实施核查记录

**三条必须修已更新方案；A 段先交付官方开球时刻工具。正式绑定与每日开启尚未执行；B段设计已审，留待后续PR实施。**

任务：[T68](../../docs/tasks/T68-new-competition-team-identity.md)。
方案：[T68-identity-binding-protocol.md](../../docs/experiments/T68-identity-binding-protocol.md)。
所有者授权依据为 [PR140 条件审查](https://github.com/qw5563680/FOOTBALL/pull/140#pullrequestreview-5904223500)
及 PR139 任务增补。A 段实施计划见 [T68-A-implementation-plan.md](../../docs/experiments/T68-A-implementation-plan.md)。
[复审](https://github.com/qw5563680/FOOTBALL/pull/140#pullrequestreview-5904285942)要求本PR只保留方案与A1，其余A模块在 `codex/T68-A-identity-and-quarantine` 后续PR交付。
[所有者决定](https://github.com/qw5563680/FOOTBALL/pull/140#issuecomment-6877359853)已批准14项归类原则和B段候选集合，注册表状态字段仍留待B段PR更新。
本次工具验收不代表全任务2–7完成；真实原件先导、正式绑定核准与线上三次验收由 Claude 执行。

## A1 官方开球时间补充工具

固定源为 `43539673769-1`，北京时间2025-11-01至2026-10-31；62条路线按PR135批准ID固定，
不随以后daily开关或latest目录变化而缩小/扩大。406页、365天、4966场原件全部重新验证。

| 分母 | 实跑结果 |
| --- | ---: |
| 新路由同名目录场次 | 1,083 |
| 泛称赛事场次（仅补时刻，不建立B段关系） | 179 |
| 选定竞彩ID总数 | 1,262 |
| 已有同JC ID时刻可复用 | 146 |
| 仍需审查者先导/续取 | 1,116 |
| 当前固定输入证据冲突 | 0 |

146条复用记录均经manifest/表SHA、竞彩来源及结果提供方、双方唯一规范身份、赛事、北京日期核验；
143场完赛、3场取消。此证明只用于少发同场时刻请求，不批准新球队，不把结果核对时间冒充原件抓取时间。
早期实现曾因缺head原件列1262待取；补齐受限库内证据与原calculator赛程支持后，最终为1116待取。

已实跑：五场先导成功/失败停扩量、恢复后重复零请求、无效原件重试、可信编号/主客/日期矛盾继续待核、
多原件不同时间经导出和全新恢复仍保留冲突、源表SHA/schema/count/重复ID拒绝、固定窗口与62路线不漂移。
原件使用组合内容SHA命名；既有Windows运行器路径下最长对应sidecar215字符，不要求修改电脑设置。
新增代码不读写比赛数据库；新归档文件进入现有manifest白名单；不修改正式别名、模型或采集开关。

检查：定向149项通过；独立内部复查135项通过；**全量4103项，0失败/0错误/0跳过，238.910秒**；
`ruff check src tests`通过，`ruff format --check src tests`为382 files already formatted。
全量开始UTC2026-11-14T12:53:16.105902（北京时间20:53:16.105902）；
命令为 `PYTHONPATH=src python -m pytest -q -o tmp_path_retention_policy=failed --junitxml=work/t68-a/a1-pytest.xml --basetemp=work/t68-a/a1-pytest-tmp`。
第一次全量因磁盘不足中断，日志单独保留，不算通过；仅清理本轮测试临时目录与pip下载缓存，备份与已安装环境未删。

[完整验收包与SHA清单](https://github.com/qw5563680/FOOTBALL/tree/7acbbc4e8bd287fb3da6b5503d5209a7e99e97ca/acceptance/t68-a1-20261114)
包含完整测试输出、逐场清单、原件哈希、红绿测试及内部审查记录；ZIP为408441字节，SHA256：
`f3cf11a81d709f5b98f42b8966c58097acf14a8b0b52818039fe0acd5f22bf59`。
固定2305份输入逐一SHA复查未变；正式18键别名、赛事注册表、国家队配置和模型参数均与基线同字节。
本地源资料只物化本工具需要的部分；结果/身份回归库不是完整生产备份或完整归档恢复，未作这种主张。
内部复查不代替Claude正式批准；真实历史接口可读性仍待其先导验证。

## 固定版本与只读核查

- 代码：[`273008329b15fabe8b5dd0cdc1ed78f139dcc270`](https://github.com/qw5563680/FOOTBALL/commit/273008329b15fabe8b5dd0cdc1ed78f139dcc270)。
- 数据：[`e370db1d5cabe1cb33ddbfff3029c6d802bb8992`](https://github.com/qw5563680/FOOTBALL/tree/e370db1d5cabe1cb33ddbfff3029c6d802bb8992)。
- 检查时间：UTC `2026-11-14T11:30:29.019165Z`；北京时间 `2026-11-14T19:30:29.019165+08:00`。
- 读取 Git 固定对象；不恢复或更改正式数据库，不抓取体育接口、不使用正式 key、不触发 Actions。
- 正式别名与注册表零改动；原工作区及历史备份保留。下表保留方案阶段的固定库存依据。

| 项目 | 实际输出 |
| --- | ---: |
| 比赛归档行数 | 148,190 |
| 赛果归档行数 | 143,874 |
| 球队归档行数 | 6,913 |
| 新赛事路由（核准、仅 backfill） | 62 |
| 这些路由的 APIF 比赛行 | 89,535 |
| 正式 APIF 别名表已有赛事键 | 18 |
| 62 个新赛事在正式别名表中的赛事键 | 0 |
| JC 12 个月原件页数、成功 SHA 校验页数 | 406 / 406 |
| 不同 JC matchId | 4,966 |
| 具备 matchDate 的 JC 行 | 4,966 |
| 具备 matchTime 或其他时间字段的 JC 行 | 0 |
| 同一 JC matchId 能在库内找到 | 826 |
| 其余没有该库内行 | 4,140 |
| 四批归档 fixtures 原件数（不含 sidecar/汇总） | 728 |
| 固定基线带 owner_decision_required 的类别 / 总表行 | 14 / 32 |
| 美职同主客规范 ID、3h < 时间差 ≤36h 的 APIF/FD 完赛对 | 12 |

上述 826 场只是按 ID 的交集，不代表时间原件、身份类别、比分和唯一性全部合格。
不能用另外 4,140 场目录的日期猜时钟，也不能用 APIF 自己的时间来证明两源一致。
89,535 是来源比赛行，不是可训练比赛数；728 是文件数，不是独立赛季数。

真实样例：

- [JC 原件](https://github.com/qw5563680/FOOTBALL/blob/e370db1d5cabe1cb33ddbfff3029c6d802bb8992/state/catalogue/43539673769-1/jc/2026-10/2026-10-31-p1.json)
  `/value/matchResult/0`：`matchId=2042404`，`homeTeamId=849`、`awayTeamId=508`，`leagueId=62`，
  `matchDate=2026-10-31`、`sectionsNo999=3:0`，**无 matchTime**。
- [APIF 原件](https://github.com/qw5563680/FOOTBALL/blob/e370db1d5cabe1cb33ddbfff3029c6d802bb8992/state/catalogue/43565204451-1/backfill/api-football/2026-11-14/fixtures_061314731566Z_1eab1eb0780643098c538ecd425cd994.json)
  `/response/0`：fixture `557536`，德乙 `79/2011`，主队1320、客队183，
  UTC `2011-07-15T16:00:00+00:00`（北京时间 2011-07-16 00:00），`FT`，常规比分2:1。
  这里只核对字段存在，不将它与上面的竞彩样例配成一场，也未据此批准球队。
- 12 组 APIF ID 为 `695164,695206,695213,816780,816884,980819,981244,981246,1150767,1151233,1151235,1151237`，
  全部与 FD 同向规范 ID 相同、相差24小时、比分相同。和[主干审查记录](../../docs/reviews/2026-11-14-t65-review.md)一致。
  此方案阶段盘点只是风险复核，不自动判定同场。A段后续隔离实现另交新旧训练对照。

## 复算命令（只读，不生成绑定）

在能读取上述固定数据提交的仓库中运行以下脚本。数据提交若尚未取得，先正常 fetch 数据分支，
不要更换成后来新增数据的分母。所有输出仅为库存/结构核查。

```python
import collections
import hashlib
import json
import subprocess
from datetime import datetime, timedelta

code = "273008329b15fabe8b5dd0cdc1ed78f139dcc270"
data = "e370db1d5cabe1cb33ddbfff3029c6d802bb8992"

def raw(ref, path):
    return subprocess.check_output(["git", "show", ref + ":" + path])

def rows(path):
    return [json.loads(line) for line in raw(data, path).splitlines()]

paths = subprocess.check_output(
    ["git", "ls-tree", "-r", "--name-only", data, "state/catalogue"], text=True
).splitlines()
pages = [p for p in paths if "/43539673769-1/jc/" in p
         and not p.endswith(".meta.json")]
official, fields = {}, collections.Counter()
for path in pages:
    blob = raw(data, path)
    meta = json.loads(raw(data, path + ".meta.json"))
    assert hashlib.sha256(blob).hexdigest() == meta["sha256"]
    for row in json.loads(blob)["value"]["matchResult"]:
        mid = "jc:" + str(row["matchId"])
        assert mid not in official
        official[mid] = row
        fields.update(row.keys())
matches = {r[0]: r for r in rows("state/matches.jsonl")}
results = {r[0]: r for r in rows("state/results.jsonl")}
print("archive", len(matches), len(results), len(rows("state/teams.jsonl")))
print("JC", len(pages), len(official), len(set(official) & set(matches)))
print("time fields", {k: v for k, v in fields.items()
                       if any(t in k.lower() for t in ("date", "time", "kick"))})
registry = json.loads(raw(code, "data/league_map.json"))
aliases = json.loads(raw(code, "data/team_aliases/api_football.json"))
new = {v["config"]["league"] for v in registry["providers"]["apif"].values()
       if v["status"] == "verified" and v["backfill"] and not v["daily"]
       and v["config"].get("catalogue_seasons")}
print("routes", len(new), sum(r[1] == "apif" and r[2] in new
                            for r in matches.values()), len(aliases), sorted(new & set(aliases)))
decisions = [c["official_catalogue"] for c in registry["competitions"]
             if c.get("official_catalogue", {}).get("owner_decision_required")]
print("decisions", len({d["reason"] for d in decisions}), len(decisions))
print("fixtures", sum("/backfill/" in p and "/fixtures_" in p
                      and p.endswith(".json") and not p.endswith(".meta.json") for p in paths))
fd = collections.defaultdict(list)
for r in matches.values():
    if r[1] == "fd" and r[2] == "美职" and r[0] in results:
        fd[r[3], r[4]].append(r)
pairs = []
for r in matches.values():
    if r[1] != "apif" or r[2] != "美职" or r[0] not in results:
        continue
    for other in fd[r[3], r[4]]:
        delta = abs(datetime.fromisoformat(r[5]) - datetime.fromisoformat(other[5]))
        if timedelta(hours=3) < delta <= timedelta(hours=36):
            pairs.append((r[0], other[0], delta.total_seconds() / 3600,
                          results[r[0]][1:3] == results[other[0]][1:3]))
print("MLS", len(pairs), sorted(pairs))
```

## 方案阶段代码基线实跑（历史记录）

代码、tests、fixtures、配置全部与上面代码基线一致。命令在独立 worktree 中执行，使用已配置 Python 3.12 环境；
显式 `PYTHONPATH=src` 避免 editable 安装指向其他 worktree。

```text
PYTHONPATH=src python -m pytest -q --junitxml=<独立日志目录>/pytest.xml
退出码: 0
JUnit: tests=4023, failures=0, errors=0, skipped=0, time=219.183s
开始: 2026-11-14T11:28:38.219993Z / 北京时间19:28:38.219993

ruff check src tests
All checks passed!

ruff format --check src tests
380 files already formatted
```

测试仍有已有 `penaltyblog/NumPy` 弃用警告，未将 warning 当成失败或修进本轮方案。
完整本地日志位置：`/workspace/work/t68-proposal-checks/`；验收主张以本节实际输出为限。
此 PR 的 CI 会另行重跑，CI 未完成前不宣称通过。

## 当前改动、影响与待审

- 新增 `jc_catalogue_kickoffs.py`、离线fixtures和测试；扩展 `cloud_feeds.py`、`football-feeds.yml` 手动自托管入口与归档白名单；同步三份项目说明和协议。
- 本模块只影响上述1262场的待取/复用计划，没有正式比赛、绑定、采集开关或模型改动，无须重跑正式模型。
- 第9节验收解释已获批准：旧18联赛原条目不改；未来受影响训练行逐ID列明；其他输入/参数/概率相同；旧冻结与账本不改。
- 三条必须修均已反映：官方时间是前置步骤；B段按核准候选赛事集合设计自动唯一关联；删除新赛事独立付费行/时标范围。
- A段其余严格绑定提案、36小时隔离、采集分类和每日开启门槛在单独分支/后续PR继续实施；尚不宣称这些业务验收完成。
- B段设计与14类归类原则已通过；B段实现首先提交全部指针读取盘点与测试，具体球队绑定仍需核准；未新采体育数据、未发布生产数据、未开启daily。
