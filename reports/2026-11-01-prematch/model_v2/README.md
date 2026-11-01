# 如何复现冻结结果

请在本仓库此报告分支（含 T05 引擎）使用 Python 3.12；依赖的确切版本见 run_log.json。该目录的 predictions.json 是开球前冻结的正式评分版本，禁止用后见结果覆盖。

```bash
python -m pip install -e ".[model]"
cd reports/2026-11-01-prematch/model_v2
python -c "import gzip,pathlib; pathlib.Path('training_input.csv').write_bytes(gzip.decompress(pathlib.Path('training_input.csv.gz').read_bytes()))"
python forecast.py
```

不带参数时程序离线重拟合，核对三项概率和全部16组抽样计数，不写回预测。训练清单只含球队、常规时间进球、时点、来源行号和赛事；不含赔率。文件 manifest 中的 CSV URL 指向供应商持续更新的地址，应以上述归档输入复现。不要用今天重新下载的历史文件伪装当日快照。
