"""Replay match 14 of archived SFC issue 26172 against a pre-cutoff Portuguese crop."""

import json
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from zucai.cloud_predict import open_national_fixtures
from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Result, Team
from zucai.predict.command import train
from zucai.report.batch import predict_batch
from zucai.sources.c500 import SfcFixture, SfcSnapshot
from zucai.sources.sfc import ingest_issue

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "data/fixtures/t103/26172-portugal.json"
FDORG = "fdorg:567361"
C500 = "c500:1438210"


def seed_archive(session, sample):
    for name, model in (("teams", Team), ("matches", Match), ("results", Result)):
        for values in sample[name]:
            row = dict(zip(sample["columns"][name], values, strict=True))
            if name == "teams":
                row["aliases"] = json.loads(row["aliases"])
            session.add(model(**row))
        session.flush()


def replay_issue(session, sample, cutoff, model_path):
    fixtures = []
    for values in sample["fixtures"]:
        row = dict(zip(sample["fixture_columns"], values, strict=True))
        fixtures.append(
            SfcFixture(
                seq=row["seq"],
                fixture_id=row["fixture_id"],
                league=row["league"],
                home=row["home"],
                away=row["away"],
                kickoff_utc=datetime.fromisoformat(row["source_kickoff_utc"]),
                ended=row["source_ended"],
                home_id=row["provider_home_id"],
                away_id=row["provider_away_id"],
            )
        )
    issue = sample["issue"]
    snapshot = ingest_issue(
        session,
        SfcSnapshot(
            issue["issue_no"],
            datetime.fromisoformat(issue["sale_end_utc"]),
            fixtures,
            issue["date_basis"],
        ),
        observed=datetime.fromisoformat(issue["observed_utc"]),
        url=issue["source_url"],
        raw_sha256=issue["raw_sha256"],
        mapped_only=True,
        own_fixtures=True,
    )
    row = snapshot.snapshot[13]
    assert row["seq"] == 14 and row["mapping"] == "matched"
    targets = open_national_fixtures(session, cutoff)
    assert targets == {date(2026, 11, 28): {row["match_id"]}}
    ids = targets[date(2026, 11, 28)]
    (prediction,) = predict_batch(
        session,
        model_files=[model_path],
        as_of=cutoff,
        generated=cutoff,
        match_ids=sorted(ids),
        sfc_only_ids=ids,
        persist=False,
    )["predictions"]
    return prediction


def test_26172_archived_fdorg_fixture_matches_c500_probabilities(tmp_path):
    sample = json.loads(SAMPLE.read_bytes())
    cutoff = datetime.fromisoformat(sample["provenance"]["replay_cutoff_utc"])
    assert sample["archived_row_14"]["mapping"] == "unmatched"
    database = tmp_path / "portugal.db"
    engine = get_engine(database)
    init_db(engine)
    with session_scope(engine) as session:
        seed_archive(session, sample)
    model_path = tmp_path / "portugal.pkl"
    result = train(
        db=database,
        league="葡超",
        source="fd,jc",
        as_of=cutoff,
        since=cutoff - timedelta(days=56),
        output=model_path,
    )
    assert result["training_rows"] == 65
    assert result["training_source_counts"] == {"fd": 37, "jc": 28}
    with Session(engine) as session:
        c500 = replay_issue(session, sample, cutoff, model_path)
        assert (c500["match_id"], c500["source"], c500["status"]) == (C500, "c500", "predicted")
        assert not c500["metadata"]["unknown_team_prior"]

    with Session(engine) as session:
        assert session.get(Match, C500) is None
        target = session.get(Match, FDORG)
        assert session.get(Team, target.away_team_id).name == "fdorg:team:5529"
        canonical = session.get(Team, 503)
        assert canonical.name == "葡国民"
        # Simulate coalescence on this isolated fixture, retaining the archived placeholder row.
        target.away_team_id = canonical.id
        session.flush()
        fdorg = replay_issue(session, sample, cutoff, model_path)

    assert (fdorg["match_id"], fdorg["source"]) == (FDORG, "fdorg")
    assert fdorg["status"] == "predicted", fdorg["reason"]
    assert not fdorg["metadata"]["unknown_team_prior"]
    assert fdorg["model_version"] == c500["model_version"]
    assert fdorg["model_probabilities"] == c500["model_probabilities"]
    assert fdorg["score_matrix"] == c500["score_matrix"]
