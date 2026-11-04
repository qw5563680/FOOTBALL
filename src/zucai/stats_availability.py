"""Counts of stored observation versions, separate from request/fixture coverage."""

import sqlite3
from collections import Counter, defaultdict

from zucai.stats_archive import ROW_TABLES


def annotate_coverage(coverage, database):
    groups, totals = defaultdict(Counter), Counter()
    with sqlite3.connect(database) as db:
        for table in ROW_TABLES:
            rows = db.execute(
                f"SELECT m.league, r.provider, r.endpoint, r.availability_basis, "
                "COALESCE(CAST(i.season AS TEXT), "
                "printf('%02d%02d', (CAST(substr(m.kickoff_utc,1,4) AS INTEGER) - "
                "(substr(m.kickoff_utc,6,2) < '07')) % 100, "
                "(CAST(substr(m.kickoff_utc,1,4) AS INTEGER) - "
                "(substr(m.kickoff_utc,6,2) < '07') + 1) % 100)), count(*) "
                f"FROM {table} r JOIN matches m ON m.match_id=r.match_id "
                "LEFT JOIN apif_fixture_index i ON r.provider='apif' "
                "AND i.provider_fixture_id=r.provider_fixture_id "
                "GROUP BY 1,2,3,4,5"
            )
            for league, provider, endpoint, basis, season, count in rows:
                groups[league, provider, season, endpoint][basis] += count
                totals[basis] += count
    coverage["availability_basis_counts"] = dict(sorted(totals.items()))
    coverage["availability_rows"] = [
        dict(league=k[0], provider=k[1], season=k[2], endpoint=k[3], counts=dict(sorted(v.items())))
        for k, v in sorted(groups.items())
    ]
    for row in coverage["rows"]:
        provider = "fd" if row["endpoint"] == "fd" else "apif"
        endpoint = "csv" if provider == "fd" else row["endpoint"]
        season = str(row["season"])
        if provider == "fd":
            year = int(row["season"])
            season = f"{year % 100:02d}{(year + 1) % 100:02d}"
        counts = groups[row["league"], provider, season, endpoint]
        row["availability_basis_counts"] = dict(sorted(counts.items()))
        row["captured_rows"] = counts["captured"]
    return coverage
