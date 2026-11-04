"""Counts of stored observation versions, separate from request/fixture coverage."""

import sqlite3
from collections import Counter, defaultdict

from zucai.stats_archive import LEGACY_ROW_TABLES


def annotate_coverage(coverage, database):
    groups, totals = defaultdict(Counter), Counter()
    with sqlite3.connect(database) as db:
        for table in LEGACY_ROW_TABLES:
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
        injury_groups = []
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='match_injuries'"
        ).fetchone():
            for league, season, captured, backfilled, linked, count in db.execute("""
                SELECT league, provider_season,
                    sum(availability_basis='captured'), sum(availability_basis='backfilled'),
                    sum(match_id IS NOT NULL AND team_id IS NOT NULL), count(*)
                FROM match_injuries GROUP BY league, provider_season ORDER BY 1,2
            """):
                counts = Counter(captured=captured, backfilled=backfilled)
                groups[league, "apif", str(season), "injuries"] += counts
                totals.update(counts)
                injury_groups.append(
                    dict(
                        league=league,
                        season=season,
                        captured_rows=captured,
                        backfilled_rows=backfilled,
                        linked_rows=linked,
                        total_rows=count,
                        linkage_rate=linked / count if count else None,
                    )
                )
        count = sum(r["total_rows"] for r in injury_groups)
        linked = sum(r["linked_rows"] for r in injury_groups)
        coverage["injuries"] = dict(
            total_rows=count,
            linked_rows=linked,
            linkage_rate=linked / count if count else None,
            captured_rows=sum(r["captured_rows"] for r in injury_groups),
            backfilled_rows=sum(r["backfilled_rows"] for r in injury_groups),
            rows=injury_groups,
            empty_interface_leagues=["日职", "韩职", "中超", "葡超", "欧冠", "欧罗巴", "欧协联"],
            empty_interface_basis="DATA_SOURCES T30/T38 已归档实测；非本次新增探测",
        )
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
