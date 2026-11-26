# T92、T97 中国足彩网备用赛程探测

## T92 第一轮实测

2026-11-26 北京时间 21:26:58 至 21:27:10，正式采集机读取了公开索引推测的两个中国足彩网路径和澳客竞彩对照页。探测作业成功，正式采集、预测和日报作业均跳过。[运行 45826832242](https://github.com/qw5563680/FOOTBALL/actions/runs/45826832242) · [附件 backup-sources-45826832242-1](https://github.com/qw5563680/FOOTBALL/actions/runs/45826832242/artifacts/15122555379)

| 候选页 | 访问规则 | HTTP 与静态行数 | 字段及稳定性 |
| --- | --- | --- | --- |
| 中国足彩网 `cp.zgzcw.com/zgzcw/lottery/` | `cp.zgzcw.com` 的 robots.txt 返回 200，禁止 `/homePage/`，未公布时段 | 200，0 条可核对静态对阵 | 字段为空，重复读取未测 |
| 中国足彩网 `cp.zgzcw.com/lottery/jczq/index.jsp` | 同域规则允许访问本路径 | 200，0 条可核对静态对阵 | 字段为空，重复读取未测 |
| 澳客 `www.okooo.com/jingcai/` | robots.txt 返回 200，允许该路径，规定 UTC 01:30–18:30（北京时间 09:30 至次日 02:30） | 200，0 条可核对静态对阵 | 字段为空，重复读取未测 |

第一轮证据支持访问状态和当时静态解析结果。两条中国足彩网路径是否为相应玩法的对阵页、静态结构为何无法解析均未确认。ZIP 共 5,790 字节，SHA-256 与 GitHub 元数据一致，附件清单的 7 个文件全部通过校验。[ZIP 与文件哈希](SHA256SUMS)

## T97 第二轮验收

正式运行待合并后执行。DeepSeek 在 `main` 的 **Football cloud feeds** 选择 `operation=probe`，填写 `options={"probe_kind":"backup-sources"}`。探测由首页发现静态入口，逐域读取 robots，按访问时段和请求间隔核对。中国足彩网上限 6 次页面请求，澳客对照上限 3 次，robots 每域 1 次单独计数。[工作流](https://github.com/qw5563680/FOOTBALL/actions/workflows/football-feeds.yml) · [T97 任务单](../../docs/tasks/T97-zgzcw-probe-from-homepage.md)

验收从 `backup-sources-<run>-<attempt>` 读取 `probe.json`、`samples/` 和 `SHA256SUMS`。回填访问规则、是否为静态对阵页、字段覆盖与复读对比，附运行链接、附件名和 SHA-256。证据不足的项目保留「未确认」。[数据来源](../../docs/DATA_SOURCES.md#t97-中国足彩网从首页发现入口)

复核当前清单时，把 T90、T92 的 ZIP 保存为附件同名的 `.zip`，分别解压到同名目录，在共同父目录执行 `sha256sum -c /path/to/reports/t92-zgzcw-probe/SHA256SUMS`。原 HTTP 响应的 SHA-256 在相应样本里。

### T97 离线回放

用合成首页、合成竞彩表格和已归档的澳客裁片作为模拟响应，走正式工作流使用的命令入口。下列行数说明探测能识别样本，真实页面的覆盖仍待第二轮核对。

| 样本 | 回放结果 |
| --- | --- |
| 首页给出两个同站子域的入口 | 中国足彩网读 3 份 robots、请求 5/6 次网页。14 场两次各 14 行，8 个字段一致。竞彩两次各 1 行，5 个字段一致。附件清单 12 个文件通过校验 |
| 首页只有站外对阵链接和玩法介绍 | 中国足彩网读 1 份 robots、只请求首页 1/6 次。保留「竞彩足球对阵」「胜负彩介绍」的可见文字，两个赛程入口均未确认 |
| 澳客对照返回 405 | 保留 HTTP 405、标题与字节数等诊断证据，赛程覆盖和稳定性未确认 |

自动测试另覆盖 robots 拒绝、访问时段、复读不一致、三类无对阵原因及样本字段过滤。离线样本只读到主客队时也会复读，分开报告对阵行数和带开球时间的行数。[请求回归](../../tests/test_backup_source_probe.py) · [页面回归](../../tests/test_backup_source_pages.py) · [Windows 工具加载](../../tests/test_windows_collection_machine.py)

## T90 既有实测回填

正式采集机在 2026-11-26 北京时间 13:47:46 至 13:48:02 运行。探测作业成功，正式采集、预测和日报作业跳过。下表按附件中的 `probe.json` 和裁片核对。[运行](https://github.com/qw5563680/FOOTBALL/actions/runs/45763565563) · [附件 backup-sources-45763565563-1](https://github.com/qw5563680/FOOTBALL/actions/runs/45763565563/artifacts/15090927941)

| 候选 | 访问规则 | 本次观察 | 稳定性 |
| --- | --- | --- | --- |
| 新球体育 | `aiplus.titan007.com/robots.txt` HTTP 302，`jc.titan007.com/robots.txt` HTTP 404，两个域的适用规则均未核实 | 14 场、竞彩页面均未请求，合计 0/3 次 | 未测试 |
| 雷速体育 | `www.leisu.com/robots.txt` HTTP 200，对探测标识没有适用的路径限制，未公布访问时段 | 只读了静态首页，HTTP 200；未确认专用的 14 场或竞彩赛程链接，合计 1/3 次 | 首页只读一次，赛程页未测试 |
| 澳客竞彩 | `www.okooo.com/robots.txt` HTTP 200，`/jingcai/` 可访问，规定 UTC 01:30–18:30 访问 | 竞彩页 HTTP 200，裁片里可核对对阵和开球时间的静态比赛行 0 条，字段覆盖未证实，合计 1/3 次 | 竞彩页只读一次，稳定性未核实 |

本轮未找到可供备用的竞彩赛程。所有者决定放弃新球体育、雷速体育两个渠道，T92 候选名单移除这两项。澳客竞彩页面保留为探测对照，澳客 14 场页面继续作为 T89 的备用名单来源。[所有者决定](https://github.com/qw5563680/FOOTBALL/pull/243#issuecomment-7183868593) · [T89 备用名单规则](../../docs/DATA_SOURCES.md#t89-澳客备用胜负彩名单)

下载的 T90 ZIP 共 6,334 字节，SHA-256 对应 GitHub 元数据。解压后，附件自带的清单所列 8 个文件全部通过校验。提交的 [SHA256SUMS](SHA256SUMS) 可复核外层 ZIP 及归档内证据。下载后将 ZIP 保存为 `backup-sources-45763565563-1.zip`，并解压至同名目录，在其父目录执行 `sha256sum -c /path/to/reports/t92-zgzcw-probe/SHA256SUMS`。附件里的原始 HTTP 响应哈希来自采集时的响应字节。
