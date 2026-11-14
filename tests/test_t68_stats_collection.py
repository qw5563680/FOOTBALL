"""Late daily statistics preserve the daily/backfill collection boundary on export."""

import json

import pytest
import responses

from test_stats_batch import NOW, register
from test_stats_batch import batch as batch
from test_stats_batch import run as run_stats
from zucai.cloud_feeds import archive_info
from zucai.stats_batch import collect_batch
from zucai.stats_schedule import plan


@pytest.mark.parametrize(
    "case", ["complete", "stats_failure", "pending", "daily_failure", "backfill"]
)
def test_late_stats_export_keeps_current_failures_and_excludes_history(batch, case):
    database, archive, release, client = batch
    path = archive / "feed_manifest.json"
    meta = json.loads(path.read_bytes())
    daily = {
        source: dict(status="complete", run_id="current-daily")
        for source in ("jc", "apif", "fdorg", "fd")
    }
    if case == "daily_failure":
        daily["apif"]["status"] = "partial"
    history = {
        f"{source}_backfill": dict(
            source=source, operation="backfill", status="partial", suspected_duplicates=523
        )
        for source in ("apif", "fdorg", "fd")
    }
    meta.update(collection={**daily, **history}, collection_status="complete")
    path.write_text(json.dumps(meta))
    if case == "pending":
        (database.parent / "rejected_results.csv").write_text(
            "date,match_id,observed_utc,reason\n"
            "2026-11-01,jc:unresolved,2026-11-04T00:00:00+00:00,offline rejected result\n"
        )
    with responses.RequestsMock() as http:
        if case == "stats_failure":
            http.get(
                "https://v3.football.api-sports.io/injuries?league=39&season=2026",
                json=dict(
                    get="injuries",
                    parameters={"league": "39", "season": "2026"},
                    errors={"parameter": "offline response failure"},
                    results=0,
                    response=[],
                ),
            )
            result = collect_batch(
                database,
                archive,
                plan("39", "2026", "injuries", NOW),
                release=release,
                run_id="late-stats-failure",
                now=NOW,
                daily=True,
                client_factory=client,
            )
            assert result["requests"] == len(http.calls) == 1
            assert result["status"] == "partial"
            cursor = json.loads((database.parent / "state/stats_backfill_cursor.json").read_bytes())
            assert {row["outcome"] for row in cursor["records"].values()} == {"error"}
        else:
            if case == "backfill":
                register(http, 102)
                register(http, 103)
            result = run_stats(batch, run_id="late-stats", daily=case != "backfill")
            assert result["status"] == "complete"
            assert len(http.calls) == (2 if case == "backfill" else 0)
    assert not result.get("archive_failed"), result
    after = archive_info(archive)
    assert all(after["collection"][source] == row for source, row in {**daily, **history}.items())
    assert after["collection"]["stats"]["status"] == result["status"]
    assert after["pending_result_count"] == int(case == "pending")
    # The non-daily statistics caller retains its existing default export semantics.
    assert after["collection_status"] == ("complete" if case == "complete" else "partial")
