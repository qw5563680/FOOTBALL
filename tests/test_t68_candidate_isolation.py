"""Only safely attributable rejected originals may spare other competitions."""

import json
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256

import pytest

from test_cloud_aliases import FIXTURES, alias_inputs, capture
from zucai.cloud_aliases import write_candidates
from zucai.sources.base import RawStore


def mixed_capture(raw_dir, case):
    doc = json.loads((FIXTURES / "apif.json").read_bytes())
    row = doc["response"][0]
    row["fixture"].update(timestamp=int(datetime(2026, 11, 1, 12, tzinfo=UTC).timestamp()))
    bad = deepcopy(row)
    bad["fixture"].update(id=123456, status={"short": "UNKNOWN"})
    bad["league"]["id"] = 292 if case == "other" else 98
    if case == "unknown":
        bad.pop("league")
    elif case == "bool":
        bad["league"]["id"] = True
    elif case == "row":
        bad = []
    doc["response"].append(bad)
    doc["results"] = 2
    if case == "pages":
        doc["paging"]["total"] = 2
    raw = json.dumps(doc).encode()
    RawStore(raw_dir).save(
        "api-football",
        "mixed",
        raw,
        "json",
        status_code=200,
        captured_at=datetime(2026, 11, 3, tzinfo=UTC),
    )
    return dict(scope={"date": "2026-11-01"}, raw_sha256=sha256(raw).hexdigest())


@pytest.mark.parametrize(
    "case,expected",
    [("other", 2), ("same", 0), ("unknown", 0), ("bool", 0), ("row", 0), ("pages", 0)],
)
def test_raw_league_attribution_is_required(tmp_path, case, expected):
    db, raw, out = alias_inputs(tmp_path, "apif", "2026-11-01T12:00:00+00:00")
    assert write_candidates("apif", db, raw, [mixed_capture(raw, case)], out) == expected


def test_known_other_league_rejection_preserves_exact_legacy_proposal(tmp_path):
    db, raw, out = alias_inputs(tmp_path, "apif", "2026-11-01T12:00:00+00:00")
    first = capture(raw, "apif", "2026-11-01T12:00:00+00:00")
    assert write_candidates("apif", db, raw, [first], out) == 2
    previous = json.loads((out / "apif.json").read_bytes())
    mixed = mixed_capture(raw, "other")
    assert write_candidates("apif", db, raw, [mixed], out) == 0
    after = json.loads((out / "apif.json").read_bytes())
    for key, old in previous["日职"].items():
        assert {k: v for k, v in old.items() if k != "evidence"} == {
            k: v for k, v in after["日职"][key].items() if k != "evidence"
        }
        assert all(item in after["日职"][key]["evidence"] for item in old["evidence"])


@pytest.mark.parametrize("numeric_id,expected", [(2014, 2), (None, 0), (True, 0), (999999, 0)])
def test_fdorg_uses_checked_numeric_competition_id(tmp_path, numeric_id, expected):
    db, raw_dir, out = alias_inputs(tmp_path, "fdorg", "2026-11-01T12:00:00+00:00")
    doc = json.loads((FIXTURES / "fdorg.json").read_bytes())
    doc["matches"][0]["utcDate"] = "2026-11-01T12:00:00+00:00"
    bad = deepcopy(doc["matches"][0])
    bad.update(id=123456, utcDate="invalid")
    bad["competition"] = {"id": numeric_id, "code": "PD"}
    doc["matches"].append(bad)
    doc["resultSet"]["count"] = 2
    raw = json.dumps(doc).encode()
    RawStore(raw_dir).save(
        "football-data-org",
        "mixed",
        raw,
        "json",
        status_code=200,
        captured_at=datetime(2026, 11, 3, tzinfo=UTC),
    )
    captures = [
        dict(
            scope=dict(competition="PL,PD", date_from="2026-11-01", date_to="2026-11-02"),
            raw_sha256=sha256(raw).hexdigest(),
        )
    ]
    assert write_candidates("fdorg", db, raw_dir, captures, out) == expected
