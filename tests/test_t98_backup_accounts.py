"""Paper decisions require the official schedule in the original frozen batch."""

import json
from copy import deepcopy
from datetime import timedelta

import pytest

from test_parlay_paper import BEFORE
from test_parlay_paper import document as document
from test_parlay_single_double import FIX
from test_scoreboard_paper import DECISION, paper_samples
from zucai.parlay.archive import load_documents, raw_json
from zucai.parlay.paper import advance_ledger as advance_parlay
from zucai.parlay.paper import publication_view as parlay_view
from zucai.parlay.single_double import build
from zucai.scoreboard.paper import advance_ledger, publication_view

BACKUP = {
    "url": "https://trade.500.com/jczq/",
    "observed_utc": "2026-11-01T08:00:00+00:00",
    "raw_sha256": "a" * 64,
    "sales_date": "2026-11-01",
}


def _virtual_legs(ledger):
    return {
        (leg["match_id"], row["batch_id"])
        for row in ledger
        if row["record_type"] == "ticket"
        for leg in row["legs"]
    }


def test_backup_is_excluded_from_all_virtual_accounts_and_official_batch_can_enter():
    forecasts, results = paper_samples()
    target = next(row for row in forecasts if row["match_id"] == "jc:one")
    initial = advance_ledger(forecasts, results, [], DECISION)
    assert (target["match_id"], target["batch_id"]) in _virtual_legs(initial)
    target["jc_backup"] = BACKUP

    pending = advance_ledger(forecasts, results, [], DECISION)
    assert {mid for mid, _ in _virtual_legs(pending)} == {"jc:two", "jc:three"}
    filtered, _ = publication_view(forecasts, results, initial, DECISION)
    assert target["match_id"] not in {mid for mid, _ in _virtual_legs(filtered)}

    official = {key: deepcopy(value) for key, value in target.items() if key != "jc_backup"}
    at = DECISION + timedelta(hours=1)
    official.update(
        batch_id="official-restored",
        generated_utc=at.isoformat(),
        as_of_utc=at.isoformat(),
        published_utc=at.isoformat(),
    )
    replay = advance_ledger([*forecasts, official], results, pending, at)
    assert replay[: len(pending)] == pending
    assert (target["match_id"], "official-restored") in _virtual_legs(replay)
    assert (target["match_id"], target["batch_id"]) not in _virtual_legs(replay)


def test_paper_parlay_freeze_excludes_backup_from_team_and_market_boards():
    sample = json.loads((FIX / "input.json").read_bytes())
    mid = sample["batch"]["predictions"][0]["match_id"]
    assert any(mid in group["match_ids"] for group in build(**sample)["groups"])
    sample["batch"]["predictions"][0]["jc_backup"] = BACKUP

    doc = build(**sample)
    assert any(row["match_id"] == mid for row in doc["diagnostics"])
    assert all(mid not in group["match_ids"] for group in doc["groups"])
    assert all(
        mid != row["match_id"]
        for board in [*doc["boards"], doc["market_reference"]]
        for row in board["matches"]
    )


@pytest.mark.parametrize("source", ["frozen_leg", "parent_forecast"])
def test_parlay_paper_rejects_replayed_backup_and_allows_a_new_official_batch(
    tmp_path, document, source
):
    first = deepcopy(document)
    mid = first["groups"][0]["single"]["match_id"]
    if source == "frozen_leg":
        first["groups"][0]["single"]["jc_backup"] = BACKUP
    else:
        directory = "forecasts/2026-11-06/offline"
        parlay_path = directory + "/parlay_sd.json"
        forecast_path = directory + "/forecast.json"
        (tmp_path / directory).mkdir(parents=True)
        (tmp_path / parlay_path).write_bytes(raw_json(first))
        (tmp_path / forecast_path).write_bytes(
            raw_json({"predictions": [{"match_id": mid, "jc_backup": BACKUP}]})
        )
        entry = {"path": directory, "batch_id": first["batch_id"], "parlay_path": parlay_path}
        manifest = {
            "runs": {"offline": {"forecasts": {"2026-11-06": entry}}},
            "files": {parlay_path: first["sha256"]},
        }
        publications = {
            path: {"published_utc": first["published_utc"]} for path in (parlay_path, forecast_path)
        }
        (first,) = load_documents(tmp_path, manifest, BEFORE, publications=publications)

    ledger = advance_parlay([first], {}, [], BEFORE)
    assert [row["record_type"] for row in ledger] == ["batch"]
    official = deepcopy(document)
    official["batch_id"] = "official-restored"
    official["generated_utc"] = "2026-11-06T02:03:00+00:00"
    official["published_utc"] = "2026-11-06T02:04:00+00:00"
    replay = advance_parlay([first, official], {}, ledger, BEFORE)
    assert replay[: len(ledger)] == ledger
    groups = [row for row in replay if row["record_type"] == "group"]
    assert [row["batch_id"] for row in groups] == ["official-restored"]
    assert mid in groups[0]["frozen"]["match_ids"]
    prior = advance_parlay([document], {}, [], BEFORE)
    view, _ = parlay_view([first], {}, prior, BEFORE)
    assert [row["record_type"] for row in view] == ["batch"]
