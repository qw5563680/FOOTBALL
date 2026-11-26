# T92 中国足彩网备用赛程探测

## 中国足彩网待实测

公开搜索索引显示中国足彩网首页有足彩和竞彩足球导航。索引也提供了下列候选页。首页的实际导航目标尚未确认，入口可读性和能否作为备用赛程以正式采集机探测为准。[首页](https://www.zgzcw.com/) · [T92 任务单](../../docs/tasks/T92-backup-source-probe-zgzcw.md)

| 用途和候选页 | robots、访问时段和频率 | 静态对阵和开球时间 | 字段覆盖 | 重复读取 |
| --- | --- | --- | --- | --- |
| [14 场胜负彩](https://cp.zgzcw.com/zgzcw/lottery/) | 同域 robots 待读，是否允许访问本路径待核 | 待实测 | 待实测 | 待实测 |
| [竞彩足球](https://cp.zgzcw.com/lottery/jczq/index.jsp) | 同域 robots 待读，是否允许访问本路径待核 | 待实测 | 待实测 | 待实测 |

核对期号、场序、赛事、主客队、开球时间、停售时间、公开比赛编号，并记录每种用途的实际覆盖。若页面只有通用比赛或栏目导航，保留证据并标为玩法未核实。

合并后由 DeepSeek 在 `main` 手动运行 **Football cloud feeds**，选择 `operation=probe`，`options` 填 `{"probe_kind":"backup-sources"}`。先读取 `cp.zgzcw.com` 的 robots.txt，按规则分别判断两个路径。每个候选最多尝试 3 次公开页面请求，间隔至少 3 秒，robots 若要求更长间隔则按其要求。中国足彩网在额度内优先走两种用途，再尝试重复读取一个已读到静态赛程的页面。另一个页面若只读一次，稳定性仍待核。[工作流](https://github.com/qw5563680/FOOTBALL/actions/workflows/football-feeds.yml)

验收时从 `backup-sources-<run>-<attempt>` 读取 `probe.json`、`samples/` 和 `SHA256SUMS`。回填 HTTP、访问规则、静态字段和重复结果，附原始响应及裁剪产物的哈希和运行链接。Claude 对照产物核实结论，是否接入由所有者决定。[数据来源](../../docs/DATA_SOURCES.md#t92-中国足彩网备用赛程探测)

## T90 既有实测回填

正式采集机在 2026-11-26 北京时间 13:47:46 至 13:48:02 运行。探测作业成功，正式采集、预测和日报作业跳过。下表按附件中的 `probe.json` 和裁片核对。[运行](https://github.com/qw5563680/FOOTBALL/actions/runs/45763565563) · [附件 backup-sources-45763565563-1](https://github.com/qw5563680/FOOTBALL/actions/runs/45763565563/artifacts/15090927941)

| 候选 | 访问规则 | 本次观察 | 稳定性 |
| --- | --- | --- | --- |
| 新球体育 | `aiplus.titan007.com/robots.txt` HTTP 302，`jc.titan007.com/robots.txt` HTTP 404，两个域的适用规则均未核实 | 14 场、竞彩页面均未请求，合计 0/3 次 | 未测试 |
| 雷速体育 | `www.leisu.com/robots.txt` HTTP 200，对探测标识没有适用的路径限制，未公布访问时段 | 只读了静态首页，HTTP 200；未确认专用的 14 场或竞彩赛程链接，合计 1/3 次 | 首页只读一次，赛程页未测试 |
| 澳客竞彩 | `www.okooo.com/robots.txt` HTTP 200，`/jingcai/` 可访问，规定 UTC 01:30–18:30 访问 | 竞彩页 HTTP 200，裁片里可核对对阵和开球时间的静态比赛行 0 条，字段覆盖未证实，合计 1/3 次 | 竞彩页只读一次，稳定性未核实 |

本轮未找到可供备用的竞彩赛程。所有者决定放弃新球体育、雷速体育两个渠道，T92 候选名单移除这两项。澳客竞彩页面保留为探测对照，澳客 14 场页面继续作为 T89 的备用名单来源。[所有者决定](https://github.com/qw5563680/FOOTBALL/pull/243#issuecomment-7183868593) · [T89 备用名单规则](../../docs/DATA_SOURCES.md#t89-澳客备用胜负彩名单)

下载的 T90 ZIP 共 6,334 字节，SHA-256 对应 GitHub 元数据。解压后，附件自带的清单所列 8 个文件全部通过校验。提交的 [SHA256SUMS](SHA256SUMS) 可复核外层 ZIP 及归档内证据。下载后将 ZIP 保存为 `backup-sources-45763565563-1.zip`，并解压至同名目录，在其父目录执行 `sha256sum -c /path/to/reports/t92-zgzcw-probe/SHA256SUMS`。附件里的原始 HTTP 响应哈希来自采集时的响应字节。
