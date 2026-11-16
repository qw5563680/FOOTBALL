"""T68 第八节第 9 项：已取消且无赛果的竞彩场次不计入未关联，单列编号。"""

import json

from test_t68_daily_gate_entry import CUTOFF, bundle  # noqa: F401
from test_t68_identity_archive_entry import archive as frozen_archive  # noqa: F401
from test_t68_identity_archive_entry import table
from zucai import identity_daily_gate as gate
from zucai.cloud_feeds import TABLES


def _rows(archive, name):
    values = [
        json.loads(line) for line in (archive / f"state/{name}.jsonl").read_text().splitlines()
    ]
    return [dict(zip(list(TABLES[name]), row, strict=False)) for row in values]


def test_cancelled_without_result_is_listed_separately_not_unresolved(bundle):  # noqa: F811
    archive, root, evidence = bundle
    matches = _rows(archive, "matches")
    original = next(row for row in matches if row["match_id"] == "jc:2042404")
    cancelled = dict(original, match_id="jc:2099999", status="cancelled", counterpart_id=None)
    postponed = dict(original, match_id="jc:2099998", status="postponed", counterpart_id=None)
    cancelled_settled = dict(
        original, match_id="jc:2099997", status="cancelled", counterpart_id=None
    )
    table(archive, "matches", matches + [cancelled, postponed, cancelled_settled])
    results = _rows(archive, "results")
    results.append(
        dict(
            match_id="jc:2099997",
            home_goals=1,
            away_goals=0,
            available_utc="2026-11-14T01:00:00Z",
        )
    )
    table(archive, "results", results)
    result = gate.build_register(
        archive, CUTOFF, codes=["98"], evidence={"98": evidence}, evidence_root=root
    )["leagues"][0]
    section = result["matches"]
    unresolved_ids = [row["jc_id"] for row in section["unresolved"]]
    # Cancelled without any result is not unresolved; it is listed separately.
    assert "jc:2099999" not in unresolved_ids
    assert section["cancelled_without_result_ids"] == ["jc:2099999"]
    assert section["cancelled_without_result_count"] == 1
    # Other statuses without results stay unresolved.
    assert "jc:2099998" in unresolved_ids
    # A cancelled fixture that somehow carries a result also stays unresolved.
    assert "jc:2099997" in unresolved_ids
