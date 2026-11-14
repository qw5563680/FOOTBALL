# T68 方案阶段核查记录

**已完成绑定方案送审准备；T68 业务实现、正式绑定及每日开启尚未执行。**

任务：[T68](../../docs/tasks/T68-new-competition-team-identity.md)。
方案：[T68-identity-binding-protocol.md](../../docs/experiments/T68-identity-binding-protocol.md)。
这一版记录只证明方案的现状依据与代码基线检查，不将任务验收 2–7 写成完成。
方案批准后才开始实现，逐项真实上线验收由 Claude 在 Actions 执行。

## 固定版本与只读核查

- 代码：[`273008329b15fabe8b5dd0cdc1ed78f139dcc270`](https://github.com/qw5563680/FOOTBALL/commit/273008329b15fabe8b5dd0cdc1ed78f139dcc270)。
- 数据：[`e370db1d5cabe1cb33ddbfff3029c6d802bb8992`](https://github.com/qw5563680/FOOTBALL/tree/e370db1d5cabe1cb33ddbfff3029c6d802bb8992)。
- 检查时间：UTC `2026-11-14T11:30:29.019165Z`；北京时间 `2026-11-14T19:30:29.019165+08:00`。
- 读取 Git 固定对象；不恢复或更改正式数据库，不抓取体育接口、不使用正式 key、不触发 Actions。
- 正式别名与注册表零改动；原工作区及历史备份保留。此 PR 只新增方案、此记录和 SHA 清单。

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
| 待所有者决定的 official_catalogue 类别 / 涉及总表行 | 14 / 32 |
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
  这次是归档行的风险复核，没有运行新的训练、更没有自动判定同场。

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

## 代码基线实跑

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

## 改动、影响与待审

- 只增加 `docs/experiments/T68-identity-binding-protocol.md`、本 README 和 `SHA256SUMS`。
- 没有比赛数据、绑定、采集开关、模型或生产文件改动，无须重跑正式模型。
- 第 9 节将“旧18联赛完全不变”与“补早年球队/移除重复训练行”的冲突明确交审查。
- 14 类归类决定仍全部待所有者逐项批准；无法取得完整精确证据的球队继续待核。
- 内部可实施性检查指出，新赛事保存独立 APIF 行及 T63 时标证明目前受训练白名单限制。
  方案第5节已明确提出独立身份/保存适用范围，不能借扩大模型训练范围来实现数据保存。
  内部检查只完善方案，不代替 Claude 的正式审查，也不是实现验收。
- 实施验收、生产应用和连续三次线上链路均未执行；方案审过才继续。
