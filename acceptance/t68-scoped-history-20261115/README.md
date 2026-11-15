# T68 四项跟进：离线验收证据

代码：16a033908233a95941c02c5e31424d289e71c579

固定归档：8b900f4cf0f9a1ca1afbafc9298e97b7cc7519f2；实际 UTC/北京时间、命令及源码哈希在 verification/*.execution.json。

压缩包：t68-scoped-history-evidence.zip，SHA256 `2a91463651d367cd20dae34cc1595484b74a22eb1f847d2bc4a5db2e1d971719`。596项，逐项 SHA256SUMS.json 已校验，不含数据库或访问密钥。

原359条拒收中349恢复正常处理，10仍待核；加原有2条共12。全部仍待核行保留编号、原因。257创建决定仍为proposed，未批准/未创建。原档日职3/52、瑞超0/77；含 auxiliary-fd 资料才有31/52、19/77。

解压后 acceptance/replay-export 与 replay-export2 是可恢复的核心表导出，并非生产全量备份。二者207个文件内容一致；原件另从固定归档取得，FD辅助原件已随包给出。

verification/ 保留失败与成功记录：第一次全量因 ENOSPC 失败；full-suite-release 是最后全量结果。成功临时目录清理插件只在用例及teardown通过后删除该用例的tmp_path，不删失败或共享目录，不改变断言。

没有运行生产任务、发布正式数据、开启每日采集或合并PR。下一步由Claude按PR任务单独立复核，再由所有者决定正式应用。
