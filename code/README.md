# T49 离线复现

这是结果表脚本，不是上线功能，也不会调用体育接口。

安装 `openpyxl` 后，下载并解压报告的数据包，在包目录运行：

```bash
python code/build_report.py .
```

脚本只读 `inputs/`、`raw/` 和已保存的 `checks/t30-half-evidence.json` 及原件，
生成 `outputs/` 和 `checks/validation.json`。失败时抛异常，不以近似数值继续。
没有完整杯赛日历时，输出固定标为“归档候选”，不是最终全赛事近10场。

若需要从同一正式归档恢复的 SQLite 重新导出输入：

```bash
python experiments/core39/extract_archive.py \
  --db /path/to/restored/input.db --repo /path/to/FOOTBALL \
  --raw-root /path/to/original-fd-raw --output /path/to/isolated-output
```

导出器用 SQLite `mode=ro` 和 `query_only`，记录读取前后数据库 SHA256；
原件的采集时间原样保留。此导出器针对本次钉住的数据提交，不接受新生产状态冒充同一基线。
随后需复制随包的 T30 复核原件及清单到新目录，再运行表格生成器。
