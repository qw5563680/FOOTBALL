#!/usr/bin/env python3
"""Independent offline retained-result/technical-index check on a real JC crop.

API fixture 1556134 is hand-built from reviewer information, not a captured API
response. This checks behavior; it proves no provider permission or coverage.
"""

import argparse
import hashlib
import inspect
import json
import sys
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sqlalchemy import select
from zucai.db import get_engine, init_db, session_scope
from zucai.models import ApifFixtureIndex, Match, Result, Team
from zucai.sources.api_common import ApiGame
from zucai.sources.apif_ingest import import_apif_games
from zucai.stats_link import link_fixtures

ROOT = Path(__file__).parent
ARCHIVE = Path("/workspace/work/t65-fixed-archive")
SEED = Path("/workspace/work/t63-review-20261114/stats/jc-2042615-seed-with-synthetic-api.json")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def snapshot(session, model):
    return [dict(row) for row in session.execute(select(model.__table__)).mappings()]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("label", choices=("t66", "t63"))
    parser.add_argument("--code", type=Path, required=True)
    args = parser.parse_args()
    seed = json.loads(SEED.read_text())
    columns = json.loads((ARCHIVE / "feed_manifest.json").read_text())["columns"]
    for table, expected in seed["tables"].items():
        path = ARCHIVE / f"state/{table}.jsonl"
        assert sha(path) == seed["source_files"][f"state/{table}.jsonl"]
        originals = {
            r[0]: dict(zip(columns[table], r, strict=True))
            for r in map(json.loads, path.read_text().splitlines())
        }
        assert all(originals[r[columns[table][0]]] == r for r in expected)
    synthetic = seed["synthetic_api"]
    now = datetime.fromisoformat(synthetic["observed_utc"])
    document = synthetic["document"]
    aliases = json.loads((args.code / "data/team_aliases/api_football.json").read_text())
    kwargs = dict(
        league_id=98,
        season=2027,
        aliases=aliases,
        metadata=dict(
            captured_utc=now,
            raw_sha256=hashlib.sha256(json.dumps(document).encode()).hexdigest(),
            raw_location="fixture://synthetic-review-api-1556134",
        ),
    )
    calls = []

    def blocked(*args, **kwargs):
        calls.append(repr(args[:2]))
        raise AssertionError("HTTP and sockets blocked for offline stats acceptance")

    with ExitStack() as stack, TemporaryDirectory(dir=ROOT, prefix="stats-small-db-") as directory:
        for name in (
            "requests.sessions.Session.request",
            "socket.create_connection",
            "socket.socket.connect",
        ):
            stack.enter_context(patch(name, side_effect=blocked))
        engine = get_engine(Path(directory) / "probe.db")
        init_db(engine)
        with session_scope(engine) as session:
            session.add_all(
                Team(**dict(r, aliases=json.loads(r["aliases"]))) for r in seed["tables"]["teams"]
            )
            session.flush()
            session.add_all(Match(**r) for r in seed["tables"]["matches"])
            session.flush()
            session.add_all(Result(**r) for r in seed["tables"]["results"])
            session.flush()
            before = dict(
                matches=snapshot(session, Match),
                results=snapshot(session, Result),
                teams=snapshot(session, Team),
            )
            game = ApiGame(
                provider="apif",
                provider_match_id=1556134,
                competition_code="98",
                league="日职",
                kickoff_utc=datetime.fromisoformat("2026-11-08T06:00:00Z"),
                status="FT",
                home_raw="Gamba Osaka",
                away_raw="Cerezo Osaka",
                home_provider_id=293,
                away_provider_id=291,
                full=tuple(synthetic["full"]),
                half=tuple(synthetic["half"]),
                regular_time_basis="regular",
            )
            assert ("retain_results" in inspect.signature(import_apif_games).parameters) == (
                args.label == "t63"
            )
            options = dict(retain_results=True, imported_utc=now) if args.label == "t63" else {}
            imported = import_apif_games(
                session, [game], seen=now, assume=None, aliases=aliases, **options
            )
            session.flush()
            imported_snapshot = dict(
                matches=snapshot(session, Match),
                results=snapshot(session, Result),
                teams=snapshot(session, Team),
            )
            original_match = next(
                r for r in imported_snapshot["matches"] if r["match_id"] == "jc:2042615"
            )
            original_result = next(
                r for r in imported_snapshot["results"] if r["match_id"] == "jc:2042615"
            )
            assert original_match == before["matches"][0]
            assert original_result == before["results"][0]
            assert before["teams"] == imported_snapshot["teams"]
            if args.label == "t63":
                assert imported["inserted"] == 1
                assert session.get(Result, "apif:1556134") is not None
                assert len(imported_snapshot["matches"]) == len(imported_snapshot["results"]) == 2
            else:
                assert imported["inserted"] == 0
                assert session.get(Match, "apif:1556134") is None
                assert session.get(Result, "apif:1556134") is None
                assert imported_snapshot == before
            linked = link_fixtures(session, document, **kwargs)
            repeat = link_fixtures(session, document, **kwargs)
            assert linked["linked"] == 1 and linked["rejected"] == 0
            assert repeat["linked"] == 0 and repeat["unchanged"] == 1
            assert session.get(ApifFixtureIndex, "1556134").match_id == "jc:2042615"
            after = dict(
                matches=snapshot(session, Match),
                results=snapshot(session, Result),
                teams=snapshot(session, Team),
            )
            assert after == imported_snapshot
            modules = {}
            for name, module in sorted(sys.modules.items()):
                path = getattr(module, "__file__", None)
                if name.startswith("zucai") and path and Path(path).suffix == ".py":
                    modules[name] = dict(path=path, sha256=sha(path))
            report = dict(
                label=args.label,
                seed_sha256=sha(SEED),
                synthetic_api_disclaimer=synthetic["disclaimer"],
                synthetic_validation_time=now.isoformat(),
                seed_exactly_matches_original_archive=True,
                import_result=imported,
                index_result=linked,
                index_repeat=repeat,
                original_jc_match_result_teams_unchanged=True,
                indexing_all_matches_results_unchanged=True,
                retained_apif_row=(args.label == "t63"),
                technical_target="jc:2042615",
                before=before,
                after=after,
                index=snapshot(session, ApifFixtureIndex),
                imported_modules=modules,
                http_calls=calls,
            )
            (ROOT / f"stats-{args.label}.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n"
            )
            print(
                json.dumps(
                    {
                        k: report[k]
                        for k in (
                            "label",
                            "synthetic_api_disclaimer",
                            "original_jc_match_result_teams_unchanged",
                            "indexing_all_matches_results_unchanged",
                            "retained_apif_row",
                            "technical_target",
                            "http_calls",
                        )
                    },
                    ensure_ascii=False,
                )
            )
        engine.dispose()
    assert not calls


if __name__ == "__main__":
    main()
