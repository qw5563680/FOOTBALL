import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import select

from zucai.db import get_engine, session_scope
from zucai.models import Match, Result
from zucai.sources.base import RawStore

ROOT = Path(__file__).resolve().parents[1] / "data/fixtures/t10"


def test_season_requires_assumption_before_db_or_network(tmp_path):
    from zucai.sources.apif_command import fetch_apif

    with pytest.raises(ValueError, match="assume-known-after"):
        fetch_apif(league_id=98, seasons=[2024], db=tmp_path / "absent.db")
    assert not (tmp_path / "absent.db").exists()


def test_replay_season_and_date_availability(tmp_path):
    from zucai.sources.apif_command import fetch_apif

    doc = json.loads((ROOT / "synthetic_season.json").read_bytes())
    doc["response"] = [doc["response"][3]]
    doc["results"] = 1
    seen = datetime(2026, 11, 1, tzinfo=UTC)
    aliases = ROOT / "synthetic_aliases.json"
    for mode in ("season", "date"):
        payload = json.loads(json.dumps(doc))
        params = {"league": "98", "season": "2024"} if mode == "season" else {"date": "2024-09-16"}
        payload["parameters"] = params
        url = "https://v3.football.api-sports.io/fixtures?" + (
            "&".join(k + "=" + v for k, v in params.items())
        )
        p = RawStore(tmp_path / "raw").save(
            "api-football",
            mode,
            json.dumps(payload).encode(),
            "json",
            captured_at=seen,
            url=url,
            status_code=200,
        )
        db = tmp_path / (mode + ".db")
        kwargs = (
            dict(league_id=98, seasons=[2024], assume=timedelta(hours=3))
            if mode == "season"
            else dict(date="2024-09-16")
        )
        report = fetch_apif(
            **kwargs, raw_file=p, db=db, aliases_path=aliases, audit_dir=tmp_path / "audit"
        )
        assert report["requests"] == 0 and report["inserted"] == 1
        engine = get_engine(db)
        with session_scope(engine) as s:
            row = s.scalars(select(Result)).one()
            assert row.available_source == "captured"  # T63 does not backdate new paid results.
            assert s.scalars(select(Match)).one().source == "apif"
        engine.dispose()
        meta = p.with_suffix(".json.meta.json")
        m = json.loads(meta.read_bytes())
        m["sha256"] = "0" * 64
        meta.write_text(json.dumps(m))
        with pytest.raises(ValueError, match="SHA256"):
            fetch_apif(**kwargs, raw_file=p, db=db, aliases_path=aliases)


def test_cli_registers_new_command_without_changing_legacy(monkeypatch):
    import zucai.sources.apif_command as command
    from zucai.cli import build_parser, main

    args = build_parser().parse_args(
        ["fetch", "apif", "--league", "98", "--season", "2024", "--assume-known-after", "3h"]
    )
    assert args.feed == "apif" and args.reserve == 10
    called = []
    monkeypatch.setattr(command, "fetch_apif", lambda **kw: called.append(kw) or {})
    assert main(["fetch", "apif", "--date", "2024-09-16"]) == 0
    assert called[0]["date"] == "2024-09-16"
    assert main(["version"]) == 0


@pytest.mark.parametrize("source", ["jc", "fd", "fdorg"])
@pytest.mark.parametrize(
    "pair", [("北港", "东岸"), ("北港", "西山"), ("apif:team:1", "apif:team:2")]
)
def test_late_higher_priority_overlap_is_rejected(source, pair):
    from zucai.predict.base import reject_cross_source_overlaps

    frame = pd.DataFrame(
        [
            dict(
                match_id="apif:1",
                source="apif",
                league="日职",
                home=pair[0],
                away=pair[1],
                kickoff_utc=pd.Timestamp("2024-09-16T08:00:00Z"),
            ),
            dict(
                match_id=source + ":2",
                source=source,
                league="日职",
                home="北港",
                away="东岸",
                kickoff_utc=pd.Timestamp("2024-09-16T08:00:00Z"),
            ),
        ]
    )
    with pytest.raises(ValueError, match="apif:1"):
        reject_cross_source_overlaps(frame)


def test_opaque_fdorg_also_blocks_training_with_apif():
    from zucai.predict.base import reject_cross_source_overlaps

    frame = pd.DataFrame(
        [
            dict(
                match_id="apif:1",
                source="apif",
                league="日职",
                home="北港",
                away="东岸",
                kickoff_utc=pd.Timestamp("2024-09-16T08:00:00Z"),
            ),
            dict(
                match_id="fdorg:2",
                source="fdorg",
                league="日职",
                home="fdorg:team:71",
                away="fdorg:team:72",
                kickoff_utc=pd.Timestamp("2024-09-16T08:00:00Z"),
            ),
        ]
    )
    with pytest.raises(ValueError, match="fdorg:2"):
        reject_cross_source_overlaps(frame)


def test_incomplete_page_replay_cannot_establish_fixture_links(tmp_path):
    from zucai.sources.apif_command import fetch_apif

    raw = (ROOT / "synthetic_page1.json").read_bytes()
    p = RawStore(tmp_path / "raw").save(
        "api-football",
        "incomplete",
        raw,
        "json",
        captured_at=datetime(2026, 11, 1, tzinfo=UTC),
        url="https://v3.football.api-sports.io/fixtures?league=98&season=2024",
        status_code=200,
    )
    with pytest.raises(ValueError, match="分页"):
        fetch_apif(
            league_id=98,
            seasons=[2024],
            assume=timedelta(hours=3),
            raw_file=p,
            db=tmp_path / "not-created.db",
            aliases_path=ROOT / "synthetic_aliases.json",
        )
    assert not (tmp_path / "not-created.db").exists()
