"""Prove identities from archived complete seasons; all fixture data here is synthetic."""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zucai.sources.base import RawStore

FIXTURE = Path(__file__).resolve().parents[1] / "data/fixtures/t09/synthetic_identity.json"
EXAMPLE = json.loads(FIXTURE.read_bytes())
SEASON_URL = "https://api.football-data.org/v4/competitions/PL/matches?season=2026"
WINDOW_URL = "https://api.football-data.org/v4/matches?dateFrom=2026-08-01&competitions=PL"
SEEN = datetime(2026, 11, 2, tzinfo=UTC)


def synthetic_season():
    """Generate both legs for twenty clubs without checking in a full provider-style dataset."""
    overrides = {(h, a): t for h, a, t in EXAMPLE["overrides"]}
    clubs = list(range(101, 121))
    matches = []
    first = datetime(2026, 9, 1, 18, tzinfo=UTC)
    for week in range(len(clubs) - 1):
        for i in range(len(clubs) // 2):
            pair = (clubs[i], clubs[-1 - i])
            for leg, (home, away) in enumerate([pair, pair[::-1]]):
                day = week + leg * (len(clubs) - 1)
                stamp = first + timedelta(weeks=day)
                matches.append(
                    dict(
                        id=home * 1000 + away,
                        competition=EXAMPLE["competition"],
                        season=EXAMPLE["season"],
                        stage="REGULAR_SEASON",
                        matchday=day + 1,
                        utcDate=overrides.get((home, away), stamp.isoformat()),
                        homeTeam={"id": home, "name": f"Club {home}"},
                        awayTeam={"id": away, "name": f"Club {away}"},
                        status="TIMED",
                        score={
                            "duration": "REGULAR",
                            "fullTime": {"home": None, "away": None},
                            "halfTime": {"home": None, "away": None},
                        },
                    )
                )
        clubs = [clubs[0], clubs[-1], *clubs[1:-1]]
    return dict(
        filters={"season": "2026"},
        competition=EXAMPLE["competition"],
        resultSet={"count": len(matches)},
        matches=matches,
    )


def save_archive(tmp_path, payload, *, url=SEASON_URL, seen=SEEN, status=200):
    return RawStore(tmp_path / "raw").save(
        "football-data-org",
        "test",
        json.dumps(payload).encode(),
        "json",
        captured_at=seen,
        url=url,
        status_code=status,
    )


def setup_inputs(tmp_path, records=None):
    db = tmp_path / "baseline.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE teams(id INTEGER PRIMARY KEY, name TEXT UNIQUE);
            CREATE TABLE matches(match_id TEXT, league TEXT, kickoff_utc TEXT,
                                 home_team_id INTEGER, away_team_id INTEGER, source TEXT);
            CREATE TABLE results(match_id TEXT, home_goals INTEGER, away_goals INTEGER);
        """)
        for mid, home, away, when in EXAMPLE["official"]:
            for team in (home, away):
                conn.execute("INSERT OR IGNORE INTO teams(name) VALUES(?)", (team,))
            conn.execute(
                "INSERT INTO matches SELECT ?, '英超', ?, h.id, a.id, 'jc' "
                "FROM teams h, teams a WHERE h.name=? AND a.name=?",
                (mid, when, home, away),
            )
        # Neither another source nor another competition may seed the graph.
        for source, league in [("fd", "英超"), ("jc", "英冠")]:
            conn.execute(
                "INSERT INTO matches SELECT ?, ?, kickoff_utc,home_team_id,away_team_id,? "
                "FROM matches WHERE match_id='jc:anchor'",
                (f"{source}:unrelated", league, source),
            )
    aliases = tmp_path / "aliases-in.json"
    aliases.write_text(json.dumps({"英超": records or {}}), encoding="utf8")
    return db, aliases


def verify(tmp_path, db, aliases, *, destination="audit"):
    from zucai.sources.fdorg_verify import main

    output = tmp_path / destination
    main(
        [
            "--db",
            str(db),
            "--aliases",
            str(aliases),
            "--raw-dir",
            str(tmp_path / "raw"),
            "--season",
            "2026",
            "--output",
            str(output),
        ]
    )
    return {
        name: json.loads((output / f"{name}.json").read_bytes())
        for name in ("coverage", "aliases", "unlinked")
    }


def test_cup_archive_does_not_crash_domestic_round_robin_verifier(tmp_path):
    from zucai.sources.fdorg_verify import COMPETITIONS_PATH, verify_inputs

    db, aliases = setup_inputs(tmp_path)
    fixture = FIXTURE.parents[1] / "t20/fdorg_cl_2024.json"
    save_archive(
        tmp_path,
        json.loads(fixture.read_bytes()),
        url="https://api.football-data.org/v4/competitions/CL/matches?season=2024",
    )
    result = verify_inputs(
        db, tmp_path / "raw", aliases, COMPETITIONS_PATH, tmp_path / "audit", 2024
    )
    assert result["coverage"]["CL"]["status"] == "incomplete"
    assert "cup" in result["coverage"]["CL"]["reason"]
    assert not result["aliases"]["欧冠"]


def test_full_schedule_keeps_ambiguous_candidates_and_records_oriented_utc_witness(tmp_path):
    proposed = {"name": "Club 108", "canonical": "己", "status": "proposed"}
    approved = {"name": "Legacy", "canonical": "未上场", "status": "approved"}
    db, aliases = setup_inputs(tmp_path, {"108": proposed, "999": approved})
    raw = save_archive(tmp_path, synthetic_season())
    sidecar = raw.with_suffix(".json.meta.json")
    before = {p: p.read_bytes() for p in (raw, sidecar, db, aliases)}
    audit = verify(tmp_path, db, aliases)
    assert all(p.read_bytes() == content for p, content in before.items())
    summary = audit["coverage"]["PL"]
    assert summary["status"] == "complete"
    assert (summary["fdorg_rows"], summary["comparable_jc"], summary["matched_jc"]) == (380, 6, 2)
    assert summary["rate"] == pytest.approx(2 / 6)
    assert summary["excluded_outside_fdorg_coverage"] == 1
    names = audit["aliases"]["英超"]
    assert set(names) == {"101", "102", "103", "108", "999"}
    assert names["999"] == approved and names["108"] == proposed
    assert (names["101"]["canonical"], names["102"]["canonical"], names["103"]["canonical"]) == (
        "甲",
        "乙",
        "丙",
    )
    witness = names["101"]["evidence"][0]
    assert witness["fdorg_id"] == "fdorg:101102" and witness["jc_id"] == "jc:anchor"
    assert witness["difference_minutes"] == -30
    assert witness["raw_sha256"] == hashlib.sha256(raw.read_bytes()).hexdigest()
    assert (tmp_path / "raw" / witness["sidecar"]).resolve() == sidecar
    gaps = {g["jc_id"]: g for g in audit["unlinked"]["PL"]}
    links = json.loads((tmp_path / "audit" / "links.json").read_bytes())["PL"]
    assert {(r["fdorg_id"], r["jc_id"]) for r in links} == {
        ("fdorg:101102", "jc:anchor"),
        ("fdorg:101103", "jc:anchored-next"),
    }
    assert set(gaps) == {
        "jc:forward-ambiguous",
        "jc:reverse-one",
        "jc:reverse-two",
        "jc:no-fixture",
    }
    assert {c["difference_minutes"] for c in gaps["jc:forward-ambiguous"]["candidates"]} == {0, 180}
    for mid in ("jc:reverse-one", "jc:reverse-two"):
        options = gaps[mid]["candidates"]
        assert len(options) == 1
        assert set(options[0]["reverse_jc_ids"]) == {"jc:reverse-one", "jc:reverse-two"}
    assert gaps["jc:no-fixture"]["candidates"] == []
    with pytest.raises(FileExistsError):
        verify(tmp_path, db, aliases)


@pytest.mark.parametrize(
    "kind", ["window", "finished", "short", "missing_pair", "wrong_host", "403"]
)
def test_incomplete_input_never_bootstraps_alias_or_reports_zero_denominator_success(
    tmp_path, kind
):
    db, aliases = setup_inputs(tmp_path)
    payload = synthetic_season()
    url = {
        "window": WINDOW_URL,
        "finished": SEASON_URL + "&status=FINISHED",
        "wrong_host": SEASON_URL.replace(".org", ".invalid"),
    }.get(kind, SEASON_URL)
    if kind == "short":
        payload["matches"] = [payload["matches"][0]]
    if kind == "missing_pair":
        payload["matches"].pop()
    payload["resultSet"]["count"] = len(payload["matches"])
    save_archive(tmp_path, payload, url=url, status=403 if kind == "403" else 200)
    audit = verify(tmp_path, db, aliases)
    summary = audit["coverage"]["PL"]
    assert summary["status"] == "incomplete" and summary["rate"] is None
    assert summary["comparable_jc"] is None and summary["matched_jc"] is None
    assert audit["aliases"]["英超"] == {}


@pytest.mark.parametrize("field", ["sha256", "fetched_at_utc"])
def test_success_requires_intact_bytes_and_explicit_utc_sidecar(tmp_path, field):
    db, aliases = setup_inputs(tmp_path)
    raw = save_archive(tmp_path, synthetic_season())
    meta = raw.with_suffix(".json.meta.json")
    values = json.loads(meta.read_bytes())
    values[field] = "2026-11-02T03:00:00" if field == "fetched_at_utc" else "0" * 64
    meta.write_text(json.dumps(values), encoding="utf8")
    with pytest.raises(ValueError, match="SHA256|UTC"):
        verify(tmp_path, db, aliases)
    assert not (tmp_path / "audit" / "aliases.json").exists()


@pytest.mark.parametrize("team_id,canonical", [("101", "冲突"), ("998", "甲")])
def test_prior_active_identity_collision_refuses_new_alias_file(tmp_path, team_id, canonical):
    db, aliases = setup_inputs(
        tmp_path, {team_id: {"name": "Prior", "status": "approved", "canonical": canonical}}
    )
    save_archive(tmp_path, synthetic_season())
    with pytest.raises(ValueError, match="conflict"):
        verify(tmp_path, db, aliases)
    assert not (tmp_path / "audit" / "aliases.json").exists()


@pytest.mark.parametrize("status", ["auto_verified", "approved"])
def test_active_anchor_can_align_an_opponent_and_its_record_is_preserved(tmp_path, status):
    existing = {"name": "Reviewed legacy spelling", "status": status, "canonical": "己"}
    db, aliases = setup_inputs(tmp_path, {"108": existing})
    save_archive(tmp_path, synthetic_season())
    audit = verify(tmp_path, db, aliases)
    assert audit["coverage"]["PL"]["matched_jc"] == 3
    assert audit["aliases"]["英超"]["108"] == existing
    counterpart = audit["aliases"]["英超"]["109"]
    assert counterpart["canonical"] == "庚"
    assert counterpart["evidence"][0]["difference_minutes"] == 180


def test_external_anchor_must_not_displace_an_independently_resolved_pair(tmp_path):
    db, aliases = setup_inputs(
        tmp_path, {"112": {"name": "Known", "status": "approved", "canonical": "辰"}}
    )
    payload = synthetic_season()
    extra_time = "2026-08-25T18:00:00Z"
    for row in payload["matches"]:
        if row["id"] in {103112, 113114, 115116}:
            row["utcDate"] = extra_time
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO teams(name) VALUES('辰')")
        conn.execute(
            "INSERT INTO matches SELECT 'jc:seeded', '英超', ?, h.id, a.id, 'jc' "
            "FROM teams h, teams a WHERE h.name='寅' AND a.name='辰'",
            (extra_time,),
        )
    save_archive(tmp_path, payload)
    # The independent chain yields 103→丙. The approved away anchor 112→辰 would yield
    # 103→寅 at the same iteration as the first independent witness.
    with pytest.raises(ValueError, match="conflict"):
        verify(tmp_path, db, aliases)
    assert not (tmp_path / "audit" / "aliases.json").exists()


def test_newest_full_season_observation_is_not_merged_with_an_obsolete_fixture(tmp_path):
    db, aliases = setup_inputs(tmp_path)
    original = synthetic_season()
    save_archive(tmp_path, original, seen=SEEN - timedelta(days=1))
    anchor = next(row for row in original["matches"] if row["id"] == 101102)
    anchor["utcDate"] = "2026-08-02T18:00:00Z"
    newest = save_archive(tmp_path, original)
    audit = verify(tmp_path, db, aliases)
    assert audit["aliases"]["英超"] == {}
    assert audit["coverage"]["PL"]["matched_jc"] == 0
    manifest = json.loads((tmp_path / "audit" / "archives.json").read_bytes())
    assert manifest["PL"]["raw_sha256"] == hashlib.sha256(newest.read_bytes()).hexdigest()


def test_later_propagation_cannot_activate_an_id_in_conflicting_independent_fixtures(tmp_path):
    db, aliases = setup_inputs(tmp_path)
    payload = synthetic_season()
    for day, away_id, home_cn, away_cn in [(25, 112, "寅", "庚"), (26, 113, "卯", "壬")]:
        stamp = f"2026-08-{day}T18:00:00Z"
        row = next(m for m in payload["matches"] if m["id"] == 103000 + away_id)
        row["utcDate"] = stamp
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO matches SELECT ?, '英超', ?, h.id, a.id, 'jc' FROM teams h, teams a "
                "WHERE h.name=? AND a.name=?",
                (f"jc:conflict{day}", stamp, home_cn, away_cn),
            )
    save_archive(tmp_path, payload)
    # Two initially unique fixtures disagree on ID 103. A later propagated 103→丙 cannot
    # restore that ID's consistency by silently discarding both contradicting witnesses.
    with pytest.raises(ValueError, match="conflict"):
        verify(tmp_path, db, aliases)
    assert not (tmp_path / "audit" / "aliases.json").exists()


def test_cross_midnight_first_game_inside_window_counts_in_both_numerator_and_denominator(tmp_path):
    db, aliases = setup_inputs(tmp_path)
    original = synthetic_season()
    anchor = next(row for row in original["matches"] if row["id"] == 101102)
    anchor["utcDate"] = "2026-08-01T01:00:00Z"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE matches SET kickoff_utc='2026-07-31T22:15:00Z' WHERE match_id='jc:anchor'"
        )
    save_archive(tmp_path, original)
    summary = verify(tmp_path, db, aliases)["coverage"]["PL"]
    assert (summary["matched_jc"], summary["comparable_jc"]) == (2, 6)


@pytest.mark.parametrize("bad_data", ["status_filter", "malformed_body", "wrong_season"])
def test_valid_hash_for_malformed_or_wrong_scope_data_does_not_certify_a_league(tmp_path, bad_data):
    db, aliases = setup_inputs(tmp_path)
    payload = synthetic_season()
    if bad_data == "status_filter":
        payload["filters"]["status"] = "TIMED"
    elif bad_data == "malformed_body":
        payload = {"errorCode": 403}
    else:
        payload["filters"]["season"] = "2025"
    save_archive(tmp_path, payload)
    audit = verify(tmp_path, db, aliases)
    assert audit["coverage"]["PL"]["status"] == "incomplete"
    assert audit["aliases"]["英超"] == {}


def test_awarded_competitor_still_prevents_an_ambiguous_jc_link(tmp_path):
    db, aliases = setup_inputs(tmp_path)
    payload = synthetic_season()
    awarded = next(m for m in payload["matches"] if m["id"] == 108109)
    awarded.update(
        status="AWARDED", score={"duration": "REGULAR", "fullTime": {"home": 3, "away": 0}}
    )
    save_archive(tmp_path, payload)
    audit = verify(tmp_path, db, aliases)
    summary = audit["coverage"]["PL"]
    assert (
        summary["status"],
        summary["fdorg_rows"],
        summary["comparable_jc"],
        summary["matched_jc"],
    ) == (
        "complete",
        380,
        6,
        2,
    )
    assert len(summary["identity_only_rows"]) == 1
    row = summary["identity_only_rows"][0]
    assert (row["fdorg_id"], row["provider_status"]) == ("fdorg:108109", "AWARDED")
    assert "AWARDED" in row["import_rejection"]
    gap = next(g for g in audit["unlinked"]["PL"] if g["jc_id"] == "jc:forward-ambiguous")
    assert {c["fdorg_id"] for c in gap["candidates"]} == {"fdorg:106107", "fdorg:108109"}
    assert (
        "AWARDED"
        in next(c for c in gap["candidates"] if c["fdorg_id"] == "fdorg:108109")["import_rejection"]
    )
    assert set(audit["aliases"]["英超"]) == {"101", "102", "103"}


def test_extra_time_without_90_minute_score_can_prove_fixture_identity_but_remains_rejected(
    tmp_path,
):
    from zucai.sources.football_data_org import FootballDataOrg

    db, aliases = setup_inputs(tmp_path)
    payload = synthetic_season()
    anchor = next(m for m in payload["matches"] if m["id"] == 101102)
    anchor.update(
        status="FINISHED", score={"duration": "EXTRA_TIME", "fullTime": {"home": 3, "away": 2}}
    )
    raw = save_archive(tmp_path, payload)
    source = FootballDataOrg()
    try:
        importable = source.parse(raw.read_bytes(), competition="PL", season=2026)
        assert all(g.provider_match_id != 101102 for g in importable)
        assert "regularTime" in source.rejected[0]["reason"]
    finally:
        source.session.close()
    audit = verify(tmp_path, db, aliases)
    summary = audit["coverage"]["PL"]
    assert (summary["status"], summary["comparable_jc"], summary["matched_jc"]) == (
        "complete",
        6,
        2,
    )
    assert set(audit["aliases"]["英超"]) == {"101", "102", "103"}
    link = audit["aliases"]["英超"]["101"]["evidence"][0]
    assert (link["jc_id"], link["fdorg_id"]) == ("jc:anchor", "fdorg:101102")
    assert "regularTime" in link["import_rejection"]
    assert summary["identity_only_rows"][0]["import_rejection"] == link["import_rejection"]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM results").fetchone() == (0,)


def test_missing_display_label_reuses_observed_name_for_the_same_numeric_id(tmp_path):
    db, aliases = setup_inputs(tmp_path)
    payload = synthetic_season()
    anchor = next(m for m in payload["matches"] if m["id"] == 101102)
    del anchor["homeTeam"]["name"]
    save_archive(tmp_path, payload)
    audit = verify(tmp_path, db, aliases)
    home = audit["aliases"]["英超"]["101"]
    assert (home["name"], home["canonical"]) == ("Club 101", "甲")
    assert home["evidence"][0]["import_rejection"]
    assert audit["coverage"]["PL"]["matched_jc"] == 2


@pytest.mark.parametrize(
    "damage",
    ["team_id", "duplicate_match", "same_sides", "naive_kickoff", "wrong_season", "wrong_code"],
)
def test_a_score_rejection_cannot_hide_a_damaged_fixture_identity(tmp_path, damage):
    db, aliases = setup_inputs(tmp_path)
    payload = synthetic_season()
    row = payload["matches"][0]
    row["status"] = "AWARDED"
    if damage == "team_id":
        row["homeTeam"]["id"] = None
    elif damage == "duplicate_match":
        row["id"] = payload["matches"][1]["id"]
    elif damage == "same_sides":
        row["awayTeam"]["id"] = row["homeTeam"]["id"]
    elif damage == "naive_kickoff":
        row["utcDate"] = "2026-08-01T18:00:00"
    elif damage == "wrong_season":
        row["season"] = {"startDate": "2025-08-01"}
    else:
        row["competition"] = {"code": "ELC"}
    save_archive(tmp_path, payload)
    audit = verify(tmp_path, db, aliases)
    summary = audit["coverage"]["PL"]
    assert summary["status"] == "incomplete" and summary["comparable_jc"] is None
    assert audit["aliases"]["英超"] == {}
