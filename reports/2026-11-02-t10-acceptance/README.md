# T10 API-Football 实际验收（2026-11-02，北京时间）

代码接入、九联赛免费整季采集和日职联合回测已实际运行；完整验收尚未通过。韩职0/3、澳超1/2的重叠链接低于90%，仅jc日职训练因样本不足失败。PR保持草稿，不合并。

## 范围、配额与存放

基线：T09最新提交 `13fc6613fba40fbd09aa4302ee3f36ca93e0a482`（PR #11，未合并），分支 `codex/T10-api-football`，PR base为 `codex/T09-football-data-org`。现有函数和类AST逐项核对，518项保持不变；CLI与训练guard仅新增注册和包装。

同一UTC日2026-11-01共 **69次APIF GET**：status 1、leagues 9、九联赛2021–2026赛季探测54、日职2027探测1、历史日期1、最近三天日期3；无隐含重试。没有跨日。最后一个有效每日剩余头为62；本地发送计数69。权限错误可无头，且服务端头观察到向上跳变，不能用100减本地发送数倒推服务端剩余。实现始终保留本地100次硬上限及默认reserve10。另有5份FD历史CSV下载作身份桥接、1次竞彩历史页请求（HTTP567），均不占APIF配额。

status实测Free、active=true、limit_day=100。每日头为x-ratelimit-requests-limit/remaining；x-ratelimit-limit/remaining为每分钟10次。服务端跨UTC午夜重置未实测。本地按UTC重置，配额文件损坏时拒绝继续。线程/文件锁、响应正文不安全时仍记录每日头、完整分页后再核验身份均有离线回归测试。

九联赛均仅2022、2023、2024三个起始年成功，2021、2025、2026逐个拒绝，日职元数据current=2027也拒绝。其余current=2026。拒绝表现为HTTP200 + errors.plan，不是空成功。27份整季均单页，7,059行。`/leagues`列出的年份不能视为权限。

日期端点历史2024-05-18被拒；UTC2026-10-31/11-01/11-02则成功，分别1568/1187/229行，包含未配置赛事。按配置过滤后才入库。因此本源的整季用途是历史训练，但近期日增量可用。

仅在恢复的jc数据分支归档副本中导入：原始jc库866场、833赛果；本轮独立新增apif **7088场**（含cancelled/scheduled），高优先级jc比分及原场次身份变动均为0。积分、赔率、阵容等没有进入新源。归档安全检查和SHA见哈希清单。

原件、数据库、模型及行级审计保留在采集环境的私有工作目录；这些大文件不提交。验收目录只提交本README与SHA256SUMS。配置、完整三状态ID别名、真实裁剪fixture及其provenance必须随代码提交。校验清单中private/路径相对于私有work/t10；不表示原件已上传。

## 逐联赛结果

入库列直接计数最终数据库中source=apif的场次，包含取消与未赛；linked_*不新增apif副本。拒绝为最后一轮仍被拒的行，不包括HTTP权限探测。可疑另列。未对齐为本轮观察到、未生效的数字ID去重数。最近三天jc完整窗口作分母，0/0代表无样本，不算通过。

|ID/联赛|可用起始年|入库场次|linked_existing (jc)|linked_fd|linked_fdorg|拒绝行|可疑行|未对齐ID|auto_verified|proposed|重叠jc链接|
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
|98/日职|2022,2023,2024|995|4|0|0|0|3|9|17|9|4/4|
|292/韩职|2022,2023,2024|696|0|0|0|0|6|20|0|20|0/3|
|188/澳超|2022,2023,2024|510|1|0|0|0|2|11|2|11|1/2|
|113/瑞超|2022,2023,2024|730|2|0|0|0|1|10|14|10|2/2|
|103/挪超|2022,2023,2024|728|2|0|0|0|3|10|13|10|2/2|
|244/芬超|2022,2023,2024|507|1|0|0|0|0|10|6|10|1/1|
|119/丹超|2022,2023,2024|584|0|0|0|0|0|15|0|15|0/0|
|253/美职|2022,2023,2024|1552|1|0|0|0|3|13|17|13|1/1|
|169/中超|2022,2023,2024|786|0|0|0|0|0|22|0|22|0/0|

approved均为0。69条auto_verified中，一部分通过T08已核验FD时区、主客赛程和规范名桥接，部分由最近三天jc直接核验；别名JSON逐项附原件SHA、对方比赛ID、UTC差值，桥接还含FD原件与T08别名表哈希。不会按名字相似度或比分选边。proposed均不生效，不能把其英文显示名当作已对齐的规范名。

韩职/澳超未达到90%；日职、瑞超、挪超、芬超和美职在此小窗口为100%，不能外推全季链接效果。历史2022–2024没有已恢复的同年jc覆盖，不能声称历史季官方链接率为100%。补取2024竞彩页被站点567拒绝，未绕过。

## 全部未链接官方场次

|竞彩ID|联赛|主队 — 客队|UTC开球|原因|
|---|---|---|---|---|
|jc:2042388|韩职|光州 — 金泉尚武|2026-10-31T07:30:00.000000Z|数字ID缺少唯一主客方向证据，proposed不启用；保留官方记录、隔离可疑APIF行|
|jc:2042414|韩职|蔚山现代 — 江原|2026-11-01T05:00:00.000000Z|数字ID缺少唯一主客方向证据，proposed不启用；保留官方记录、隔离可疑APIF行|
|jc:2042416|韩职|首尔 — 仁川联|2026-11-01T07:30:00.000000Z|数字ID缺少唯一主客方向证据，proposed不启用；保留官方记录、隔离可疑APIF行|
|jc:2042389|澳超|麦克阿瑟 — 西悉尼|2026-10-31T08:40:00.000000Z|数字ID缺少唯一主客方向证据，proposed不启用；保留官方记录、隔离可疑APIF行|

## 日职训练与滚动回测

两组同库、同 `--since 2026-09-01 --as-of 2026-11-01T17:00:00Z`、90天半衰期。只比较纯模型；没有可配对赛前市场样本。结果是在开发中查看数据后的历史回放，不是未见验证。

|来源|实际最终训练|最终assumed训练占比|评分n|Log Loss|三分类Brier|状态|
|---|---:|---:|---:|---:|---:|---|
|jc|32个已知赛果候选；不足以拟合|0%|0|不可计算|不可计算|CLI退出2；单独evaluate为warmup=32|
|jc,apif|1024（jc32，apif992）|96.875%|22|1.2313130615|0.7426293784|CLI退出0，模型已保存|

联合回测逐折训练均用992条历史assumed行，fold训练假设占比100%；评分标签来自jc captured，assumed_eval_share=0%。35个区间候选中3个标签在截点尚不可知，另10场球队未见，实际评分22场。不能把仅jc的n=0当作0损失或宣称联合模型改善。仅jc最终候选32场，模型既有最小要求为2×球队数+1；本任务未降低门槛或伪造可知时间。

实际CLI：

```bash
zucai fetch apif --league 98 --season 2022 --season 2023 --season 2024 --assume-known-after 3h --reserve 10
zucai fetch apif --date 2026-11-01
zucai fetch apif --league 98 --season 2024 --assume-known-after 3h --raw-file <原件.json> --db <副本.db>
zucai train --league 日职 --source jc,apif --db <副本.db> --since 2026-09-01 --as-of 2026-11-01T17:00:00Z --output <临时模型.pkl>
zucai train --league 日职 --source jc --db <同一副本.db> --since 2026-09-01 --as-of 2026-11-01T17:00:00Z --output <临时jc.pkl>
```

## 数据语义、测试与审查

真实韩职2024加时行apif:1317866：score.fulltime=2:1、goals=3:1、extratime=1:0；只导入2:1。日期裁剪含真实PST/AWD/AET/NS/FT，原件值未改写。历史季没有PST/AWD，因而真实状态分别保留于日响应，合成季样本另行明确标识，不能把拼接样本说成真实整季。MLS真实Canc大小写变体显式作为CANC处理。真实多页未观察到，两页及第二页失败只用离线构造样本验证。

最终本地完整pytest：**560 passed**（401条既有penaltyblog/NumPy弃用警告）；Ruff check及format --check通过。全部T10测试输入仅来自data/fixtures/t10及离线传输替身。原件和裁剪前后做raw及解码JSON key回显检查，提交前再扫描全部暂存文件及PR正文；不展示key。

独立代码审查发现的共享实例锁竞争、不安全正文丢失权威配额头、分页先入库可能抢占链接均已修复并经RED→GREEN回归测试；最终全套通过。现有518项函数/类AST保持一致。SQLite完整性与外键检查通过；原jc赛果及身份字段变动0。

## 全部proposed（120条，不生效）

以下每行保留本源数字ID和实际英文显示名；canonical均为null，须补充比赛证据后审查，不自动猜中文名。

|联赛|本源球队ID|显示名|
|---|---:|---|
|日职|279|Consadole Sapporo|
|日职|280|Jubilo Iwata|
|日职|283|Shimizu S-pulse|
|日职|284|Shonan Bellmare|
|日职|295|Sagan Tosu|
|日职|301|JEF United Chiba|
|日职|305|Mito Hollyhock|
|日职|307|Yokohama FC|
|日职|311|Albirex Niigata|
|韩职|2745|Bucheon FC 1995|
|韩职|2746|Gangwon FC|
|韩职|2747|Daegu FC|
|韩职|2748|FC Anyang|
|韩职|2749|Seoul E-Land FC|
|韩职|2750|Daejeon Citizen|
|韩职|2752|Busan I Park|
|韩职|2753|Asan Mugunghwa|
|韩职|2756|Suwon City FC|
|韩职|2757|Seongnam FC|
|韩职|2759|Gwangju FC|
|韩职|2761|Jeju United FC|
|韩职|2762|Jeonbuk Motors|
|韩职|2763|Incheon United|
|韩职|2764|Pohang Steelers|
|韩职|2765|Suwon Bluewings|
|韩职|2766|FC Seoul|
|韩职|2767|Ulsan Hyundai FC|
|韩职|2768|Gimcheon Sangmu FC|
|韩职|7078|Gimpo Citizen|
|澳超|939|Western Sydney Wanderers|
|澳超|941|Central Coast Mariners|
|澳超|942|Wellington Phoenix|
|澳超|943|Sydney|
|澳超|944|Melbourne Victory|
|澳超|945|Melbourne City|
|澳超|946|Newcastle Jets|
|澳超|947|Brisbane Roar|
|澳超|7573|Western United|
|澳超|15550|Macarthur|
|澳超|24608|Auckland|
|瑞超|373|GIF Sundsvall|
|瑞超|378|IFK Norrkoping|
|瑞超|766|Halmstad|
|瑞超|811|Helsingborg|
|瑞超|2163|IFK Varnamo|
|瑞超|2166|Orgryte IS|
|瑞超|2171|Varbergs BoIS FC|
|瑞超|2174|Osters IF|
|瑞超|2176|Landskrona BoIS|
|瑞超|6706|Utsikten|
|挪超|323|Stabaek|
|挪超|324|Stromsgodset|
|挪超|328|Haugesund|
|挪超|330|ODD Ballklubb|
|挪超|334|Start|
|挪超|757|Aalesund|
|挪超|2144|Kongsvinger|
|挪超|2151|jerv|
|挪超|2159|Ham-Kam|
|挪超|7012|Moss|
|芬超|587|Mariehamn|
|芬超|689|SJK|
|芬超|1163|Ilves|
|芬超|1166|Lahti|
|芬超|1169|Honka|
|芬超|1172|HIFK Helsinki|
|芬超|2075|FF Jaro|
|芬超|2076|Kooteepee|
|芬超|2078|EIF|
|芬超|2080|Haka|
|丹超|395|Vejle|
|丹超|396|Sonderjyske|
|丹超|397|FC Midtjylland|
|丹超|398|FC Nordsjaelland|
|丹超|400|FC Copenhagen|
|丹超|401|Randers FC|
|丹超|402|Aalborg|
|丹超|404|AC Horsens|
|丹超|405|Odense|
|丹超|406|Aarhus|
|丹超|407|Brondby|
|丹超|625|Lyngby|
|丹超|2070|Viborg|
|丹超|2072|Hvidovre|
|丹超|2073|Silkeborg|
|美职|1596|San Jose Earthquakes|
|美职|1598|Orlando City SC|
|美职|1599|Philadelphia Union|
|美职|1600|Houston Dynamo|
|美职|1608|Atlanta United FC|
|美职|1611|Sporting Kansas City|
|美职|1612|Minnesota United FC|
|美职|1614|CF Montreal|
|美职|1615|DC United|
|美职|1617|Portland Timbers|
|美职|16489|Austin|
|美职|18310|Charlotte|
|美职|25484|San Diego|
|中超|830|Beijing Guoan|
|中超|831|Dalian Aerbin|
|中超|832|Guangzhou Evergrande FC|
|中超|833|Shanghai Shenhua|
|中超|834|Changchun Yatai|
|中超|836|SHANGHAI SIPG|
|中超|837|Tianjin Teda|
|中超|838|Guangzhou R&F|
|中超|839|Hebei Zhongji|
|中超|840|Henan Jianye|
|中超|844|Shandong Luneng|
|中超|848|Hangzhou Greentown|
|中超|849|Shijiazhuang Y. J.|
|中超|1431|Qingdao Jonoon|
|中超|1433|Shenzhen Ruby FC|
|中超|1434|Wuhan Zall|
|中超|1439|Meizhou Kejia|
|中超|2626|Nantong Zhiyun|
|中超|5648|Chengdu Better City|
|中超|5686|Sichuan Jiuniu|
|中超|5695|Wuhan Three Towns|
|中超|17265|Qingdao Youth Island|
