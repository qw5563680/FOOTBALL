import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from zucai.sources.api_common import ApiGame

ROOT = Path(__file__).resolve().parents[1] / "data/fixtures/t10"


def test_only_mutually_unique_oriented_fixture_evidence_activates_ids():
    from zucai.sources.apif_verify import infer_aliases

    d = json.loads((ROOT / "synthetic_ingest.json").read_bytes())["fixture"]
    d["kickoff_utc"] = datetime.fromisoformat(d["kickoff_utc"])
    d["full"] = tuple(d["full"])
    d["half"] = tuple(d["half"])
    game = ApiGame(**d)
    official = [
        dict(match_id="jc:1", home="北港", away="东岸", kickoff_utc=game.kickoff_utc.isoformat())
    ]
    aliases = infer_aliases(
        [game], official, provenance={game.provider_match_id: {"raw_sha256": "a" * 64}}
    )
    assert aliases["551"]["status"] == "auto_verified"
    assert aliases["552"]["canonical"] == "东岸"
    assert aliases["551"]["evidence"][0]["counterpart_id"] == "jc:1"
    ambiguous = infer_aliases(
        [game, replace(game, provider_match_id=99999, home_provider_id=777, away_provider_id=778)],
        official,
    )
    assert all(v["status"] == "proposed" for v in ambiguous.values())


def test_prior_active_record_preserved_and_unknown_not_guessed():
    from zucai.sources.apif_verify import infer_aliases

    d = json.loads((ROOT / "synthetic_ingest.json").read_bytes())["fixture"]
    d["kickoff_utc"] = datetime(2026, 10, 10, 14, tzinfo=UTC)
    d["full"] = tuple(d["full"])
    d["half"] = tuple(d["half"])
    prior = {"551": {"name": "old label", "status": "approved", "canonical": "北港"}}
    records = infer_aliases([ApiGame(**d)], [], aliases=prior)
    assert records["551"] == prior["551"]
    assert records["552"]["status"] == "proposed" and records["552"]["canonical"] is None
