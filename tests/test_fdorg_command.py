"""Temporary replay uses synthetic v4 bytes from T09 only; the test suite denies live HTTP."""

import hashlib
import json
import logging
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
            ["--days", "7"],
            "/matches?dateFrom=2026-10-26&dateTo=2026-11-10&competitions=PL",
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
