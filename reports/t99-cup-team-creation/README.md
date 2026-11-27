# T99 11 支欧战球队数据回放

固定使用数据分支 `b2556ba8f2485620c2b27c761b1b639f33310ce3`。原流程和第二阶段均运行 `identity_canonical plan --codes 3,848`。第二阶段明确拦截 HTTP，输出写入归档之外，`production_writes=0`。核对前后 Git 工作区以及清单、球队表、比赛表、赛果表和五份球队列表证据的 SHA-256 均一致。[正式探测回执](https://github.com/qw5563680/FOOTBALL/actions/runs/45976356364)

| 竞彩官方 ID | API-Football ID | 球队 | 原件中的国家 | 球队列表赛事 | 原流程 | 第二阶段 |
| --- | --- | --- | --- | --- | --- | --- |
| 215 | 254 | 哈茨 | Scotland | 3, 848 | 待核，14 个联赛 | 待核，欧冠 9 支占位球队 |
| 709 | 397 | 中日德兰 | Denmark | 3, 848 | 待核，14 个联赛 | 待核，欧冠 9 支占位球队 |
| 2298 | 406 | 奥胡斯 | Denmark | 3, 848 | 待核，14 个联赛 | 待核，欧冠 9 支占位球队 |
| 318 | 598 | 贝红星 | Serbia | 3, 848 | 待核，14 个联赛 | 待核，欧冠 9 支占位球队 |
| 326 | 608 | 斯海杜克 | Croatia | 3, 848 | 待核，14 个联赛 | 待核，欧冠 9 支占位球队 |
| 287 | 617 | 帕纳辛纳 | Greece | 848 | 待核，14 个联赛 | 待核，欧冠 9 支占位球队 |
| 2182 | 664 | 阿拉木图 | Kazakhstan | 3, 848 | 待核，14 个联赛 | 待核，欧冠 9 支占位球队 |
| 2208 | 667 | 红色小鬼 | Gibraltar | 3, 848 | 待核，14 个联赛 | 待核，欧冠 9 支占位球队 |
| 237 | 735 | 圣图尔登 | Belgium | 3, 848 | 待核，14 个联赛 | 待核，欧冠 9 支占位球队 |
| 692 | 1012 | 图恩 | Switzerland | 3, 848 | 待核，14 个联赛 | 待核，欧冠 9 支占位球队 |
| 1055702 | 3403 | 帕福斯 | Cyprus | 3, 848 | 待核，14 个联赛 | 待核，欧冠 9 支占位球队 |

11 支的国家均有原件证据。同国和国家未知的联赛均无额外阻塞，余下拦截对 11 支相同。源审计中的英文队名只用于准备待核证据。

| football-data.org 占位编号 | 源审计英文队名 |
| --- | --- |
| `fdorg:team:298` | Girona FC |
| `fdorg:team:611` | Qarabağ Ağdam FK |
| `fdorg:team:907` | AC Sparta Praha |
| `fdorg:team:1864` | Royal Antwerp FC |
| `fdorg:team:1871` | BSC Young Boys |
| `fdorg:team:1887` | FK Shakhtar Donetsk |
| `fdorg:team:7283` | FK Crvena Zvezda |
| `fdorg:team:10601` | FK Kairat |
| `fdorg:team:11034` | Paphos FC |

[归档球队列表 3](https://github.com/qw5563680/FOOTBALL/blob/b2556ba8f2485620c2b27c761b1b639f33310ce3/state/catalogue/45976356364-1/apif-team-countries/3.json) · [归档球队列表 848](https://github.com/qw5563680/FOOTBALL/blob/b2556ba8f2485620c2b27c761b1b639f33310ce3/state/catalogue/45976356364-1/apif-team-countries/848.json) · [九支占位球队来源审计](https://github.com/qw5563680/FOOTBALL/blob/b2556ba8f2485620c2b27c761b1b639f33310ce3/state/audit/fdorg_unmatched_teams.csv)

离线复现使用已检出的上述数据提交，从项目根目录运行：

```sh
PYTHONPATH=src python -m zucai.identity_canonical plan --archive /path/to/archive --codes 3,848 --out /tmp/t99-plan.json
```

在 `existing_team_verdicts` 按竞彩官方编号查看每支球队。国家证据列出原件路径、SHA-256 和字段位置。拦截项列出库内球队 ID、占位名称和联赛。真实样本仅用于读取和生成提案。
