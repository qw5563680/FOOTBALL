"""Read-only extraction for the owner's one-off T49 coverage experiment."""

import argparse
import csv
import hashlib
import json
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def extract(db, repo, raw_root, output):
    output.mkdir(parents=True, exist_ok=True)
    (output / "inputs").mkdir(exist_ok=True)
    (output / "raw").mkdir(exist_ok=True)
    digest = hashlib.sha256(db.read_bytes()).hexdigest()
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    target = dict(conn.execute("SELECT * FROM matches WHERE match_id='jc:2042565'").fetchone())
    assert (target["home_team_id"], target["away_team_id"]) == (58, 52)
    teams = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM teams")}
    matches = [
        dict(r)
        for r in conn.execute(
            "SELECT m.*, r.home_goals,r.away_goals,r.available_utc AS result_available_utc "
            "FROM matches m LEFT JOIN results r USING(match_id) "
            "WHERE home_team_id IN (52,58) OR away_team_id IN (52,58)"
        )
    ]
    # Keep all source rows. Selection and deduplication are independently reproducible.
    ids = {r["match_id"] for r in matches} | {
        r["counterpart_id"] for r in matches if r["counterpart_id"]
    }
    stats = [
        dict(r)
        for r in conn.execute("SELECT * FROM match_stats")
        if r["match_id"] in ids or r["provider_fixture_id"] in ids
    ]
    related = {r[k] for r in matches for k in ("home_team_id", "away_team_id")}
    inventory = {}
    for table in ("match_stats", "match_events", "match_lineups"):
        inventory[table] = {
            "total_rows": conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0],
            "research_team_rows": conn.execute(
                f"SELECT count(*) FROM {table} WHERE team_id IN(52,58)"
            ).fetchone()[0],
        }
    inventory["apif_fixture_index"] = {
        "total_rows": conn.execute("SELECT count(*) FROM apif_fixture_index").fetchone()[0],
        "research_team_rows": conn.execute(
            "SELECT count(*) FROM apif_fixture_index "
            "WHERE home_team_id IN(52,58) OR away_team_id IN(52,58)"
        ).fetchone()[0],
    }
    raw_manifest = []
    for location, sha in sorted({(r["raw_location"], r["raw_sha256"]) for r in stats}):
        member = location.split("#", 1)[1]
        source = raw_root / member
        assert hashlib.sha256(source.read_bytes()).hexdigest() == sha
        dest = output / "raw" / source.name
        shutil.copyfile(source, dest)
        sidecar = Path(str(source) + ".meta.json")
        meta = json.loads(sidecar.read_text())
        assert meta["sha256"] == sha
        shutil.copyfile(sidecar, Path(str(dest) + ".meta.json"))
        raw_manifest.append(
            {
                "raw_location": location,
                "path": str(dest.relative_to(output)),
                "sha256": sha,
                "captured_utc": meta["fetched_at_utc"],
                "url": meta["url"],
            }
        )
    provenance = {
        "extracted_utc": datetime.now(UTC).isoformat(),
        "code_commit": "a664318450675946a578c041cb1f24219981e771",
        "official_data_commit": "9688806d7045824de9dfdfbcbb7bd6f8f3f4cb2e",
        "database_sha256_before": digest,
        "archive_scope": "Official manifest-restored database; T30 raw archives checked separately",
        "sample_complete_all_competitions": False,
        "target": target,
        "inventory": inventory,
        "api_football_new_requests": 0,
        "api_football_status": (
            "未执行：GitHub API 代理拒绝；连接器无 workflow_dispatch 工具；"
            "现有 probe 不支持本次定向球队/比赛"
        ),
        "sportmonks_status": (
            "未实测：未发现环境绑定或仓库配置；无法读取 Actions Secrets 名单，"
            "不能断言账户没有试用权限"
        ),
    }
    conn.close()
    provenance["database_sha256_after"] = hashlib.sha256(db.read_bytes()).hexdigest()
    assert provenance["database_sha256_before"] == provenance["database_sha256_after"]
    for name, value in {
        "matches": matches,
        "teams": {i: teams[i] for i in related},
        "stats": stats,
        "raw_manifest": raw_manifest,
        "provenance": provenance,
    }.items():
        dump(output / "inputs" / f"{name}.json", value)
    for src, name in (
        ("docs/indicators/match-indicators-core-39.csv", "core-39.csv"),
        ("data/team_aliases/football_data_co_uk.json", "fd-team-aliases.json"),
        ("data/football_data_co_uk/leagues.json", "fd-leagues.json"),
    ):
        shutil.copyfile(repo / src, output / "inputs" / name)
    registry = list(csv.DictReader((output / "inputs/core-39.csv").open(encoding="utf-8-sig")))
    assert len(registry) == 39
    return provenance


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("db", "repo", "raw-root", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    extract(args.db, args.repo, args.raw_root, args.output)
