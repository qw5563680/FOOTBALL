# T38 伤停入库、特征与离线实验验收

非购买建议 · BET 禁用。结论：**证据不足，尚不能判断加入伤停是否改善预测**。
实际完成九联赛 2025/26 的 3,221 场目标检查；三组模型的六列共同可评分场次为 0，所有概率与精度指标保留 null。
不能把本次结果写成“已提高准确率”或“证明伤停无用”。正式模型、参数、P2、BET、T28 均未改动。

## 冻结顺序与数据来源

- 协议先提交：`0b171fcf7e92150c9d46de69be73deedf6b11509`，实现与所有结果均在其后。
- 本次运行代码：`b96a5ed4efc2e1eabf3966ebb4d5b8f1868b2cbc`；实际生成 UTC：`2026-11-04T22:56:45.565263+00:00`。
- 协议：`docs/experiments/T38-evaluation-protocol.json`，SHA256 `23a92f44f6a9467048238dacc9d3d51476f2af0af0698461908a5ef0c0bc552b`。
- 冻结时 main `f7fa6c5` 尚无 T34b；按任务单备用方案，宿主为 T33 `dc-sot-v1`。
  候选加入两项伤停差值，消融去掉两项且共用候选完整训练掩码；全部系数拟合，不手设扣分。
- 原数据库来源：数据分支提交 `da17cbcb3633b084b5357991c1fa77d63398e346` 对应的已恢复 T33 快照，
  34,775 场比赛、34,138 场赛果、320,798 行统计、11 行事件、94 行阵容、10,730 条索引。
  原库 SHA256 `83944b6cd812d7c52f1db31b1d4c6c7b6f80eb3da6f5f7edc85ab7608d813c22`，原库未改动；只在副本增加新表并回放旧伤停原件。
- 伤停来源为已有 [Actions run 41877829928](https://github.com/qw5563680/FOOTBALL/actions/runs/41877829928)
  的 `apis-source-41877829928-1`，artifact ID `13346833909`；原 ZIP SHA256
  `fc26815f7eb97eae7e14cf514900f7968dc6f90c5bd7b3cadea9f3e3fd1754ac`。本轮只是下载已有附件，没有新请求体育接口、没有使用正式 key、没有触发 Actions。
- 实验副本 SHA256 `9ff688d7f0b5e942e04e5c609814657b797a022f1eb8dc084e18073a1aa41cbc`；只读运行前后数据库及两份技术/伤停审计 SHA256 不变。
  输入文件、依赖版本、所有源码哈希均在实验 ZIP 的 `input_manifest.json`。

## 旧原件离线入库核对

11 份 2026 季日常原件，16,585 条原始条目，去重保存 11,362 条观察版本：captured 222、backfilled 11,140；
关联已有比赛的 4,021 条，关联率 35.39%。未关联记录保留空身份和审计，没有按名字猜测，没有创建球队/比赛。
这些是存储版本数，不是伤员去重数、在售比赛覆盖率或精度证据。重复回放全部 11 页新增 0 行，数据库与审计字节不变。

| 联赛 / 供应商季 | 原件条数 | 去重存储版本 | captured | backfilled | 关联数 | 关联率 |
| --- | --- | --- | --- | --- | --- | --- |
| 丹超 / 2026 | 676 | 535 | 3 | 532 | 0 | 0.00% |
| 德甲 / 2026 | 1047 | 605 | 15 | 590 | 605 | 100.00% |
| 意甲 / 2026 | 1243 | 968 | 164 | 804 | 820 | 84.71% |
| 挪超 / 2026 | 1963 | 1481 | 0 | 1481 | 0 | 0.00% |
| 法甲 / 2026 | 794 | 563 | 5 | 558 | 563 | 100.00% |
| 澳超 / 2026 | 223 | 117 | 12 | 105 | 0 | 0.00% |
| 瑞超 / 2026 | 1540 | 1333 | 5 | 1328 | 20 | 1.50% |
| 美职 / 2026 | 6136 | 3659 | 0 | 3659 | 5 | 0.14% |
| 英超 / 2026 | 1080 | 710 | 7 | 703 | 627 | 88.31% |
| 荷甲 / 2026 | 921 | 641 | 5 | 636 | 631 | 98.44% |
| 西甲 / 2026 | 962 | 750 | 6 | 744 | 750 | 100.00% |

原件有重复玩家/比赛条目，原件数与存储数因此不同。当前旧快照索引覆盖不全，丹超/挪超/澳超等关联率低，
不能为改善数字改身份规则。日职、韩职、中超、葡超、欧冠/欧罗巴/欧协联为空接口的说明沿用已有实测，不是本次新增探测。
44 次历史季回填与连续 7 天赛前捕获覆盖，尚待所有者额度决定及 Claude 的真实验收。

## 独立实验口径

训练开始 2021-07-01；2024-07-01 至 2025-07-01 为月折选窗，2025-07-01 至 2026-07-01 为周折评价。
训练特征 backfill_replay，目标 strict_capture_time。历史名单赛前已知只是假设；库中历史季页始终 backfilled/available=NULL。
目标严格按实际捕获时刻，捕获等于或晚于截点拒绝。统一身份同场的特征截点取最早开球，上一场结束与标签边界取最晚开球。
周初拟合、开球取特征的设计不对称已预先披露，不能冒充当时在线冻结预测。

本次射正统计是在 2026-11 才捕获，晚于全部评价目标；新增伤停仅覆盖供应商 2026 季，没有 2025/26 的历史名单回执。
英冠、法乙及少量荷甲还有索引缺口。所有目标都缺少 strict 必需输入，因此 315 个评价折、180 个选参窗口折中的三组拟合
均明确记 `fit_not_attempted:no_eligible_target_inputs`，实际拟合次数 0；没有生成候选概率，没有用原模型概率补位。
各联赛 N=5/10 均无共同候选样本，N=5 只是协议预先规定的诊断默认值，不是得到验证的推荐参数。

## 六列全部指标

全目标分母为 3,221，六列真实共同样本为 0；0 表示样本数/覆盖率，null 表示无法计算的指标。

| 指标 | 射正宿主·原始 | 射正宿主·校准 | 加入伤停·原始 | 加入伤停·校准 | 去掉伤停·原始 | 去掉伤停·校准 |
| --- | --- | --- | --- | --- | --- | --- |
| 全目标有效 n / N | 0 / 3221 | 0 / 3221 | 0 / 3221 | 0 / 3221 | 0 / 3221 | 0 / 3221 |
| 覆盖率 | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% |
| Log Loss | null | null | null | null | null | null |
| Brier（三项求和） | null | null | null | null | null | null |
| 首项命中率 | null | null | null | null | null | null |
| 比分 Brier | null | null | null | null | null | null |
| 平局概率均值 | null | null | null | null | null | null |
| 平局实际率 | null | null | null | null | null | null |
| 平局偏差 | null | null | null | null | null | null |
| 10 桶校准 | 无可评分样本 | 无可评分样本 | 无可评分样本 | 无可评分样本 | 无可评分样本 | 无可评分样本 |
| 共同样本 n | 0 | 0 | 0 | 0 | 0 | 0 |
| 共同样本 LL / Brier / 命中率 | null / null / null | null / null / null | null / null / null | null / null / null | null / null / null | null / null / null |

均匀三分类仅为公式参照：n=3,221，LL=1.09861229、
Brier=0.66666667、并列首项按均分记 0.33333333；
不把它当成球队模型或实际随机投注成绩。jc 赛前 had 市场列 n=0、指标 null；没有使用 fd 收盘赔率替补。
市场读取发生在全部拟合/选窗流程结束以后，未进入 TEAM 输入、先验、校准或选参。

## 六组配对差与区间

预定 UTC 周整块 bootstrap，2,000 次、seed=3301、95% 区间；因配对样本 0，不执行虚假的抽样或编造区间。
以下每项均为 `no_paired_observations`，全量配对与六列共同配对分别保留。

| 比较（左减右） | 全量配对 n | 六列共同配对 n | LL 差 / 95% CI | Brier 差 / 95% CI | 命中率差 / 95% CI |
| --- | --- | --- | --- | --- | --- |
| candidate_raw-baseline_raw | 0 | 0 | null / null | null / null | null / null |
| ablation_raw-baseline_raw | 0 | 0 | null / null | null / null | null / null |
| candidate_raw-ablation_raw | 0 | 0 | null / null | null / null | null / null |
| candidate_calibrated-baseline_calibrated | 0 | 0 | null / null | null / null | null / null |
| ablation_calibrated-baseline_calibrated | 0 | 0 | null / null | null / null | null / null |
| candidate_calibrated-ablation_calibrated | 0 | 0 | null / null | null / null | null / null |

## 联赛与其他预先规定分组

每个联赛的六列 LL/Brier/命中率/比分 Brier/平局偏差及各配对区间均为 null，校准桶为空；全量数值与原因在 ZIP 的 summary.json。

| fd 联赛 / 评价季 | 全目标 / 有赛果 | 六列各有效 n | 共同 n | N=5 / 10 选参共同 n | 窗口及状态 |
| --- | --- | --- | --- | --- | --- |
| 英超 / 2526 | 380 / 380 | 0/0/0/0/0/0 | 0 | 0 | N=5；证据不足，仅按协议保留诊断默认值 |
| 英冠 / 2526 | 552 / 552 | 0/0/0/0/0/0 | 0 | 0 | N=5；证据不足，仅按协议保留诊断默认值 |
| 西甲 / 2526 | 380 / 380 | 0/0/0/0/0/0 | 0 | 0 | N=5；证据不足，仅按协议保留诊断默认值 |
| 意甲 / 2526 | 380 / 380 | 0/0/0/0/0/0 | 0 | 0 | N=5；证据不足，仅按协议保留诊断默认值 |
| 德甲 / 2526 | 306 / 306 | 0/0/0/0/0/0 | 0 | 0 | N=5；证据不足，仅按协议保留诊断默认值 |
| 法甲 / 2526 | 306 / 306 | 0/0/0/0/0/0 | 0 | 0 | N=5；证据不足，仅按协议保留诊断默认值 |
| 法乙 / 2526 | 305 / 305 | 0/0/0/0/0/0 | 0 | 0 | N=5；证据不足，仅按协议保留诊断默认值 |
| 荷甲 / 2526 | 306 / 306 | 0/0/0/0/0/0 | 0 | 0 | N=5；证据不足，仅按协议保留诊断默认值 |
| 葡超 / 2526 | 306 / 306 | 0/0/0/0/0/0 | 0 | 0 | N=5；证据不足，仅按协议保留诊断默认值 |

| 分组 | 目标数 | 六列各有效 n | 共同 n |
| --- | --- | --- | --- |
| season=2526 | 3221 | 0/0/0/0/0/0 | 0 |
| cold_start=true | 14 | 0/0/0/0/0/0 | 0 |
| cold_start=false | 3207 | 0/0/0/0/0/0 | 0 |
| cold_start=unknown | 0 | 0/0/0/0/0/0 | 0 |
| statistics_missing=true | 3221 | 0/0/0/0/0/0 | 0 |
| statistics_missing=false | 0 | 0/0/0/0/0/0 | 0 |
| statistics_missing=unknown | 0 | 0/0/0/0/0/0 | 0 |
| injuries_missing=true | 3221 | 0/0/0/0/0/0 | 0 |
| injuries_missing=false | 0 | 0/0/0/0/0/0 | 0 |
| injuries_missing=unknown | 0 | 0/0/0/0/0/0 | 0 |

冷启动按折前可知赛果中未出现过球队判断，不以伤停缺失替代冷启动。各缺失原因可以重叠，不能相加当独立比赛数。

## 归档与复现

全量逐场预测、缺失证据、选窗输出、折状态、输入清单与协议已压缩；独立补充包保存旧原件回放明细、运行日志和测试证据。
目标 Release 为 `experiments-202611`，**两份资产目前尚未上传**：运行环境对 `api.github.com` / `uploads.github.com` 的访问被拒绝，
可用 GitHub 连接器能创建 PR 和下载旧附件，不能上传二进制 Release 资产。没有写数据分支替代，也没有伪造 publication.json。
保持草稿，交由有 Release 权限的 Claude 环境复现并上传；新一轮 run ID/时间会改变 ZIP 哈希，必须记录该轮自己的 SHA256。

| 本地已生成，Release 尚未上传 | 字节 | SHA256 |
| --- | --- | --- |
| t38-injuries-local-20261104T225700Z.zip | 1283709 | `125ca24a76b27f48408cbaa0964e406d4f0d4149fa4e7ee511553253c85f10bd` |
| t38-ingestion-and-validation-local-20261104T225700Z.zip | 48424 | `ad5430f8470caeec5907e93dabb7ab7d8ff269ab9d15d739507721a7a47d0963` |

本工作区位置为 `work/t38-results/local-20261104T225700Z/`；main 仅提交本 README 与 SHA256SUMS，合计小于 200 KB。
ZIP 内部 SHA256SUMS 已逐文件校验。补充包包含 replay 脚本的本机路径供审计，跨环境使用以下命令与对应归档路径：

```python
from pathlib import Path
from zucai.cloud_feeds import restore_feeds
# 先将数据归档只读检出到该目录，并定位上述数据提交。
restore_feeds(Path("work/t38-source-archive"), Path("work/t38-repro/feeds.db"))
```

```sh
# 以下只从既有 GitHub Release 恢复行表，不请求体育 API。
python -m zucai stats restore-rows --db work/t38-repro/feeds.db \
  --archive work/t38-source-archive --repository qw5563680/FOOTBALL
```

下载上述已有 artifact，仅解出 stats-injuries 原件及同名 sidecar 到 `work/t38-existing-raw/` 后：

```python
import json
from pathlib import Path
from zucai.injuries_storage import ingest_injuries
root = Path("work/t38-existing-raw")
for raw in sorted(root.rglob("stats-injuries*.json")):
    if raw.name.endswith(".meta.json"):
        continue
    scope = json.loads(raw.read_bytes())["parameters"]
    ingest_injuries(Path("work/t38-repro/feeds.db"), raw,
        league_id=int(scope["league"]), season=int(scope["season"]), daily=True,
        raw_location="actions:41877829928/artifact:13346833909/" + str(raw.relative_to(root)))
```

`daily=True` 只用于这里已确认来源的日常旧原件；历史季回填不能这样标。

```sh
python -m zucai.predict.experiments.injury_command \
  --db work/t38-repro/feeds.db --out work/t38-review-run \
  --source-commit da17cbcb3633b084b5357991c1fa77d63398e346 \
  --run-id claude-t38-review --repository qw5563680/FOOTBALL \
  --release experiments-202611 --publish
```

命令禁止覆盖已有输出；上传采用现有 upload_verified + SHA256SUMS 校验，仅成功才写 publication.json。
不得把本机尚未发布的包写成已存在的 Release 下载链接。

## 离线验证与边界

- `pytest -q`：1923 passed、0 failed、0 skipped（134.199s）；仅环境 anyio 的弃用提示。
- `ruff check src tests examples`：All checks passed；`ruff format --check src tests`：232 files already formatted。
- actionlint、git diff --check 通过；全部测试的抓取输入来自 data/fixtures，所有 Release 请求用 mock。
- T38 fixtures 共 13,729 字节，真实原件仅裁剪 4 条（3,373 字节），来源和原件/节选哈希另存。
- 覆盖 captured/历史区分、相等与晚于截点拒绝、strict/replay 分离、缺失保留 null、上一场完整首发交集、
  同日唯一键及类型变更版本、同日冲突拒绝快照、重复原件幂等、事务回滚、真实整季名单的 CSV 回执分块。
- 覆盖新表外部分片、未关联行 round-trip、旧技术 manifest v1/v2 恢复与原清单不变、升级 v3、
  空表与覆盖率、CLI 与摘要、赔率隔离、同掩码消融、未来标签不改选窗、无目标输入不假装拟合、Release mock 上传校验。
- 复核发现的两个同场开球时间差边界均修复，额外 12 个回归用例通过；这不代替 Claude 的独立审查。
- 只扩展附属表与伤停实验，正式 Dixon-Coles、T33 原协议/代码、params.json、P2、BET、T28 未修改。
- 技术清单升级 v3 后旧写入程序不接受新清单，合入时须协调排队中的旧作业；旧 v1/v2 数据恢复仍兼容。
- 真实 44 次历史回填、连续 7 天覆盖、独立复算和 Release 实际发布仍待 Claude/所有者完成，未自行触发。
