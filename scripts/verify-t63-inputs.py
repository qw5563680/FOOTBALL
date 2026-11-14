"""Read-only source inventory and selected-row differences; no fitting or scoring."""

import argparse
import hashlib
import json
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from zucai.db import get_engine
from zucai.predict.backtest import read_history
from zucai.predict.pooled import select_training_history
from zucai.predict.training_sources import actual_result_leagues, apif_history_sources


def inspect_inputs(database, cutoff):
    database = Path(database).resolve()
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    result = {
        "database_sha256": before,
        "cutoff_utc": cutoff,
        "fitted_models": 0,
        "probability_comparisons": 0,
        "network_requests": 0,
        "comparison": "current selector old-vs-expanded sources; not an independent main baseline",
        "leagues": {},
    }
    engine = get_engine(database)
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA query_only=ON")
            conn.exec_driver_sql("BEGIN")
            result["inventory"] = [
                dict(row._mapping)
                for row in conn.execute(
                    text(
                        "SELECT m.league,m.source,count(*) AS matches,count(r.match_id) AS results "
                        "FROM matches m LEFT JOIN results r USING(match_id) "
                        "GROUP BY m.league,m.source "
                        "ORDER BY m.league,m.source"
                    )
                )
            ]
            with Session(bind=conn) as session:
                for league in sorted(actual_result_leagues()):
                    entry = {}
                    try:
                        old = select_training_history(
                            read_history(session, league, "fd,jc"), cutoff
                        )
                        expanded = read_history(
                            session, league, ",".join(sorted(apif_history_sources()[league]))
                        )
                        new = select_training_history(expanded, cutoff)
                        columns = [
                            "match_id",
                            "source",
                            "kickoff_utc",
                            "available_utc",
                            "available_source",
                            "home",
                            "away",
                            "home_goals",
                            "away_goals",
                        ]

                        def records(frame, columns=columns):
                            return {
                                row["match_id"]: row for row in frame[columns].to_dict("records")
                            }

                        previous, current = records(old), records(new)
                        entry.update(
                            status="compared",
                            old_rows=len(old),
                            new_rows=len(new),
                            old_source_counts=old.source.value_counts().to_dict(),
                            new_source_counts=new.source.value_counts().to_dict(),
                            missing_historical_identity=sorted(
                                expanded.loc[
                                    expanded.source.isin(("apif", "fdorg"))
                                    & expanded.identity_verified_utc.isna(),
                                    "match_id",
                                ]
                            ),
                            unverified_numeric_identity=sorted(
                                expanded.loc[
                                    expanded.source.isin(("apif", "fdorg"))
                                    & ~expanded.provider_identity_verified,
                                    "match_id",
                                ]
                            ),
                            removed=[
                                previous[key] for key in sorted(previous.keys() - current.keys())
                            ],
                            added=[
                                current[key] for key in sorted(current.keys() - previous.keys())
                            ],
                            changed=[
                                {"before": previous[key], "after": current[key]}
                                for key in sorted(previous.keys() & current.keys())
                                if previous[key] != current[key]
                            ],
                        )
                    except ValueError as exc:
                        entry.update(status="blocked", reason=str(exc))
                    result["leagues"][league] = entry
    finally:
        engine.dispose()
    after = hashlib.sha256(database.read_bytes()).hexdigest()
    if before != after:
        raise ValueError("Read-only source inventory changed the database")
    result["database_unchanged"] = True
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--cutoff", default="2026-11-14T00:00:00Z")
    args = parser.parse_args()
    if args.out.exists() or args.out.resolve() == args.db.resolve():
        parser.error("Output must be a new, independent file")
    report = inspect_inputs(args.db, args.cutoff)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
