"""Synthetic fixture replay through the real feed and forecast readers."""

import csv
import json
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from forecast_publication_support import confirm_fixture
from zucai.cloud_feeds import digest, export_feeds
from zucai.db import get_engine, init_db, session_scope
from zucai.forecast_archive import publish_forecasts
from zucai.models import Match, Odds, Result, Team
from zucai.report.render import write_report
from zucai.result_source_links import record_result_link
from zucai.sources.sporttery import CATALOG_FIELDS

NOW = datetime(2026, 11, 3, 10, tzinfo=UTC)
NEW_CAPTURE = NOW - timedelta(minutes=10)
OLD_CAPTURE = NOW - timedelta(minutes=40)
DAY = NOW.date().isoformat()
SOURCE = Path(__file__).resolve().parents[1] / "data/fixtures/t25/forecast.json"


def _catalog_row(index, kickoff, *, captured=NEW_CAPTURE, **changes):
    return dict(
        {
            "business_date": DAY,
            "jc_num": f"周二{index:03}",
            "match_id": f"jc:{index}",
            "matchId": str(index),
            "home": "合成主队",
            "away": "合成客队",
            "league": "测试联赛",
            "matchStatus": "Selling",
            "sellStatus": "1",
            "retained": "1",
            "has_fixed_odds": "1",
            "captured_utc": captured.isoformat(),
            "kickoff_utc": kickoff.isoformat(),
        },
        **changes,
    )


def _forecast(archive, stage, run_id, cutoff, rows, published):
    batch = json.loads(SOURCE.read_bytes())
    base = batch["predictions"][0]
    model_path = f"models/{run_id}/测试联赛.pkl"
    train_path = f"models/{run_id}/测试联赛.train.json"
    (stage / model_path).parent.mkdir(parents=True)
    (stage / model_path).write_bytes(b"t25-offline-model")
    (stage / train_path).write_text("{}")
    predictions = []
    for row in rows:
        prediction = deepcopy(base)
        prediction.update({key: row[key] for key in ("match_id", "jc_num", "kickoff_utc")})
        if row["match_id"] == "jc:1":
            prediction["market"] = {
                "prices": {"h": 2.0, "d": 4.0, "a": 4.0},
                "probabilities": {"h": 0.5, "d": 0.25, "a": 0.25},
                "snapshot_utc": OLD_CAPTURE.isoformat(),
            }
        predictions.append(prediction)
    stamp = cutoff.isoformat()
    forecasts = {}
    for i in range(2):
        day = (NOW.date() + timedelta(days=i)).isoformat()
        frozen = {
            **batch,
            "batch_id": f"{run_id}-{day}",
            "as_of_utc": stamp,
            "generated_utc": stamp,
            "input": {**batch["input"], "kickoff_date_beijing": day},
            "predictions": [] if i else predictions,
        }
        directory = f"forecasts/{day}/{run_id}"
        write_report(frozen, stage / directory)
        forecasts[day] = dict(path=directory, batch_id=frozen["batch_id"], generated_utc=stamp)
    publish_forecasts(
        archive,
        stage,
        {
            "run_id": run_id,
            "generated_utc": stamp,
            "database_sha256": "b" * 64,
            "leagues": {
                "测试联赛": {
                    "status": "trained",
                    "model_path": model_path,
                    "training_json_path": train_path,
                    "sha256": digest(stage / model_path),
                    "training_database_sha256": "b" * 64,
                }
            },
            "forecasts": forecasts,
        },
    )
    confirm_fixture(archive, published=published.isoformat(), only_unpublished=True)


def _add_result(session, name, kickoff, *, source="jc", league="测试杯", **changes):
    match_id = f"{source}:{name}"
    away = changes.pop("away_team_id", 3)
    session.add(
        Match(
            match_id=match_id,
            source=source,
            league=league,
            home_team_id=1,
            away_team_id=away,
            kickoff_utc=kickoff,
            status="finished",
        )
    )
    session.flush()
    session.add(
        Result(
            match_id=match_id,
            **{
                "home_goals": 1,
                "away_goals": 0,
                "available_utc": kickoff + timedelta(hours=3),
                "available_source": "captured",
                **changes,
            },
        )
    )


@pytest.fixture
def web_target_archive(tmp_path):
    archive = tmp_path / "archive"
    database = tmp_path / "work" / "feeds.db"
    engine = get_engine(database)
    init_db(engine)
    kickoffs = [NOW + timedelta(hours=hour) for hour in (2, 1, 3, 4, 5, 6, 7)]
    rows = [_catalog_row(i, kickoff) for i, kickoff in enumerate(kickoffs, 1)]
    with session_scope(engine) as session:
        session.add_all(
            Team(id=i, name=name) for i, name in enumerate(("合成主队", "合成客队", "合成对手"), 1)
        )
        session.flush()
        session.get(Team, 1).aliases = [
            "合成主队足球俱乐部",
            "Home FC",
            "Home U21",
            "合成主队女足",
            "合成主队B队",
            "apif:测试联赛:team:41",
        ]
        session.get(Team, 2).aliases = ["合成客队FC", "jc:team:901"]
        for i, kickoff in enumerate(kickoffs, 1):
            session.add(
                Match(
                    match_id=f"jc:{i}",
                    source="jc",
                    league="测试联赛",
                    jc_num=rows[i - 1]["jc_num"],
                    home_team_id=1,
                    away_team_id=2,
                    kickoff_utc=kickoff + timedelta(minutes=30 if i == 6 else 0),
                    status="cancelled" if i == 3 else "scheduled",
                )
            )
        for i in (1, 2):
            provider = Match(
                match_id=f"apif:{40 + i}",
                source="apif",
                league="测试联赛",
                home_team_id=1,
                away_team_id=2,
                kickoff_utc=kickoffs[i - 1],
                status="scheduled",
            )
            session.add(provider)
            session.flush()
            verified = NOW + timedelta(minutes=-2 if i == 1 else 1)
            record_result_link(session, provider, session.get(Match, f"jc:{i}"), verified)
        session.flush()
        for stamp, prices in (
            (OLD_CAPTURE, (2.0, 4.0, 4.0)),
            (NEW_CAPTURE, (2.2, 3.5, 3.6)),
            (NOW - timedelta(minutes=5), (9.0,)),
            (NOW + timedelta(minutes=1), (9.0, 9.0, 9.0)),
            (kickoffs[0], (8.0, 8.0, 8.0)),
        ):
            session.add_all(
                Odds(match_id="jc:1", pool="had", selection=key, price=price, snapshot_utc=stamp)
                for key, price in zip("hda", prices, strict=False)
            )
        for i in range(1, 13):
            _add_result(
                session,
                f"past-{i}",
                NOW - timedelta(days=i),
                league="测试杯" if i == 1 else "测试联赛",
                away_team_id=2 if i == 2 else 3,
            )
        _add_result(session, "same", NOW - timedelta(days=1), source="fd")
        _add_result(session, "friendly", NOW - timedelta(hours=4), league="国际友谊赛")
        _add_result(session, "unseen", NOW - timedelta(hours=2))
        _add_result(session, "assumed", NOW - timedelta(hours=12), available_source="assumed")
    engine.dispose()
    catalog = database.parent / "state/audit/jc_catalog/2026-11.csv"
    catalog.parent.mkdir(parents=True)
    catalog_rows = [
        *rows[:3],
        {**rows[3], "matchStatus": "Stopped", "retained": "0"},
        {**rows[4], "captured_utc": OLD_CAPTURE.isoformat()},
        {**rows[5], "kickoff_utc": (kickoffs[5] + timedelta(minutes=30)).isoformat()},
        {**rows[6], "captured_utc": (NOW + timedelta(minutes=10)).isoformat()},
    ]
    with catalog.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=CATALOG_FIELDS)
        writer.writeheader()
        writer.writerows(catalog_rows)
    feed = export_feeds(
        database,
        archive,
        date.fromisoformat(DAY),
        "offline-feed",
        collection={
            "jc": {"schedule_status": "complete", "schedule_updated_utc": NEW_CAPTURE.isoformat()}
        },
    )
    feed["created_utc"] = (NOW - timedelta(minutes=1)).isoformat()
    (archive / "feed_manifest.json").write_text(json.dumps(feed))
    _forecast(
        archive,
        tmp_path / "released-stage",
        "released",
        NOW - timedelta(minutes=20),
        rows,
        NOW - timedelta(minutes=15),
    )
    _forecast(
        archive,
        tmp_path / "not-published-stage",
        "not-published",
        NOW - timedelta(minutes=3),
        rows,
        NOW + timedelta(minutes=1),
    )
    return archive


def test_research_targets_require_current_official_sale_and_published_identity(web_target_archive):
    from zucai.web_research_targets import select

    archive = web_target_archive
    before = {path: digest(path) for path in archive.rglob("*") if path.is_file()}
    rows = select(archive, NOW)
    assert [row["match_id"] for row in rows] == ["jc:2", "jc:1"]
    assert select(archive, NOW, forecast_run_id="released") == rows
    assert select(archive, NOW, forecast_run_id="not-published") == []
    assert [row["match_id"] for row in select(archive, NOW + timedelta(hours=1))] == ["jc:1"]
    for row in rows:
        assert row["forecast_run_id"] == "released"
        assert row["forecast_sha256"] == digest(archive / row["forecast_path"])
        assert (
            row["forecast_publication_evidence"]["published_utc"] == row["forecast_published_utc"]
        )
    assert {path: digest(path) for path in archive.rglob("*") if path.is_file()} == before


def test_recent_and_odds_keep_actual_availability_and_source(web_target_archive):
    from zucai.web_research_targets import select

    second = select(web_target_archive, NOW)[1]
    data = second["material"]
    assert data["catalog_captured_utc"] == NEW_CAPTURE.isoformat()
    assert data["kickoff_beijing"] == "2026-11-03T20:00:00+08:00"
    assert len(data["top_scores"]) == 5
    assert data["half_full_time"] is None
    assert [r["match_id"] for r in data["recent"]["home"]] == [f"jc:past-{i}" for i in range(1, 11)]
    assert data["recent"]["home"][0]["league"] == "测试杯"
    assert data["recent"]["home"][0]["source"] == "jc"
    assert [r["match_id"] for r in data["recent"]["away"]] == ["jc:past-2"]
    assert [r["match_id"] for r in second["verification_recent"]["home"]] == [
        f"jc:past-{i}" for i in range(1, 13)
    ]
    assert data["odds"] == {"h": 2.2, "d": 3.5, "a": 3.6}
    assert datetime.fromisoformat(data["odds_snapshot_utc"]) == NEW_CAPTURE
    assert second["market_probabilities"] == {"h": 0.5, "d": 0.25, "a": 0.25}


def test_identity_context_keeps_archive_scope_and_timed_fixture_proofs(
    web_target_archive, monkeypatch
):
    from zucai import web_research_targets as targets

    monkeypatch.setattr(
        targets,
        "_identity_alias_config",
        lambda: {
            "apif": {
                "测试联赛": {
                    "41": {
                        "name": "Home FC",
                        "canonical": "合成主队",
                        "status": "approved",
                        "approved_utc": OLD_CAPTURE.isoformat(),
                    },
                    "501": {
                        "name": "Late FC",
                        "canonical": "合成客队",
                        "status": "approved",
                        "approved_utc": (NOW + timedelta(minutes=5)).isoformat(),
                    },
                }
            }
        },
    )
    first, second = targets.select(web_target_archive, NOW)
    assert "identity_context" not in first["material"]
    assert "home_team_id" not in first["material"]["recent"]["home"][0]
    assert "home_team_id" not in first["verification_recent"]["home"][0]
    local = second["identity_context"]
    assert local["target"]["match_id"] == "jc:1"
    assert (local["target"]["home_team_id"], local["target"]["away_team_id"]) == (1, 2)
    teams = {team["id"]: team for team in local["teams"]}
    assert {team["name"] for team in teams.values()} >= {"合成主队", "合成客队", "合成对手"}
    home_aliases = [row for row in teams[1]["aliases"] if row["name"] == "Home FC"]
    assert [(row["league"], row["source"]) for row in home_aliases] == [("测试联赛", "apif")]
    assert {row["name"] for row in teams[1]["aliases"]} == {"合成主队足球俱乐部", "Home FC"}
    assert any(row["name"] == "合成客队FC" for row in teams[2]["aliases"])
    assert all(row["name"] != "Late FC" for row in teams[2]["aliases"])
    assert any(row["source"] == "jc" and row["id"] == "901" for row in teams[2]["provider_ids"])
    assert not any(row["source"] == "apif" for row in teams[2]["provider_ids"])
    fixtures = {row["id"]: row for row in local["fixtures"]}
    assert fixtures["jc:1"]["fixture_ids"] == ["apif:41", "jc:1"]
    assert fixtures["jc:past-1"]["away_team_id"] == 3
    other = {row["id"]: row for row in first["identity_context"]["fixtures"]}
    assert other["jc:2"]["fixture_ids"] == ["jc:2"]


def test_future_feed_snapshot_cannot_supply_team_aliases_or_external_links(web_target_archive):
    from zucai import web_research_targets as targets

    manifest = web_target_archive / "feed_manifest.json"
    feed = json.loads(manifest.read_bytes())
    feed["created_utc"] = (NOW + timedelta(minutes=1)).isoformat()
    manifest.write_text(json.dumps(feed))
    row = targets.select(web_target_archive, NOW)[1]["identity_context"]
    assert all(not team["aliases"] and not team["provider_ids"] for team in row["teams"])
    match = next(match for match in row["fixtures"] if match["id"] == "jc:1")
    assert match["fixture_ids"] == ["jc:1"]


@pytest.mark.parametrize(
    "changes",
    [
        {"schedule_status": "failed"},
        {"schedule_updated_utc": (NOW - timedelta(minutes=8)).isoformat()},
        {"schedule_updated_utc": (NOW + timedelta(minutes=10)).isoformat()},
    ],
)
def test_failed_empty_or_future_sale_snapshot_cannot_reuse_stale_positive_rows(
    web_target_archive, changes
):
    from zucai.web_research_targets import select

    manifest_path = web_target_archive / "feed_manifest.json"
    payload = json.loads(manifest_path.read_bytes())
    payload["collection"]["jc"].update(changes)
    manifest_path.write_text(json.dumps(payload))
    assert select(web_target_archive, NOW) == []
