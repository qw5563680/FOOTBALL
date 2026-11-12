"""T54 uses the actual November 12 archive; no live requests or model retraining."""

import csv
import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from zucai.cloud_feeds import TABLES
from zucai.db import get_engine, init_db
from zucai.models import Match, SfcIssue, Team
from zucai.report.sfc import build_sfc_report
from zucai.sources.jc_identity import reconcile
from zucai.sources.sfc import refresh_snapshot

FIXTURE = Path(__file__).parents[1] / "data/fixtures/t54"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


@pytest.fixture
def real_database(tmp_path):
    database = tmp_path / "football.db"
    engine = get_engine(database)
    init_db(engine)
    with Session(engine) as session:
        for table, cls in (("teams", Team), ("matches", Match)):
            for line in (FIXTURE / f"{table}.jsonl").read_text().splitlines():
                row = dict(zip(TABLES[table], json.loads(line), strict=True))
                if table == "teams":
                    row["aliases"] = json.loads(row["aliases"])
                session.add(cls(**row))
            session.flush()
        session.add(SfcIssue(**json.loads((FIXTURE / "26163.json").read_text())["issue"]))
        session.commit()
    engine.dispose()
    return database


def test_actual_nov12_baseline_is_thirteen_with_third_identity_conflict(real_database):
    engine = get_engine(real_database)
    try:
        with Session(engine) as session:
            issue = session.get(SfcIssue, "26163")
            before = canonical(issue.snapshot)
            rows = refresh_snapshot(session, issue)
            assert sum(r["mapping"] == "matched" for r in rows) == 13
            assert rows[2]["match_id"] is None
            assert rows[2]["reason"] == "conflicting_identity"
            assert session.get(Match, "jc:2042694").home_team_id == 1156
            assert session.get(Match, "apif:1528963").home_team_id == 1149
            assert canonical(issue.snapshot) == before
    finally:
        engine.dispose()


def test_actual_repair_maps_fourteen_and_preserves_frozen_thirteen(real_database):
    result = reconcile(real_database, "t54-real-fixture")
    assert result["rekeyed_matches"] == 1
    assert result["requests"] == 0
    engine = get_engine(real_database)
    try:
        with Session(engine) as session:
            issue = session.get(SfcIssue, "26163")
            archived_issue = json.loads((FIXTURE / "26163.json").read_text())["issue"]
            assert canonical(issue.snapshot) == canonical(archived_issue["snapshot"])
            mapped = refresh_snapshot(session, issue)
            assert len(mapped) == 14 and all(row["mapping"] == "matched" for row in mapped)
            assert mapped[2]["match_id"] == "jc:2042694"
            assert mapped[2]["matched_identity"]["home"] == "阿尔巴尼亚"
            jc = session.get(Match, "jc:2042694")
            apif = session.get(Match, "apif:1528963")
            assert (jc.home_team_id, jc.away_team_id, jc.kickoff_utc) == (
                apif.home_team_id,
                apif.away_team_id,
                apif.kickoff_utc,
            )
            assert jc.home_team_id == 1149
            assert session.get(Team, 1156).name == "阿尔巴尼"
            batch = json.loads((FIXTURE / "forecast-fourteen.json").read_text())
            batch_before = canonical(batch)
            report = build_sfc_report(
                session,
                issue,
                batch,
                forecast_sha256=hashlib.sha256(batch_before).hexdigest(),
                generated=datetime.fromisoformat(batch["generated_utc"]),
                allow_missing=True,
            )
            thirteen = [
                {k: row[k] for k in ("seq", "match_id", "probabilities")}
                for row in report["rows"]
                if row["seq"] != 3
            ]
            original = json.loads((FIXTURE / "frozen-thirteen.json").read_text())
            assert canonical(thirteen) == canonical(original)
            # The original third prediction was excluded for the duplicate team's
            # missing history. It must stay frozen; the full acceptance script
            # creates a separate model replay instead of rewriting this old row.
            assert report["rows"][2]["probabilities"] is None
            assert report["rows"][2]["reason"] == "无模型：模型缺少球队历史记录：阿尔巴尼 / 芬兰"
            assert canonical(batch) == batch_before
            assert canonical(issue.snapshot) == canonical(archived_issue["snapshot"])
    finally:
        engine.dispose()
    audit = real_database.parent / "state/audit/jc_identity_merges.csv"
    with audit.open() as stream:
        assert list(csv.DictReader(stream))


def test_real_fixture_provenance_and_original_short_name():
    provenance = json.loads((FIXTURE / "provenance.json").read_text())
    assert provenance["archive_commit"] == "b492f4d7c501fd8cdbd2694d760f2f13892f796c"
    assert provenance["historical_as_of_utc"] == "2026-11-12T10:33:56.067019+00:00"
    for name, metadata in provenance["files"].items():
        assert hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest() == metadata["sha256"]
    with (FIXTURE / "jc_catalog.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1
    assert rows[0]["match_id"] == "jc:2042694"
    assert rows[0]["sellStatus"] == "1" and rows[0]["retained"] == "1"
