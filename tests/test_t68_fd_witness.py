"""T68 第八节第 13 项：只有英文名的 fd 球队按比赛见证对接口编号（合成样本，只测规则）。"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from zucai.identity_fd_witness import MIN_MATCHES, main, propose, witness_proposals

START = datetime(2024, 8, 10, 14, 30, tzinfo=UTC)
LEAGUE = "样例联赛"


def _season(team=9, provider="700", games=24, *, shift=timedelta(minutes=0), league=LEAGUE):
    """One fd-only team alternating home and away, each match mirrored by a provider row."""
    matches, rows = [], []
    for index in range(games):
        kickoff = START + timedelta(days=7 * index)
        home = index % 2 == 0
        opponent = 100 + index
        matches.append(
            dict(
                match_id=f"fd:X:{team}:{index}",
                source="fd",
                league=league,
                home_team_id=team if home else opponent,
                away_team_id=opponent if home else team,
                kickoff_utc=kickoff.isoformat(),
            )
        )
        rows.append(
            dict(
                kickoff=kickoff + shift,
                home=provider if home else str(5000 + index),
                away=str(5000 + index) if home else provider,
                match_id=f"apif:{team}{index:03d}",
                names=dict(home="Whatever" if home else "Other", away="Other"),
                sha256="0" * 64,
            )
        )
    return matches, rows


def _teams(*ids, extra=None):
    teams = {team: dict(name=f"English {team}", aliases=[]) for team in ids}
    for index in range(100, 140):
        teams[index] = dict(name=f"对手{index}", aliases=[])
    teams.update(extra or {})
    return teams


def _for_team(entries, team=9):
    return [entry for entry in entries if entry["project_team_id"] == str(team)]


def _one(entries, team=9):
    found = [entry for entry in entries if entry["project_team_id"] == str(team)]
    assert len(found) == 1
    return found[0]


def test_consistent_same_side_provider_id_is_proposed_for_registration():
    matches, rows = _season()
    entry = _one(witness_proposals(_teams(9), matches, {LEAGUE: rows}, {}))
    assert entry["status"] == "proposed" and entry["action"] == "register_provider_id"
    assert entry["provider_team_id"] == "700"
    assert (entry["matches"], entry["covered"]) == (24, 24)
    assert (entry["home_witnesses"], entry["away_witnesses"]) == (12, 12)
    assert len(entry["witnesses"]) == 24 and entry["reasons"] == []
    assert entry["witnesses"][0]["provider_match_id"] == "apif:9000"


def test_team_names_do_not_take_part_in_the_decision():
    matches, rows = _season()
    for row in rows:
        row["names"] = dict(home="Completely", away="Unrelated")
    renamed = _teams(9)
    renamed[9]["name"] = "Nothing alike"
    entry = _one(witness_proposals(renamed, matches, {LEAGUE: rows}, {}))
    assert entry["status"] == "proposed" and entry["provider_team_id"] == "700"


def test_provider_id_with_an_approved_entry_for_another_stored_team_is_same_team():
    matches, rows = _season()
    formal = {"别的联赛": {"700": {"name": "X", "canonical": "中文队", "status": "approved"}}}
    teams = _teams(9, extra={77: dict(name="中文队", aliases=[])})
    entry = _one(witness_proposals(teams, matches, {LEAGUE: rows}, formal))
    assert entry["status"] == "proposed" and entry["action"] == "same_team"
    assert entry["same_as_project_team_ids"] == ["77"] and entry["same_as_names"] == ["中文队"]


def test_a_proposed_alias_entry_is_not_an_approval():
    matches, rows = _season()
    formal = {"别的联赛": {"700": {"name": "X", "canonical": "中文队", "status": "proposed"}}}
    teams = _teams(9, extra={77: dict(name="中文队", aliases=[])})
    entry = _one(witness_proposals(teams, matches, {LEAGUE: rows}, formal))
    assert entry["action"] == "register_provider_id"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("few", "fewer_than_min_matches"),
        ("exception", "no_consistent_provider_id"),
        ("uncovered", "coverage_below_threshold"),
        ("home_only", "missing_home_or_away_witness"),
        ("no_records", "no_provider_records_for_league"),
        ("outside_window", "coverage_below_threshold"),
    ],
)
def test_failed_condition_gives_no_proposal_and_names_the_reason(change, reason):
    games = MIN_MATCHES - 1 if change == "few" else 24
    shift = timedelta(hours=3, minutes=1) if change == "outside_window" else timedelta(0)
    matches, rows = _season(games=games, shift=shift)
    if change == "exception":
        rows[5]["home" if rows[5]["home"] == "700" else "away"] = "701"
    if change == "uncovered":
        rows = rows[:20]
    if change == "home_only":
        for row in rows:
            if row["away"] == "700":
                row["away"], row["home"] = row["home"], "700"
        for match in matches:
            if match["away_team_id"] == 9:
                match["away_team_id"], match["home_team_id"] = match["home_team_id"], 9
    provider_rows = {} if change == "no_records" else {LEAGUE: rows}
    entry = _one(witness_proposals(_teams(9), matches, provider_rows, {}))
    assert entry["status"] == "unverified" and entry["action"] is None
    assert reason in entry["reasons"]


def test_several_provider_ids_fitting_every_match_are_not_resolved_by_guessing():
    matches, rows = _season()
    twins = []
    for row in rows:
        twin = dict(row, match_id=row["match_id"] + "b")
        side = "home" if row["home"] == "700" else "away"
        twin[side] = "701"
        twins.append(twin)
    entry = _one(witness_proposals(_teams(9), matches, {LEAGUE: rows + twins}, {}))
    assert entry["status"] == "unverified"
    assert entry["reasons"] == ["several_consistent_provider_ids"]


def test_one_provider_id_claimed_by_two_fd_teams_is_rejected_for_both():
    first, rows = _season(team=9)
    second = [
        dict(
            match,
            match_id=match["match_id"] + ":dup",
            home_team_id=10 if match["home_team_id"] == 9 else match["home_team_id"],
            away_team_id=10 if match["away_team_id"] == 9 else match["away_team_id"],
        )
        for match in first
    ]
    entries = witness_proposals(_teams(9, 10), first + second, {LEAGUE: rows}, {})
    for team in (9, 10):
        entry = _one(entries, team)
        assert entry["status"] == "unverified"
        assert "provider_id_claimed_by_several_fd_teams" in entry["reasons"]


def test_teams_with_other_sources_or_an_existing_provider_identity_are_left_alone():
    matches, rows = _season()
    with_jc = matches + [
        dict(
            match_id="jc:1",
            source="jc",
            league=LEAGUE,
            home_team_id=9,
            away_team_id=100,
            kickoff_utc=START.isoformat(),
        )
    ]
    assert not [
        e
        for e in witness_proposals(_teams(9), with_jc, {LEAGUE: rows}, {})
        if e["project_team_id"] == "9"
    ]
    aliased = _teams(9)
    aliased[9]["aliases"] = ["apif:欧罗巴:team:700"]
    assert not _for_team(witness_proposals(aliased, matches, {LEAGUE: rows}, {}))
    formal = {LEAGUE: {"700": {"name": "X", "canonical": "English 9", "status": "approved"}}}
    assert not _for_team(witness_proposals(_teams(9), matches, {LEAGUE: rows}, formal))


def test_different_provider_ids_in_two_leagues_block_both():
    first, rows_a = _season(provider="700")
    second, rows_b = _season(provider="800", league="另一联赛")
    for match in second:
        match["match_id"] += ":b"
    entries = [
        entry
        for entry in witness_proposals(
            _teams(9), first + second, {LEAGUE: rows_a, "另一联赛": rows_b}, {}
        )
        if entry["project_team_id"] == "9"
    ]
    assert len(entries) == 2
    assert all(entry["status"] == "unverified" for entry in entries)
    assert all("conflicting_provider_ids_across_leagues" in e["reasons"] for e in entries)


def _archive(tmp_path, matches, rows):
    archive = tmp_path / "archive"
    (archive / "state/audit").mkdir(parents=True)
    teams = _teams(9)
    columns = dict(
        teams=["id", "name", "aliases"],
        matches=["match_id", "source", "league", "home_team_id", "away_team_id", "kickoff_utc"],
    )
    (archive / "feed_manifest.json").write_text(json.dumps(dict(columns=columns)))
    (archive / "state/teams.jsonl").write_text(
        "".join(
            json.dumps([key, value["name"], value["aliases"]], ensure_ascii=False) + "\n"
            for key, value in teams.items()
        ),
        encoding="utf-8",
    )
    (archive / "state/matches.jsonl").write_text(
        "".join(
            json.dumps([m[c] for c in columns["matches"]], ensure_ascii=False) + "\n"
            for m in matches
        ),
        encoding="utf-8",
    )
    header = (
        "provider_match_id,league_id,league,season,kickoff_utc,home_provider_id,"
        "away_provider_id,home,away,raw_sha256,complete,scope_size\n"
    )
    lines = [
        f"{row['match_id'][5:]},1,{LEAGUE},2024,{row['kickoff'].isoformat()},{row['home']},"
        f"{row['away']},H,A,{'0' * 64},true,24\n"
        for row in rows
    ]
    # An incomplete scope row must be ignored, whatever it claims.
    lines.append(f"999999,1,{LEAGUE},2024,{START.isoformat()},701,702,H,A,{'0' * 64},false,24\n")
    (archive / "state/audit/apif_identity_matches.csv").write_text(
        header + "".join(lines), encoding="utf-8"
    )
    formal = tmp_path / "formal.json"
    formal.write_text("{}")
    return archive, formal


def test_archive_entry_reads_identity_records_and_writes_only_a_new_proposal_folder(
    tmp_path, monkeypatch
):
    matches, rows = _season()
    archive, formal = _archive(tmp_path, matches, rows)
    before = {p: p.read_bytes() for p in archive.rglob("*") if p.is_file()}
    document = propose(archive, formal_path=formal)
    assert document["requests"] == 0 and document["production_writes"] == 0
    entry = _one(document["proposals"])
    assert entry["status"] == "proposed" and entry["provider_team_id"] == "700"
    assert document["inputs"]["identity_matches_sha256"]

    monkeypatch.setattr("zucai.identity_binding_archive.FORMAL", formal)
    out = tmp_path / "out"
    main(["--archive", str(archive), "--out", str(out)])
    written = json.loads((out / "fd-witness.json").read_bytes())
    assert written["proposals"] == document["proposals"]
    review = (out / "owner-review.md").read_text(encoding="utf-8")
    assert "提案：给这支球队登记该接口编号" in review and "English 9（9）" in review
    assert {p: p.read_bytes() for p in archive.rglob("*") if p.is_file()} == before
    with pytest.raises(SystemExit):
        main(["--archive", str(archive), "--out", str(out)])
    with pytest.raises(SystemExit):
        main(["--archive", str(archive), "--out", str(archive / "inside")])


def test_stored_provider_matches_count_through_provisional_names(tmp_path):
    matches, rows = _season()
    archive, formal = _archive(tmp_path, matches, [])
    teams = _teams(9)
    stored = []
    for index, row in enumerate(rows):
        for side in ("home", "away"):
            teams[9000 + int(row[side])] = dict(name=f"apif:team:{row[side]}", aliases=[])
        stored.append(
            dict(
                match_id=row["match_id"],
                source="apif",
                league=LEAGUE,
                home_team_id=9000 + int(row["home"]),
                away_team_id=9000 + int(row["away"]),
                kickoff_utc=row["kickoff"].isoformat(),
            )
        )
        assert index >= 0
    columns = ["match_id", "source", "league", "home_team_id", "away_team_id", "kickoff_utc"]
    (archive / "state/teams.jsonl").write_text(
        "".join(
            json.dumps([key, value["name"], value["aliases"]], ensure_ascii=False) + "\n"
            for key, value in teams.items()
        ),
        encoding="utf-8",
    )
    (archive / "state/matches.jsonl").write_text(
        "".join(
            json.dumps([m[c] for c in columns], ensure_ascii=False) + "\n" for m in matches + stored
        ),
        encoding="utf-8",
    )
    entry = _one(propose(archive, formal_path=formal)["proposals"])
    assert entry["status"] == "proposed" and entry["provider_team_id"] == "700"
