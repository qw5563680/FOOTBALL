"""Offline review reproduction: archived JC seed plus explicitly synthetic API data."""

import hashlib
import json
import subprocess
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import select

from zucai.db import get_engine, init_db, session_scope
from zucai.models import ApifFixtureIndex, Match, Result, Team
from zucai.sources.api_common import ApiGame
from zucai.sources.apif_ingest import import_apif_games
from zucai.stats_link import link_fixtures

ROOT = Path(__file__).parent
ARCHIVE = Path("/workspace/work/t65-fixed-archive")
REPO = Path("/workspace/work/football-t63")
NOW = datetime.fromisoformat("2026-11-14T00:00:00Z")


def seed():
    manifest = json.loads((ARCHIVE / "feed_manifest.json").read_text())
    result = {"archive_commit": "e5848e5", "source_files": {}, "tables": {}}
    for table in ("teams", "matches", "results"):
        path = ARCHIVE / "state" / f"{table}.jsonl"
        rows = []
        for line in path.read_text().splitlines():
            row = json.loads(line)
            if (table == "teams" and row[0] in (42, 226)) or (
                table != "teams" and row[0] == "jc:2042615"
            ):
                rows.append(dict(zip(manifest["columns"][table], row, strict=True)))
        result["source_files"][str(path.relative_to(ARCHIVE))] = hashlib.sha256(path.read_bytes()).hexdigest()
        result["tables"][table] = rows
    return result


def snapshot(session, model, match_id=None):
    query = select(model.__table__)
    if match_id is not None:
        query = query.where(model.match_id == match_id)
    return [dict(row) for row in session.execute(query).mappings()]


def main():
    sample = seed()
    fixture = dict(
        fixture=dict(id=1556134, date="2026-11-08T06:00:00Z", status=dict(short="FT")),
        league=dict(id=98, season=2027),
        teams=dict(home=dict(id=293, name="Gamba Osaka"), away=dict(id=291, name="Cerezo Osaka")),
    )
    document = dict(get="fixtures", parameters=dict(league=98, season=2027), errors=[],
                    results=1, paging=dict(current=1, total=1), response=[fixture])
    sample["synthetic_api"] = dict(
        disclaimer="Manually constructed from the review's fixture ID and archived JC match; not a real provider response or coverage evidence.",
        document=document, full=[2, 3], half=[0, 1], observed_utc=NOW.isoformat(),
    )
    (ROOT / "jc-2042615-seed-with-synthetic-api.json").write_text(json.dumps(sample, ensure_ascii=False, indent=2) + "\n")
    baseline_source = subprocess.check_output(
        ["git", "show", "b821ae6524e7d339c38eee4701f67d1013a3e280:src/zucai/stats_link.py"], cwd=REPO, text=True
    )
    baseline = {}
    exec(compile(baseline_source, "T63-b821-stats_link.py", "exec"), baseline)
    aliases = json.loads((REPO / "data/team_aliases/api_football.json").read_text())
    kwargs = dict(league_id=98, season=2027, aliases=aliases,
                  metadata=dict(captured_utc=NOW, raw_sha256=hashlib.sha256(json.dumps(document).encode()).hexdigest(),
                                raw_location="fixture://synthetic-review-api-1556134"))
    with TemporaryDirectory() as directory:
        engine = get_engine(Path(directory) / "probe.db")
        init_db(engine)
        with session_scope(engine) as session:
            for row in sample["tables"]["teams"]:
                session.add(Team(**dict(row, aliases=json.loads(row["aliases"]))))
            session.flush()
            session.add_all(Match(**row) for row in sample["tables"]["matches"])
            session.flush()
            session.add_all(Result(**row) for row in sample["tables"]["results"])
            session.flush()
            before = dict(matches=snapshot(session, Match, "jc:2042615"), results=snapshot(session, Result, "jc:2042615"))
            imported = import_apif_games(
                session, [ApiGame(provider="apif", provider_match_id=1556134, competition_code="98", league="日职",
                                  kickoff_utc=datetime.fromisoformat(fixture["fixture"]["date"]), status="FT",
                                  home_raw="Gamba Osaka", away_raw="Cerezo Osaka", home_provider_id=293, away_provider_id=291,
                                  full=(2, 3), half=(0, 1), regular_time_basis="regular")],
                seen=NOW, assume=None, aliases=aliases, retain_results=True, imported_utc=NOW,
            )
            assert imported["inserted"] == 1, imported
            baseline_result = baseline["link_fixtures"](session, document, **kwargs)
            assert baseline_result["linked"] == 0 and baseline_result["audit"][0]["reason"] == "多对一"
            all_results = snapshot(session, Result)
            fixed = link_fixtures(session, document, **kwargs)
            repeat = link_fixtures(session, document, **kwargs)
            assert fixed["linked"] == 1 and fixed["rejected"] == 0
            assert repeat["linked"] == 0 and repeat["unchanged"] == 1
            assert session.get(ApifFixtureIndex, "1556134").match_id == "jc:2042615"
            after = dict(matches=snapshot(session, Match, "jc:2042615"), results=snapshot(session, Result, "jc:2042615"))
            assert before == after
            assert all_results == snapshot(session, Result)
            report = dict(synthetic_api_disclaimer=sample["synthetic_api"]["disclaimer"], imported=imported,
                          baseline=baseline_result, fixed=fixed, repeat=repeat, original_jc_unchanged=before == after,
                          all_results_unchanged=True, indices=snapshot(session, ApifFixtureIndex))
            (ROOT / "retained-index-probe.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
            print(json.dumps(dict(baseline=baseline_result["linked"], fixed=fixed["linked"], repeat=repeat["linked"], target="jc:2042615", unchanged=True)))
        engine.dispose()


if __name__ == "__main__":
    main()
