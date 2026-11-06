"""T48 offline proofs preserve matches while attaching archived FD observations."""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from zucai.db import get_engine, init_db, session_scope
from zucai.models import Match, Team
from zucai.sources.apif_stats import instant
from zucai.sources.base import RawStore
from zucai.stats_storage import ingest_fd

FIX = Path(__file__).parents[1] / "data/fixtures/t31"
FD_ID = "fd:E0:20241001:home:away"
CAPTURE = "2026-11-04T12:00:00Z"


def formal(canonical):
    return dict(
        canonical=canonical,
        status="approved",
        approved_by="owner",
        approved_utc="2026-11-04T00:00:00Z",
        approval_pr="https://example.org/approval/1",
    )


@pytest.fixture
def setup(tmp_path):
    database = tmp_path / "feeds.db"
    engine = get_engine(database)
    init_db(engine)
    with session_scope(engine) as session:
        session.add_all(
            [
                Team(id=1, name="主队", aliases=[]),
                Team(id=2, name="客队", aliases=[]),
                Team(id=3, name="第三队", aliases=[]),
            ]
        )
        session.flush()
        session.add(
            Match(
                match_id="jc:1",
                source="jc",
                league="英超",
                home_team_id=1,
                away_team_id=2,
                kickoff_utc="2024-10-01T19:00:00Z",
                status="finished",
            )
        )
    engine.dispose()
    aliases = tmp_path / "aliases.json"
    aliases.write_text(json.dumps({"英超": {"Home": formal("主队"), "Away": formal("客队")}}))
    raw = save_raw(tmp_path, (FIX / "fd.csv").read_bytes())
    return database, raw, aliases


def save_raw(tmp_path, content, capture=CAPTURE):
    return RawStore(tmp_path / "raw").save(
        "football-data-co-uk",
        "E0-2425",
        content,
        "csv",
        captured_at=instant(capture),
        url="https://www.football-data.co.uk/mmz4281/2425/E0.csv",
        status_code=200,
    )


def ingest(setup, **kwargs):
    database, raw, aliases = setup
    return ingest_fd(
        database,
        raw,
        div="E0",
        season="2425",
        leagues_path=FIX / "fd_leagues.json",
        aliases_path=aliases,
        **kwargs,
    )


def sql(database, query, args=()):
    with sqlite3.connect(database) as db:
        return db.execute(query, args).fetchall()


def mutate(database, query, args=()):
    with sqlite3.connect(database) as db:
        db.execute(query, args)


def test_historical_absorbed_counterpart_is_accepted_without_new_evidence(setup):
    database, raw, _ = setup
    mutate(database, "UPDATE matches SET counterpart_id=?", (FD_ID,))
    # Exercise the pre-existing public signature so the regression fails on behavior.
    result = ingest_fd(database, raw, div="E0", season="2425", leagues_path=FIX / "fd_leagues.json")
    assert result == {"inserted": 16, "rejected": 0}
    assert sql(
        database, "SELECT DISTINCT match_id,available_utc,availability_basis FROM match_stats"
    ) == [("jc:1", None, "backfilled")]


@pytest.mark.parametrize("pointer", [None, "fdorg:old"])
def test_exact_alias_link_preserves_pointer_and_replay_is_zero(setup, pointer):
    database, _, _ = setup
    mutate(database, "UPDATE matches SET counterpart_id=?", (pointer,))
    before = sql(database, "SELECT * FROM matches")
    assert ingest(setup) == {"inserted": 16, "rejected": 0}
    assert sql(database, "SELECT * FROM matches") == before
    assert sql(database, "SELECT provider_fixture_id,match_id FROM fd_stats_links") == [
        (FD_ID, "jc:1")
    ]
    audit = database.parent / "state/audit/stats_ingest_audit.csv"
    old_audit = audit.read_bytes()
    assert ingest(setup) == {"inserted": 0, "rejected": 0}
    assert audit.read_bytes() == old_audit
    assert sql(database, "SELECT COUNT(*) FROM teams") == [(3,)]
    from zucai.fd_stats_links import validate_fd_links

    with sqlite3.connect(database) as db:
        validate_fd_links(db)


@pytest.mark.parametrize("kind", ["missing", "proposed", "invalid", "malformed", "unknown"])
def test_only_formal_existing_canonical_identity_authorizes(setup, kind):
    _, _, path = setup
    aliases = json.loads(path.read_text())
    entry = aliases["英超"]["Home"]
    if kind == "missing":
        del aliases["英超"]["Home"]
    elif kind == "unknown":
        entry["canonical"] = "不存在"
    elif kind == "malformed":
        entry.pop("approval_pr")
    else:
        entry["status"] = kind
    path.write_text(json.dumps(aliases))
    assert ingest(setup) == {"inserted": 0, "rejected": 1}
    assert sql(setup[0], "SELECT COUNT(*) FROM fd_stats_links") == [(0,)]


@pytest.mark.parametrize("kind", ["reverse", "shared", "ambiguous", "late", "league", "claimed"])
def test_reverse_shared_ambiguous_time_league_and_claims_reject(setup, kind):
    database, _, _ = setup
    if kind in {"reverse", "shared", "ambiguous"}:
        home, away = {"reverse": (2, 1), "shared": (1, 3), "ambiguous": (1, 2)}[kind]
        mutate(
            database,
            "INSERT INTO matches(match_id,source,league,home_team_id,away_team_id,"
            "kickoff_utc,status) VALUES('jc:2','jc','英超',?,?, "
            "'2024-10-01T19:00:00Z','finished')",
            (home, away),
        )
    elif kind == "late":
        mutate(database, "UPDATE matches SET kickoff_utc='2024-10-01T22:00:01Z'")
    elif kind == "league":
        mutate(database, "UPDATE matches SET league='英冠'")
    else:
        mutate(database, "UPDATE matches SET counterpart_id='fd:E0:other'")
    assert ingest(setup) == {"inserted": 0, "rejected": 1}


@pytest.mark.parametrize("alias_competitor", [False, True])
def test_raw_duplicate_or_competing_aliases_do_not_claim_first(setup, tmp_path, alias_competitor):
    database, raw, path = setup
    content = raw.read_bytes()
    row = content.splitlines()[1]
    if alias_competitor:
        row = row.replace(b"Home", b"Home2")
        aliases = json.loads(path.read_text())
        aliases["英超"]["Home2"] = formal("主队")
        path.write_text(json.dumps(aliases))
    changed = save_raw(tmp_path, content + row + b"\n")
    assert ingest((database, changed, path)) == {"inserted": 0, "rejected": 2}


def test_atomic_audit_failure_removes_rows_and_new_link(setup, monkeypatch):
    from zucai import stats_storage

    real = stats_storage.write_audit

    def fail(*args, **kwargs):
        real(*args, **kwargs)
        raise OSError("synthetic audit failure")

    monkeypatch.setattr(stats_storage, "write_audit", fail)
    with pytest.raises(OSError, match="synthetic"):
        ingest(setup)
    assert sql(setup[0], "SELECT COUNT(*) FROM fd_stats_links") == [(0,)]
    assert sql(setup[0], "SELECT COUNT(*) FROM match_stats") == [(0,)]
    assert not (setup[0].parent / "state/audit/stats_ingest_audit.csv").exists()


def test_conflicting_cross_raw_identity_cannot_reuse_link(setup, tmp_path):
    database, raw, path = setup
    assert ingest(setup)["inserted"] == 16
    aliases = json.loads(path.read_text())
    aliases["英超"]["Home"]["canonical"] = "第三队"
    path.write_text(json.dumps(aliases))
    changed = save_raw(tmp_path, raw.read_bytes().replace(b",10,0,", b",11,0,"))
    assert ingest((database, changed, path)) == {"inserted": 0, "rejected": 1}
    assert sql(database, "SELECT COUNT(*) FROM match_stats") == [(16,)]


def test_link_correction_retains_each_real_observation(setup, tmp_path):
    database, raw, path = setup
    assert ingest(setup)["inserted"] == 16
    changed = save_raw(
        tmp_path, raw.read_bytes().replace(b",10,0,", b",11,0,"), "2026-11-05T12:00:00Z"
    )
    assert ingest((database, changed, path))["inserted"] == 1
    assert sql(database, "SELECT captured_utc,available_utc FROM match_stats WHERE version=2") == [
        ("2026-11-05T12:00:00.000000Z", None)
    ]
    evidence = json.loads(sql(database, "SELECT evidence FROM fd_stats_links")[0][0])
    assert {o["raw_sha256"] for o in evidence["observations"]} == {
        hashlib.sha256(raw.read_bytes()).hexdigest(),
        hashlib.sha256(changed.read_bytes()).hexdigest(),
    }


@pytest.mark.parametrize(
    "tamper", ["team", "kickoff", "hash", "alias", "pointer", "missing_observation"]
)
def test_independent_sqlite_validator_rejects_tampered_proof(setup, tamper):
    database = setup[0]
    assert ingest(setup)["inserted"] == 16
    if tamper == "team":
        mutate(database, "UPDATE fd_stats_links SET home_team_id=3")
    elif tamper == "kickoff":
        mutate(database, "UPDATE fd_stats_links SET kickoff_utc='2024-10-02T19:00:00Z'")
    elif tamper == "hash":
        mutate(database, "UPDATE fd_stats_links SET aliases_sha256=?", ("0" * 64,))
    elif tamper == "pointer":
        mutate(database, "UPDATE matches SET counterpart_id='fd:conflicting'")
    else:
        evidence = json.loads(sql(database, "SELECT evidence FROM fd_stats_links")[0][0])
        if tamper == "alias":
            evidence["aliases"]["英超"]["Home"]["status"] = "proposed"
        else:
            evidence["observations"] = []
        mutate(database, "UPDATE fd_stats_links SET evidence=?", (json.dumps(evidence),))
    from zucai.fd_stats_links import validate_fd_links

    with sqlite3.connect(database) as db, pytest.raises(ValueError):
        validate_fd_links(db)


@pytest.mark.parametrize("other_pair", [("Away", "Home"), ("Home", "Third")])
def test_incoming_reverse_and_shared_team_reject_both_rows(setup, tmp_path, other_pair):
    database, raw, path = setup
    aliases = json.loads(path.read_text())
    aliases["英超"]["Third"] = formal("第三队")
    path.write_text(json.dumps(aliases))
    row = raw.read_bytes().splitlines()[1].replace(b"Home,Away", ",".join(other_pair).encode())
    changed = save_raw(tmp_path, raw.read_bytes() + row + b"\n")
    assert ingest((database, changed, path)) == {"inserted": 0, "rejected": 2}


def test_twenty_six_independent_alias_attachments_are_idempotent(setup, tmp_path):
    database, raw, path = setup
    engine = get_engine(database)
    aliases = {"英超": {}}
    header = raw.read_bytes().splitlines()[0].decode()
    rows = []
    with session_scope(engine) as session:
        session.query(Match).delete()
        for n in range(26):
            home, away = 10 + 2 * n, 11 + 2 * n
            session.add_all(
                [Team(id=home, name=f"主{n}", aliases=[]), Team(id=away, name=f"客{n}", aliases=[])]
            )
            session.flush()
            session.add(
                Match(
                    match_id=f"jc:{n}",
                    source="jc",
                    league="英超",
                    home_team_id=home,
                    away_team_id=away,
                    kickoff_utc="2024-10-01T19:00:00Z",
                    counterpart_id=f"fdorg:{n}" if n % 2 else None,
                    status="finished",
                )
            )
            aliases["英超"][f"Home{n}"] = formal(f"主{n}")
            aliases["英超"][f"Away{n}"] = formal(f"客{n}")
            rows.append(f"E0,01/10/2024,20:00,Home{n},Away{n},10,0,4,,6,2,11,9,2,3,1,0,0,0,13,12")
    engine.dispose()
    path.write_text(json.dumps(aliases))
    changed = save_raw(tmp_path, (header + "\n" + "\n".join(rows) + "\n").encode())
    before = sql(database, "SELECT * FROM matches ORDER BY match_id")
    assert ingest((database, changed, path)) == {"inserted": 26 * 16, "rejected": 0}
    assert ingest((database, changed, path)) == {"inserted": 0, "rejected": 0}
    assert sql(database, "SELECT COUNT(*) FROM fd_stats_links") == [(26,)]
    assert sql(database, "SELECT * FROM matches ORDER BY match_id") == before


def test_writer_rejects_unobserved_raw_even_when_link_exists(setup):
    from sqlalchemy import select

    from zucai.models import MatchStat
    from zucai.stats_storage import store_rows

    database = setup[0]
    assert ingest(setup)["inserted"] == 16
    engine = get_engine(database)
    with session_scope(engine) as session:
        stored = session.scalar(select(MatchStat))
        row = {
            column.name: getattr(stored, column.name)
            for column in MatchStat.__table__.columns
            if column.name
            not in {
                "id",
                "captured_utc",
                "available_utc",
                "availability_basis",
                "raw_sha256",
                "raw_location",
                "version",
            }
        }
        with pytest.raises(ValueError, match="观察"):
            store_rows(
                session,
                [row],
                {
                    "captured_utc": CAPTURE,
                    "raw_sha256": "f" * 64,
                    "raw_location": stored.raw_location,
                },
            )
    engine.dispose()


def test_division_and_league_must_correspond_even_with_self_consistent_id(setup):
    database = setup[0]
    assert ingest(setup)["inserted"] == 16
    mutate(
        database,
        "UPDATE fd_stats_links SET div_code='D1', provider_fixture_id=?",
        (FD_ID.replace("fd:E0:", "fd:D1:"),),
    )
    from zucai.fd_stats_links import validate_fd_links

    with sqlite3.connect(database) as db, pytest.raises(ValueError, match="division"):
        validate_fd_links(db)


def test_raw_reverse_without_formal_alias_still_blocks_known_counterpart(setup, tmp_path):
    database, raw, path = setup
    mutate(database, "UPDATE matches SET counterpart_id=?", (FD_ID,))
    path.write_text("{}")
    reversed_row = raw.read_bytes().splitlines()[1].replace(b"Home,Away", b"Away,Home")
    changed = save_raw(tmp_path, raw.read_bytes() + reversed_row + b"\n")
    assert ingest((database, changed, path)) == {"inserted": 0, "rejected": 2}


@pytest.mark.parametrize("malformed", [None, "witnesses", "evidence"])
def test_auto_verified_alias_requires_structured_offline_evidence(setup, malformed):
    _, _, path = setup
    aliases = json.loads(path.read_text())
    aliases["英超"]["Home"] = dict(
        canonical="主队",
        status="auto_verified",
        inference=dict(
            unique_fd_candidates=1,
            unique_jc_candidates=1,
            witnesses=[dict(fd_row=2, jc_match_id="jc:1", difference_minutes=0)],
        ),
        evidence=[dict(fd_row=2, jc_match_id="jc:1", raw_sha256="a" * 64, div_code="E0")],
    )
    if malformed == "witnesses":
        aliases["英超"]["Home"]["inference"]["witnesses"] = "unverified"
    elif malformed == "evidence":
        aliases["英超"]["Home"]["evidence"] = "unverified"
    path.write_text(json.dumps(aliases))
    assert ingest(setup) == (
        {"inserted": 16, "rejected": 0} if malformed is None else {"inserted": 0, "rejected": 1}
    )
