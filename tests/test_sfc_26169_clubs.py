"""Replay the actual lottery sheets and a time-bounded crop of the Championship feed."""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sfc_publication_support import confirm_report
from test_sfc_official_pipeline import SAMPLES, ReplayClock, _collect_replay, _setup_replay
from zucai import cloud_predict, sfc_club_names
from zucai.cloud_feeds import export_feeds
from zucai.daily_digest import generate
from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Result, SfcIssue, Team
from zucai.sfc_reports import publish_reports, read_report

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = [f"fdorg:{n}" for n in (561959, 561954, 561955, 561956, 561960, 561957)]
KEYS = {"728", "1095", "1163", "1128", "954"}
AT = datetime(2026, 11, 23, 16, 32, tzinfo=UTC)


def seed(db, sample):
    engine = get_engine(db)
    init_db(engine)
    with session_scope(engine) as session:
        teams = {}
        for section, columns in (("fixtures", "fixture_columns"), ("history", "history_columns")):
            for values in sample[section]:
                row = dict(zip(sample[columns], values, strict=True))
                for side in ("home", "away"):
                    name = row.pop(side)
                    if name not in teams:
                        teams[name] = Team(name=name)
                        session.add(teams[name])
                        session.flush()
                    row[side + "_team_id"] = teams[name].id
                result = None
                if section == "history":
                    result = Result(
                        match_id=row["match_id"],
                        **{
                            k: row.pop(k)
                            for k in (
                                "home_goals",
                                "away_goals",
                                "available_utc",
                                "available_source",
                            )
                        },
                    )
                session.add(
                    Match(
                        **row,
                        source=row["match_id"].split(":")[0],
                        league="英冠",
                        status="finished" if result else "scheduled",
                    )
                )
                session.flush()
                if result:
                    session.add(result)
    return engine


def test_collection_forecast_and_digest(tmp_path, monkeypatch):
    sample = json.loads((SAMPLES / "26169-championship.json").read_bytes())
    _setup_replay(tmp_path, monkeypatch)
    monkeypatch.setattr(ReplayClock, "instant", AT)
    db = tmp_path / "data/feed.db"
    db.parent.mkdir()
    engine = seed(db, sample)
    with monkeypatch.context() as before:
        old = {k: v for k, v in sfc_club_names.load().items() if k[0] != "英冠" or k[1] not in KEYS}
        before.setattr(sfc_club_names, "_table", lambda: old)
        _collect_replay(db)
        with session_scope(engine) as session:
            assert [r["match_id"] for r in session.get(SfcIssue, "26169").snapshot[8:]] == [
                None
            ] * 5 + EXPECTED[-1:]

    # The approved path uses the shipped table and checks the provider ID and printed label.
    table = sfc_club_names.load()
    for key in KEYS:
        entry = table["英冠", key]
        assert entry["approval_pr"] == "https://github.com/qw5563680/FOOTBALL/pull/221"
        assert sfc_club_names.club_name("英冠", key, entry["label"]) == entry["canonical"]
        assert sfc_club_names.club_name("英冠", key, entry["canonical"]) is None

    collection, _ = _collect_replay(db)
    with session_scope(engine) as session:
        rows = session.get(SfcIssue, "26169").snapshot[8:]
        assert [r["match_id"] for r in rows] == EXPECTED
        assert [r["candidate_ids"] for r in rows] == [[key] for key in EXPECTED]
        assert all(r["mapping_evidence"]["basis"] == "approved_club_short_names" for r in rows[:5])
    engine.dispose()
    archive = tmp_path / "archive"
    export_feeds(db, archive, date(2026, 11, 24), "replay", collection={"sfc": collection})
    params = json.loads((ROOT / "data/model/params.json").read_bytes())
    params["leagues"] = {"英冠": params["leagues"]["英冠"]}
    params_path = tmp_path / "params.json"
    params_path.write_text(json.dumps(params))
    monkeypatch.setattr(cloud_predict, "_now", lambda: AT + timedelta(minutes=1))
    result = cloud_predict.run(archive, params_path, "replay-26169")
    assert result["forecasts"]["2026-11-25"]["predicted"] == 6
    assert "26169" in publish_reports(archive, now=AT + timedelta(minutes=2))
    original = archive / "sfc/26169/history/replay-26169"
    files = {p: p.read_bytes() for p in original.iterdir()}
    confirm_report(archive, at=(AT + timedelta(minutes=3)).isoformat())
    report = read_report(archive, "sfc/26169")
    text = generate(archive, tmp_path / "daily", now=AT + timedelta(minutes=4)).read_text()
    assert report["published_utc"] == (AT + timedelta(minutes=3)).isoformat()
    assert len(files) == 4 and all(p.read_bytes() == raw for p, raw in files.items())
    for row, match_id in zip(report["rows"][8:], EXPECTED, strict=True):
        assert row["match_id"] == match_id
        probs = row["probabilities"]
        assert set(probs) == set("hda") and abs(sum(probs.values()) - 1) < 1e-8
        assert row["home"] in text and row["away"] in text
        print(
            row["seq"],
            row["home"],
            row["away"],
            match_id,
            " / ".join(f"{probs[k]:.2%}" for k in "hda"),
        )
