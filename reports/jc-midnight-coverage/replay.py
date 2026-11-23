"""Offline coverage replay on a bounded crop of the pinned production feed."""

import argparse
import hashlib
import json
import socket
import sqlite3
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from forecast_publication_support import confirm_fixture
from zucai import cloud_predict
from zucai.cloud_feeds import export_feeds
from zucai.daily_digest import generate
from zucai.db import get_engine, init_db
from zucai.scoreboard.score import instant


def replay(source, work):
    fixture = json.loads(Path("data/fixtures/jc-sales-day/midnight-2026-11-23.json").read_bytes())
    raw = (source / "feed_manifest.json").read_bytes()
    if (
        hashlib.sha256(raw).hexdigest()
        != "d00bac3f46c33d180eef018231a4d9d0beb735184c474c234340e0c4ccd323c2"
    ):
        raise ValueError("回放要求 226c3709 的原始采集清单")
    manifest = json.loads(raw)
    ids = {r["match_id"] for r in fixture["catalog"]}
    leagues = {r["league"] for r in fixture["catalog"]}
    batch = fixture["forecasts"]["2026-11-23"]
    cutoff = instant(batch["generated_utc"])
    config = json.loads(Path("data/model/params.json").read_bytes())
    config["leagues"] = {k: v for k, v in config["leagues"].items() if k in leagues}

    def verified(path):
        data = (source / path).read_bytes()
        if hashlib.sha256(data).hexdigest() != manifest["files"][path]:
            raise ValueError(f"原始归档校验失败 {path}")
        return data

    def rows(table):
        return [
            dict(zip(manifest["columns"][table], json.loads(line), strict=True))
            for line in verified(f"state/{table}.jsonl").splitlines()
        ]

    matches = rows("matches")
    results = {r["match_id"]: r for r in rows("results")}
    chosen = [r for r in matches if r["match_id"] in ids]
    for league, params in config["leagues"].items():
        eligible = [
            r
            for r in matches
            if r["league"] == league
            and r["source"] in params["sources"]
            and r["status"] == "finished"
            and instant(r["kickoff_utc"]) < cutoff
            and (result := results.get(r["match_id"]))
            and result["available_utc"]
            and instant(result["available_utc"]) < cutoff
        ]
        chosen.extend(sorted(eligible, key=lambda r: r["kickoff_utc"])[-250:])
    database = work / "sample.db"
    engine = get_engine(database)
    init_db(engine)
    engine.dispose()
    with sqlite3.connect(database) as db:

        def insert(table, record):
            columns = manifest["columns"][table]
            placeholders = ",".join("?" * len(columns))
            db.execute(
                f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
                [record[k] for k in columns],
            )

        teams = {r[key] for r in chosen for key in ("home_team_id", "away_team_id")}
        for row in rows("teams"):
            if row["id"] in teams:
                insert("teams", row)
        for row in chosen:
            insert(
                "matches", dict(row, counterpart_id=None)
            )  # only the selected fixtures are copied
            if row["match_id"] not in ids:
                insert("results", results[row["match_id"]])
    for name in ("jc_catalog", "jc_skipped"):
        path = f"state/audit/{name}/2026-11.csv"
        (work / path).parent.mkdir(parents=True)
        (work / path).write_bytes(verified(path))
    params = work / "params.json"
    params.write_text(json.dumps(config))
    archive = work / "archive"
    export_feeds(database, archive, date(2026, 11, 22), "fixed-input")
    with patch.object(cloud_predict, "_now", return_value=cutoff):
        run = cloud_predict.run(archive, params, "offline-2357")
    confirm_fixture(archive, batch["published_utc"])
    text = generate(archive, work / "daily", now=fixture["report_generated_utc"]).read_text()
    print(
        "跨午夜批次", run["jc_extra_dates"], "已预测", run["forecasts"]["2026-11-24"]["predicted"]
    )
    print(text.split("## 竞彩销售日", 1)[1].split("## 今日与明日", 1)[0])
    assert "应有 8 场、已预测 8 场、缺 0 场" in text
    assert all(f"| 周一{i:03} |" in text for i in range(1, 9))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    args = parser.parse_args()
    with (
        TemporaryDirectory() as directory,
        patch.object(socket.socket, "connect", side_effect=OSError("offline")),
    ):
        replay(args.snapshot, Path(directory))
