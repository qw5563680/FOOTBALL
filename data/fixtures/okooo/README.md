# 澳客足彩页面裁剪

北京时间 2026-11-24 00:35 左右，在云端开发机读取了以下公开页面。夹具用 UTF-8 保存标题、期号、停售标签和 14 行表格中与比赛身份及时间有关的字段。原页面不提供可核实的球队编号。

| 夹具 | 实际入口 | 裁剪前响应 SHA-256 |
|---|---|---|
| 26169.html | https://www.okooo.com/zucai/ | 2e2716b7ee502ba0306277a3acedce2476cea1617a664947daa20a20cba6250b |
| 26170.html | https://www.okooo.com/zucai/26170/ | fa02e680fa28accccb9da492bd6a5776fe672aa2700a72186932b5dc4f6223cb |

随后两次独立探测的逐期响应哈希、状态码和 28 场输出见[验收记录](../../../reports/okooo-public-probe/README.md)。
