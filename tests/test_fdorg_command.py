"""Offline replay of T09 synthetic fixtures and explicitly marked live crops."""

import hashlib
import json
import logging
import runpy
import secrets
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from zucai.cli import main
from zucai.predict.base import reject_cross_source_overlaps

SAMPLE = Path(__file__).resolve().parents[1] / "data/fixtures/t09/synthetic_command.json"
API = "https://api.football-data.org/v4"
SEEN = "2026-11-02T01:02:03Z"


def archive(tmp_path, url):
    path = tmp_path / "archive.json"
    raw = SAMPLE.read_bytes()
    path.write_bytes(raw)
    path.with_suffix(".json.meta.json").write_text(
        json.dumps(
            dict(
                url=url,
                sha256=hashlib.sha256(raw).hexdigest(),
                status_code=200,
                fetched_at_utc=SEEN,
            )
        ),
        encoding="utf-8",
    )
    return path


def call(tmp_path, path, *options):
    return main(
        [
            "fetch",
            "fdorg",
            "--competition",
            "PL",
            "--db",
            str(tmp_path / "db.sqlite"),
            "--audit-dir",
            str(tmp_path),
            "--raw-file",
            str(path),
            *options,
        ]
    )


@pytest.mark.parametrize(
    "mode,url,basis,available",
    [
        (
            ["--season", "2026", "--assume-known-after", "3h"],
            "/competitions/PL/matches?season=2026",
            "assumed",
            "2026-11-01T15:00:00",
        ),
        (
            ["--days", "4"],
            "/matches?dateFrom=2026-10-29&dateTo=2026-11-07&competitions=PL",
            "captured",
            "2026-11-02T01:02:03",
        ),
    ],
)
def test_cli_replays_both_windows_with_actual_knowledge_times(
    tmp_path, monkeypatch, caplog, mode, url, basis, available
):
    monkeypatch.delenv("FOOTBALL_DATA_ORG_TOKEN", raising=False)
    caplog.set_level(logging.INFO)
    path = archive(tmp_path, API + url)
    assert call(tmp_path, path, *mode) == 0
    with sqlite3.connect(tmp_path / "db.sqlite") as db:
        assert db.execute("SELECT status FROM matches ORDER BY match_id").fetchall() == [
            ("finished",),
            ("scheduled",),
        ]
        result = db.execute("SELECT available_utc,available_source FROM results").fetchall()
        assert len(result) == 1 and result[0][1] == basis and result[0][0].startswith(available)
    assert "linked_existing=0" in caplog.text and "linked_fd=0" in caplog.text


def test_bad_or_wrong_scope_replay_fails_before_import(tmp_path):
    path = archive(tmp_path, API + "/competitions/PD/matches?season=2026")
    assert call(tmp_path, path, "--season", "2026", "--assume-known-after", "3h") == 1
    path = archive(tmp_path, API + "/competitions/PL/matches?season=2026")
    path.write_bytes(path.read_bytes() + b" ")
    assert call(tmp_path, path, "--season", "2026", "--assume-known-after", "3h") == 1


def test_season_requires_explicit_assumption_even_with_valid_raw(tmp_path, caplog):
    path = archive(tmp_path, API + "/competitions/PL/matches?season=2026")
    assert call(tmp_path, path, "--season", "2026") == 1
    assert "--assume-known-after" in caplog.text
    assert not (tmp_path / "db.sqlite").exists()


@pytest.mark.parametrize(
    "peer,delta,conflict", [("jc", 180, True), ("fd", 180, True), ("fd", 181, False)]
)
def test_later_higher_priority_source_cannot_double_train(peer, delta, conflict):
    match = json.loads(SAMPLE.read_bytes())["matches"][0]
    stamp = pd.Timestamp(match["utcDate"])
    frame = pd.DataFrame(
        [
            dict(
                league=match["competition"]["code"],
                home=match["homeTeam"]["name"],
                away=match["awayTeam"]["name"],
                source=src,
                match_id=key,
                kickoff_utc=k,
            )
            for src, key, k in [
                (peer, peer + ":peer", stamp + pd.Timedelta(minutes=delta)),
                ("fdorg", "fdorg:" + str(match["id"]), stamp),
            ]
        ]
    )
    if conflict:
        with pytest.raises(ValueError, match="重叠|重复"):
            reject_cross_source_overlaps(frame)
    else:
        reject_cross_source_overlaps(frame)


def test_acceptance_census_and_crop_isolate_malformed_observations(tmp_path, monkeypatch):
    from zucai.sources.base import RawStore
    from zucai.sources.football_data_org import FootballDataOrg

    monkeypatch.setenv("FOOTBALL_DATA_ORG_TOKEN", secrets.token_hex(24))
    fields = json.loads((SAMPLE.parent / "synthetic_capture_edges.json").read_bytes())
    payload = json.loads(SAMPLE.read_bytes())
    scheduled = payload["matches"][1]
    scheduled["score"] = fields["non_object_score"]
    payload["matches"] += [
        fields["bad_row"],
        dict(scheduled, id=scheduled["id"] + 1, score={"duration": fields["non_string_duration"]}),
    ]
    payload["resultSet"]["count"] = len(payload["matches"])
    source = FootballDataOrg(store=RawStore(tmp_path / "raw"))
    helpers = runpy.run_path(str(SAMPLE.parents[3] / "examples/t09_capture.py"))
    try:
        path = source.store.save(
            source.name,
            "matches",
            json.dumps(payload).encode(),
            "json",
            url=API + "/competitions/PL/matches?season=2026",
            status_code=200,
        )
        rows, durations, rejected = helpers["_observations"](
            source, path, competition="PL", season=2026
        )
        assert len(rows) == 3 and len(rejected) == 1
        assert durations == {"REGULAR": 1, "missing": 2}
        dest = tmp_path / "cropped.json"
        info = helpers["_crop"](source, path, dest)
        assert info["source_rows"] == 4 and info["valid_rows"] == 3 and info["rejected_rows"] == 1
        assert json.loads(dest.read_bytes())["resultSet"]["count"] == len(info["copied_ids"])
        assert not any(
            game.status == "FINISHED" and game.full is None
            for game in source.parse(dest.read_bytes())
        )
        assert source.rejected == []
    finally:
        source.session.close()


@pytest.mark.parametrize(
    "home,away,delta,conflict",
    [
        ("same", "unmapped", 180, True),
        ("unmapped", "same", 180, True),
        ("different", "unmapped", 180, True),
        ("different", "verified", 180, False),
        ("same", "unmapped", 181, False),
    ],
)
def test_later_official_fixture_cannot_double_train_partial_or_reversed_ids(
    home, away, delta, conflict
):
    event = json.loads(SAMPLE.read_bytes())["matches"][0]
    stamp = pd.Timestamp(event["utcDate"])
    labels = {
        "same": event["homeTeam"]["name"],
        "unmapped": f"fdorg:team:{event['awayTeam']['id']}",
        "different": "Separate home",
        "verified": "Separate away",
    }
    frame = pd.DataFrame(
        [
            dict(
                league="PL",
                source="fdorg",
                match_id=f"fdorg:{event['id']}",
                kickoff_utc=stamp,
                home=labels[home],
                away=labels[away],
            ),
            dict(
                league="PL",
                source="jc",
                match_id="jc:later",
                kickoff_utc=stamp + pd.Timedelta(minutes=delta),
                home=event["homeTeam"]["name"],
                away=event["awayTeam"]["name"],
            ),
        ]
    )
    if conflict:
        with pytest.raises(ValueError, match="重复|重叠"):
            reject_cross_source_overlaps(frame)
    else:
        reject_cross_source_overlaps(frame)


def test_crop_retains_verified_top_level_scope(tmp_path, monkeypatch):
    from zucai.sources.base import RawStore
    from zucai.sources.football_data_org import FootballDataOrg

    monkeypatch.setenv("FOOTBALL_DATA_ORG_TOKEN", secrets.token_hex(24))
    payload = json.loads(SAMPLE.read_bytes())
    payload["competition"] = payload["matches"][0]["competition"]
    payload["filters"] = {"season": "2026"}
    for row in payload["matches"]:
        row.pop("competition")
        row.pop("season")
    source = FootballDataOrg(store=RawStore(tmp_path / "raw"))
    try:
        path = source.store.save(
            source.name,
            "matches",
            json.dumps(payload).encode(),
            "json",
            url=API + "/competitions/PL/matches?season=2026",
            status_code=200,
        )
        crop = runpy.run_path(str(SAMPLE.parents[3] / "examples/t09_capture.py"))["_crop"]
        dest = tmp_path / "clean.json"
        crop(source, path, dest)
        games = source.parse(dest.read_bytes(), competition="PL", season=2026)
        assert len(games) == len(payload["matches"]) and not source.rejected
    finally:
        source.session.close()
