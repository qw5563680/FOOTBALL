"""Offline fixture identity verification: ambiguity must survive until real anchors resolve it."""

import pytest

from zucai.sources.fd_verify import infer_identity, load_rows, verify_league


def fd(row, home, away, day="04/09/2026", clock="19:00", score=(2, 1)):
    return dict(
        row=row, date=day, time=clock, home=home, away=away, hg=score[0], ag=score[1], season="2627"
    )


def official(mid, home, away, day="2026-09-04", clock="18:00", score=(2, 1)):
    return dict(
        match_id=mid,
        home=home,
        away=away,
        league="L",
        kickoff_utc=f"{day}T{clock}:00Z",
        hg=score[0],
        ag=score[1],
    )


def test_mutually_unique_oriented_pair_records_provenance():
    result = infer_identity(
        [fd(2, "raw1", "raw2")], [official("jc:1", "甲", "乙")], "Europe/London"
    )
    assert result["aliases"] == {"raw1": "甲", "raw2": "乙"}
    assert result["comparison_rows"][0]["difference_minutes"] == 0
    assert result["alias_origin"]["raw1"]["iteration"] == 0


def test_full_three_hour_window_cannot_be_replaced_by_exact_time_matching():
    rows = [fd(2, "a", "b"), fd(3, "c", "d", clock="22:00")]
    result = infer_identity(rows, [official("jc:1", "甲", "乙")], "Europe/London")
    assert result["aliases"] == {}  # second fixture is exactly +180 minutes
    assert result["comparison_rows"] == []


def test_reverse_uniqueness_is_required():
    result = infer_identity(
        [fd(2, "a", "b")],
        [official("jc:1", "甲", "乙"), official("jc:2", "丙", "丁")],
        "Europe/London",
    )
    assert result["aliases"] == {}


def test_anchor_propagation_uses_forward_and_reverse_team_constraints():
    rows = [fd(2, "a", "b"), fd(3, "a", "c", day="11/09/2026"), fd(4, "d", "e", day="11/09/2026")]
    js = [official("jc:1", "甲", "乙"), official("jc:2", "甲", "丙", day="2026-09-11")]
    result = infer_identity(rows, js, "Europe/London")
    assert result["aliases"] == {"a": "甲", "b": "乙", "c": "丙"}
    assert result["alias_origin"]["c"]["iteration"] == 1
    assert len(result["comparison_rows"]) == 2


def test_conflicting_name_rejects_entire_pairs_without_half_alias_leak():
    rows = [fd(2, "a", "b"), fd(3, "a", "c", day="11/09/2026")]
    js = [official("jc:1", "甲", "乙"), official("jc:2", "丁", "丙", day="2026-09-11")]
    result = infer_identity(rows, js, "Europe/London")
    assert result["aliases"] == {}  # b/c cannot leak from their conflicting pairs
    assert len(result["conflicts"]) == 2
    assert result["comparison_rows"] == []


def test_reverse_alias_collision_rejects_whole_pairs():
    rows = [fd(2, "a", "b"), fd(3, "c", "d", day="11/09/2026")]
    js = [official("jc:1", "甲", "乙"), official("jc:2", "甲", "丙", day="2026-09-11")]
    assert infer_identity(rows, js, "Europe/London")["aliases"] == {}


def witnesses(score=(2, 1)):
    return (
        [fd(2, "a", "b"), fd(3, "b", "a", day="11/09/2026", score=score)],
        [official("jc:1", "甲", "乙"), official("jc:2", "乙", "甲", day="2026-09-11")],
    )


def test_timezone_uses_exact_utc_and_discloses_equivalent_zones():
    rows, js = witnesses()
    result = verify_league(
        rows,
        js,
        {
            "div_code": "X",
            "league": "L",
            "candidate_zones": ["Europe/London", "Europe/Lisbon", "Europe/Berlin"],
        },
    )
    assert result["status"] == "verified"
    assert result["time_basis"] == "Europe/London"
    assert result["verified_by"] == 2
    assert result["equivalent_time_bases_in_observed_sample"] == ["Europe/London", "Europe/Lisbon"]
    assert result["zone_comparisons"][2]["exact"] == 0


def test_score_conflict_never_selects_names_and_prevents_time_verification():
    rows, js = witnesses(score=(4, 0))
    inferred = infer_identity(rows, js, "Europe/London")
    assert len(inferred["comparison_rows"]) == 2
    assert inferred["comparison_rows"][1]["score_agrees"] is False
    result = verify_league(
        rows, js, {"div_code": "X", "league": "L", "candidate_zones": ["Europe/London"]}
    )
    assert result["status"] == "unverified"
    assert result["aliases"] == {}


def test_one_exact_witness_does_not_verify_timezone():
    result = verify_league(
        [fd(2, "a", "b")],
        [official("jc:1", "甲", "乙")],
        {"div_code": "X", "league": "L", "candidate_zones": ["Europe/London"]},
    )
    assert result["status"] == "unverified"


def test_strict_dst_rejects_ambiguous_local_time():
    row = fd(2, "a", "b", day="25/10/2026", clock="01:30")
    result = infer_identity([row], [official("jc:1", "甲", "乙")], "Europe/London")
    assert result["aliases"] == {}
    assert result["rejected_rows"][0]["fd_row"] == 2


def test_load_rows_validates_sha_before_parsing(tmp_path):
    path = tmp_path / "input.csv"
    path.write_text("Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG\n", encoding="utf8")
    with pytest.raises(ValueError, match="SHA256"):
        load_rows({"path": str(path), "sha256": "0" * 64}, 2026)


def test_tied_nonequivalent_timebases_remain_unverified():
    rows = [fd(i + 2, "a", "b", day=f"{day:02}/09/2026") for i, day in enumerate((4, 11, 18, 25))]
    games = [
        official(
            f"jc:{i}", "甲", "乙", day=f"2026-09-{day:02}", clock="18:00" if i < 2 else "19:00"
        )
        for i, day in enumerate((4, 11, 18, 25))
    ]
    result = verify_league(
        rows, games, {"div_code": "X", "league": "L", "candidate_zones": ["Europe/London", "UTC"]}
    )
    assert [zone["exact"] for zone in result["zone_comparisons"]] == [2, 2]
    assert result["status"] == "unverified"


def test_strict_dst_rejects_nonexistent_spring_time():
    row = fd(2, "a", "b", day="29/03/2026", clock="01:30")
    result = infer_identity([row], [], "Europe/London")
    assert result["times"] == [None]
    assert len(result["rejected_rows"]) == 1


def test_end_to_end_outputs_are_replayable_and_inputs_remain_unchanged(tmp_path):
    import hashlib
    import json
    import sqlite3

    from zucai.sources.fd_verify import main

    db = tmp_path / "official.db"
    with sqlite3.connect(db) as connection:
        connection.executescript("""
            CREATE TABLE teams(id INTEGER, name TEXT);
            CREATE TABLE matches(match_id TEXT,league TEXT,kickoff_utc TEXT,
                                 home_team_id INTEGER,away_team_id INTEGER,source TEXT);
            CREATE TABLE results(match_id TEXT, home_goals INTEGER,away_goals INTEGER);
            INSERT INTO teams VALUES(1,'甲'),(2,'乙');
            INSERT INTO matches VALUES('jc:1','L','2026-09-04T18:00:00Z',1,2,'jc'),
                                      ('jc:2','L','2026-09-11T18:00:00Z',2,1,'jc'),
                                      ('jc:3','L','2026-11-01T18:00:00Z',1,2,'jc');
            INSERT INTO results VALUES('jc:1',2,1),('jc:2',2,1);
        """)
    raw = tmp_path / "sample.csv"
    raw.write_text(
        "Country,League,Season,Date,Time,Home,Away,HG,AG\n"
        "C,L,2026/2027,04/09/2026,19:00,a,b,2,1\n"
        "C,L,2026/2027,11/09/2026,19:00,b,a,2,1\n"
        "C,L,2025/2026,11/09/2025,19:00,old1,old2,1,0\n",
        encoding="utf8",
    )
    index = tmp_path / "index.json"
    digest = hashlib.sha256(raw.read_bytes()).hexdigest()
    index.write_text(json.dumps({"X": {"path": "sample.csv", "sha256": digest}}))
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            [dict(div_code="X", league="L", candidate_zones=["Europe/London", "Europe/Berlin"])]
        )
    )
    before = {p: p.read_bytes() for p in (raw, db, index, config)}
    output = tmp_path / "audit"
    assert (
        main(
            [
                "--db",
                str(db),
                "--index",
                str(index),
                "--config",
                str(config),
                "--year",
                "2026",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert all(path.read_bytes() == content for path, content in before.items())
    coverage = json.loads((output / "coverage.json").read_text(encoding="utf8"))["X"]
    assert coverage["comparable_jc"] == coverage["matched_jc"] == 2
    assert coverage["excluded_outside_fd_coverage"] == 1
    aliases = json.loads((output / "aliases_evidence.json").read_text(encoding="utf8"))["L"]
    assert set(aliases) == {"a", "b"}
    assert aliases["a"]["evidence"][0]["raw_sha256"] == digest
    assert (output / "gap_diagnostics.json").is_file()
