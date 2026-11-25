# T84 补齐 #217–#220 没同步的文档

来源：所有者 2026-11-25「交给 Codex 修」；
`docs/reviews/2026-11-25-codex-217-224-audit.md` 第 5 条。排在 T80、T81 合并之后做，文档按合并后的实际写。

## 问题

1. `docs/ARCHITECTURE.md` 的日报通知一节仍是 #220 之前的写法。
   `jc_extra_dates`、`jc_spillover`、`notification-failures` 这三个新东西，
   在 `docs/ARCHITECTURE.md`、`README.md`、`docs/DATA_SOURCES.md` 里一次都没有出现。
2. `python -m zucai.digest_notify --date … --version … --history-root …`（补发）是新增的命令行用法，
   `README.md` 里没有（任务规则第 4 条）。
3. `docs/DATA_SOURCES.md` 的「越过去的期次日报里不提」一段仍写「期次清单只来自第三方页面，
   项目没有官方渠道核对」，T79 之后已不成立。
4. `docs/DATA_SOURCES.md` 的「T79 胜负彩官方公告」一节记了 2026-11-23 在开发环境请求三个站的 robots.txt：
   竞彩网返回 567，规则至今没有结论。开发环境发真实请求本身不合任务规则第 10 条，不能当依据。

## 范围

只改文档。

1. `docs/ARCHITECTURE.md`：
   - 写明运行清单的 `jc_extra_dates`，以及批次 `input.scope` 的三种情况（缺省、`sfc_only`、`jc_spillover`）
     和 `jc_scope_match_ids`；
   - 日报通知一节按现状重写：补发、失败记录（`daily/<日期>/notification-failures/…`）、
     超长处理（T80）、目录格式（T81）。
2. `README.md`：补一行补发用法；说明「竞彩销售日跨午夜」是什么。
3. `docs/DATA_SOURCES.md`：
   - 改正「项目没有官方渠道核对」的过时说法，指向「T79 胜负彩官方公告」一节；
   - robots 一段写清：开发环境那次检查不作为依据；结论以正式运行里程序自己的 robots 检查为准，
     由审查者经 Actions 取得后回填；取得之前写「未核实」。
4. 如果正式运行目前没有把各站 robots 检查的结果留档，在文档里照实写「未留档」，不在本任务里改代码。

## 不做

- 不改 `src/`、`tests/`、工作流里的任何文件。
- 不发任何真实请求。

## 验收标准

1. 三个名字在 `docs/ARCHITECTURE.md` 里各有说明；`README.md` 有补发用法；`docs/DATA_SOURCES.md` 两处已改正。
2. 改动只涉及 `docs/` 和 `README.md`。
3. 文档写的与代码一致（审查者对照 `digest_notify.py`、`cloud_predict.py`、`digest_context.py`）。
4. 纯文档，合并即结项。

## 交审时请列出

改动的文件。没有需要重跑的测试。
