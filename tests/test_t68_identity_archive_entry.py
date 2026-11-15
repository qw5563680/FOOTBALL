"""Exercise the real frozen archive reader and replay, including source tampering."""

import hashlib
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from test_t68_jc_kickoffs import catalogue, save_json
from zucai import identity_binding_archive as reader
from zucai import identity_categories
from zucai import jc_catalogue_kickoffs as kickoffs
from zucai.cloud_feeds import TABLES
from zucai.identity_binding import replay

FIXTURES = Path(__file__).parents[1] / "data/fixtures"
CAPTURED = "2026-11-14T01:00:00Z"
CLOCK = "2026-10-31T12:15:00+00:00"


def seal(root):
    manifest_path = root / "feed_manifest.json"
    manifest = (
        json.loads(manifest_path.read_bytes())
        if manifest_path.exists()
        else dict(version=2, created_utc="2026-11-14T02:00:00Z", columns={}, counts={})
    )
    manifest["files"] = {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and p != manifest_path
    }
    save_json(manifest_path, manifest)


def table(root, name, rows):
    columns = list(TABLES[name])
    path = root / f"state/{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps([row.get(c) for c in columns]) + "\n" for row in rows))
    manifest_path = root / "feed_manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["columns"][name] = columns
    manifest["counts"][name] = len(rows)
    save_json(manifest_path, manifest)
    seal(root)


def raw(root, relative, value, url):
    path = root / relative
    save_json(path, value)
    save_json(
        path.with_suffix(path.suffix + ".meta.json"),
        dict(
            url=url,
            status_code=200,
            fetched_at_utc=CAPTURED,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        ),
    )
    return path


def pin_categories(tmp_path, monkeypatch, approved=()):
    """Use a copy of the category registry so tests do not follow the owner's real approvals."""
    document = json.loads(identity_categories.PATH.read_bytes())
    for code, entry in document["competitions"].items():
        entry["status"] = "approved" if code in approved else "proposed"
        if code in approved:
            entry["review_url"] = "https://github.com/qw5563680/FOOTBALL/pull/0#synthetic-review"
            entry["owner_approval"] = dict(by="qw5563680", utc=CAPTURED, basis="synthetic")
    path = tmp_path / "identity_categories.json"
    path.write_text(json.dumps(document, ensure_ascii=False))
    monkeypatch.setattr(identity_categories, "PATH", path)


@pytest.fixture
def archive(tmp_path, monkeypatch):
    pin_categories(tmp_path, monkeypatch)
    root = tmp_path / "archive"
    catalogue(root)
    monkeypatch.setattr(kickoffs, "CATALOGUE_SOURCE_RUN", "catalogue")
    monkeypatch.setattr(kickoffs, "CATALOGUE_WINDOW", (date(2026, 10, 31), date(2026, 10, 31)))
    seal(root)
    table(
        root,
        "teams",
        [
            dict(id=849, name="Unrelated numeric collision", aliases=[]),
            dict(id=1, name="Project home", aliases=[]),
            dict(id=2, name="Project away", aliases=[]),
        ],
    )
    table(
        root,
        "matches",
        [
            dict(
                match_id="jc:2042404",
                source="jc",
                league="英联赛杯",
                home_team_id=1,
                away_team_id=2,
                kickoff_utc=CLOCK,
                status="finished",
                result_provider="jc",
                result_checked_utc=CAPTURED,
            )
        ],
    )
    table(
        root,
        "results",
        [dict(match_id="jc:2042404", home_goals=1, away_goals=3, available_utc=CAPTURED)],
    )
    head = json.loads((FIXTURES / "t68/heads/success.json").read_bytes())
    head["value"]["fullCourtGoal"] = "1:3"
    head["value"]["matchDateTime"] = "2026-10-31 20:15"
    raw(
        root,
        "state/catalogue/heads/jc-heads/2042404.json",
        head,
        kickoffs.API + "getMatchHeadV1.qry?source=web&sportteryMatchId=2042404",
    )
    page = json.loads((FIXTURES / "t68/identity/apif-page.json").read_bytes())
    page["parameters"] = dict(league="48", season="2026")
    row = page["response"][0]
    row["league"] = dict(id=48, season=2026)
    row["fixture"].update(date=CLOCK, timestamp=int(datetime.fromisoformat(CLOCK).timestamp()))
    row["goals"] = row["score"]["fulltime"] = dict(home=1, away=3)
    raw(
        root,
        "state/catalogue/backfill/backfill/api-football/day/fixtures_one.json",
        page,
        "https://v3.football.api-sports.io/fixtures?league=48&season=2026",
    )
    seal(root)
    return root


def load(root):
    return reader.load_archive(root, leagues=["48"])


def test_actual_reader_uses_numbered_namespace_and_exact_snapshot_association(archive):
    inputs = load(archive)
    row = inputs["official"][0]
    assert row["home"] == "jc-team:849"
    assert row["away"] == "jc-team:508"
    assert inputs["teams"][row["home"]]["project_team_id"] == "1"
    assert inputs["teams"][row["home"]]["official_team_id"] == "849"
    assert inputs["teams"][row["home"]]["name"] == "加的夫城"
    assert inputs["teams"][row["home"]]["category_status"] == "proposed"


def test_category_approval_is_read_from_the_registry_and_lifts_only_the_category_gap(
    archive, tmp_path, monkeypatch
):
    before = replay(archive, archive.parent / "pending", leagues=["48"])
    pin_categories(tmp_path, monkeypatch, approved={"48"})
    assert load(archive)["teams"]["jc-team:849"]["category_status"] == "approved"
    after = replay(archive, archive.parent / "approved", leagues=["48"])

    def candidates(result):
        return {
            e["candidate_id"]: e
            for rows in result["apif"].values()
            for e in rows.values()
            if e.get("candidate_id")
        }

    pending, approved = candidates(before), candidates(after)
    assert pending and set(pending) == set(approved)
    for key, entry in pending.items():
        assert "category_approval_pending" in entry["approval_gaps"]
        assert "category_approval_pending" not in approved[key].get("approval_gaps", [])
        assert approved[key]["status"] == entry["status"] == "proposed"
        assert approved[key]["canonical_team_id"] == entry["canonical_team_id"]
    assert after["summary"]["production_writes"] == 0


def test_unknown_official_ids_get_creation_proposals_even_if_names_match(archive):
    table(archive, "matches", [])
    table(archive, "results", [])
    table(archive, "teams", [dict(id=849, name="加的夫城", aliases=[])])
    result = replay(archive, archive.parent / "out", leagues=["48"])
    rows = result["official_register"]["rows"]
    assert len(rows) == 2
    assert all(row["identity_status"] == "creation_proposed" for row in rows)
    assert all(row["project_team_id"] is None for row in rows)
    assert all(
        row["candidate_ids"] and "canonical_identity_creation_pending" in row["approval_gaps"]
        for row in rows
    )
    assert result["summary"]["strict_verified"] == 0
    assert result["summary"]["conditional_verified"] == 2


@pytest.mark.parametrize(
    "relative",
    [
        "state/catalogue/catalogue/jc/2026-10/2026-10-31-p1.json",
        "state/catalogue/heads/jc-heads/2042404.json",
    ],
)
def test_raw_and_self_consistent_sidecar_cannot_replace_manifest_evidence(archive, relative):
    path = archive / relative
    path.write_bytes(path.read_bytes() + b" ")
    meta = json.loads(path.with_suffix(".json.meta.json").read_bytes())
    meta["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    save_json(path.with_suffix(".json.meta.json"), meta)
    with pytest.raises(ValueError, match="manifest|Frozen"):
        load(archive)


def test_unlisted_supplementary_head_cannot_supply_clock(archive):
    table(archive, "matches", [])
    table(archive, "results", [])
    manifest = json.loads((archive / "feed_manifest.json").read_bytes())
    manifest["files"] = {p: h for p, h in manifest["files"].items() if "/jc-heads/" not in p}
    save_json(archive / "feed_manifest.json", manifest)
    row = load(archive)["official"][0]
    assert row["kickoff_utc"] is None


@pytest.mark.parametrize("change", ["cancelled", "score", "league", "kickoff", "same_team"])
def test_current_snapshot_contradiction_cannot_supply_project_identity(archive, change):
    if change == "score":
        table(archive, "results", [dict(match_id="jc:2042404", home_goals=4, away_goals=3)])
    else:
        manifest = json.loads((archive / "feed_manifest.json").read_bytes())
        columns = manifest["columns"]["matches"]
        row = dict(
            zip(columns, json.loads((archive / "state/matches.jsonl").read_text()), strict=True)
        )
        row.update(
            {
                "cancelled": dict(status="cancelled"),
                "league": dict(league="西甲"),
                "kickoff": dict(kickoff_utc="2026-10-31T14:15:00Z"),
                "same_team": dict(away_team_id=1),
            }[change]
        )
        table(archive, "matches", [row])
    inputs = load(archive)
    assert inputs["teams"]["jc-team:849"]["project_team_id"] is None
    assert any("current_snapshot_identity_conflict" in row["reasons"] for row in inputs["issues"])


@pytest.mark.parametrize("status", ["TBD", "PST"])
def test_apif_placeholder_clock_is_not_precise(archive, status):
    path = archive / "state/catalogue/backfill/backfill/api-football/day/fixtures_one.json"
    doc = json.loads(path.read_bytes())
    doc["response"][0]["fixture"]["status"]["short"] = status
    raw(
        archive,
        path.relative_to(archive),
        doc,
        "https://v3.football.api-sports.io/fixtures?league=48&season=2026",
    )
    seal(archive)
    assert load(archive)["apif"][0]["kickoff_utc"] is None


def test_real_reader_replays_are_byte_identical_and_register_every_item(archive):
    first, second = archive.parent / "one", archive.parent / "two"
    replay(archive, first, leagues=["48"])
    replay(archive, second, leagues=["48"])
    assert {
        p.relative_to(first).as_posix(): p.read_bytes() for p in first.rglob("*") if p.is_file()
    } == {
        p.relative_to(second).as_posix(): p.read_bytes() for p in second.rglob("*") if p.is_file()
    }
    register = json.loads((first / "official_register.json").read_bytes())
    assert register["official_ids"] == 2
    assert register["competition_items"] == 2


def preserve_snapshot(root):
    # Use the real kickoff snapshot writer, then independently freeze those bytes.
    table(
        root,
        "teams",
        [dict(id=1, name="加的夫城", aliases=[]), dict(id=2, name="切尔西", aliases=[])],
    )
    rows, _, catalogue_proof = kickoffs._catalogue(root)
    clocks, issues = kickoffs._database_evidence(root, rows, catalogue_proof)
    assert not issues and clocks
    kickoffs._preserve_evidence(clocks[2042404][0], rows[0], root, root, root / "unused")
    seal(root)


def test_archived_snapshot_must_not_authorize_different_current_sides(archive):
    preserve_snapshot(archive)
    document = json.loads((archive / "feed_manifest.json").read_bytes())
    values = json.loads((archive / "state/matches.jsonl").read_text())
    match = dict(zip(document["columns"]["matches"], values, strict=True))
    match["home_team_id"], match["away_team_id"] = 2, 1
    table(archive, "matches", [match])
    inputs = load(archive)
    assert inputs["teams"]["jc-team:849"]["project_team_id"] is None
    assert any("current_snapshot_identity_conflict" in r["reasons"] for r in inputs["issues"])


def test_snapshot_file_must_be_covered_by_outer_frozen_manifest(archive):
    preserve_snapshot(archive)
    path = next(archive.glob("state/catalogue/*/jc-db-matches.jsonl"))
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="manifest"):
        load(archive)


def test_capture_later_than_frozen_manifest_is_untrusted(archive):
    path = (
        archive / "state/catalogue/backfill/backfill/api-football/day/fixtures_one.json.meta.json"
    )
    doc = json.loads(path.read_bytes())
    doc["fetched_at_utc"] = "2026-11-14T03:00:00Z"
    save_json(path, doc)
    seal(archive)
    inputs = load(archive)
    assert not inputs["apif"] or not any(r["complete"] for r in inputs["apif"])


def test_official_display_name_change_does_not_change_numeric_project_link(archive):
    path = archive / "state/catalogue/catalogue/jc/2026-10/2026-10-31-p1.json"
    doc = json.loads(path.read_bytes())
    doc["value"]["matchResult"][0]["homeTeam"] = "Official renamed home"
    meta = json.loads(path.with_suffix(".json.meta.json").read_bytes())
    raw(archive, path.relative_to(archive), doc, meta["url"])
    seal(archive)
    team = load(archive)["teams"]["jc-team:849"]
    assert team["project_team_id"] == "1"
    assert team["name"] == "Official renamed home"


def test_complete_replay_is_independent_of_archive_location(archive):
    import shutil

    moved = archive.parent / "relocated"
    shutil.copytree(archive, moved)
    first = replay(archive, archive.parent / "original-out", leagues=["48"])
    second = replay(moved, archive.parent / "moved-out", leagues=["48"])
    assert first == second
    assert str(archive) not in json.dumps(first)


def test_duplicate_official_to_project_targets_are_conflicts(archive):
    path = archive / "state/catalogue/catalogue/jc/2026-10/2026-10-31-p1.json"
    doc = json.loads(path.read_bytes())
    second = dict(doc["value"]["matchResult"][0], matchId=2042405)
    doc["value"]["matchResult"].append(second)
    doc["value"]["total"] = 2
    meta = json.loads(path.with_suffix(".json.meta.json").read_bytes())
    raw(archive, path.relative_to(archive), doc, meta["url"])
    table(
        archive,
        "teams",
        [
            dict(id=1, name="Project home", aliases=[]),
            dict(id=2, name="Project away", aliases=[]),
            dict(id=3, name="Another project home", aliases=[]),
        ],
    )
    manifest = json.loads((archive / "feed_manifest.json").read_bytes())
    match = dict(
        zip(
            manifest["columns"]["matches"],
            json.loads((archive / "state/matches.jsonl").read_text()),
            strict=True,
        )
    )
    table(archive, "matches", [match, dict(match, match_id="jc:2042405", home_team_id=3)])
    result = dict(match_id="jc:2042404", home_goals=1, away_goals=3)
    table(archive, "results", [result, dict(result, match_id="jc:2042405")])
    inputs = load(archive)
    team = inputs["teams"]["jc-team:849"]
    assert team["project_team_id"] is None
    assert team["identity_status"] == "conflict"
    assert team["conflicting_project_ids"] == ["1", "3"]


def test_real_replay_outputs_readable_owner_evidence(archive):
    out = archive.parent / "owner-out"
    result = replay(archive, out, leagues=["48"])
    row = next(r for r in result["official_register"]["rows"] if r["official_team_id"] == "849")
    assert row["api_candidates"][0]["provider_team_id"] == "101"
    witness = row["api_candidates"][0]["witnesses"][0]
    assert witness["official_opponent"] == dict(name="切尔西", official_team_id="508")
    assert witness["score"] == [1, 3]
    assert witness["date"] == "2026-10-31"
    assert witness["apif_provenance"][0]["pointer"] == "/response/0"
    page = (out / "owner-review.html").read_text()
    assert "<table" in page and "加的夫城" in page and "849" in page
    assert "Alpha" in page and "101" in page and "待核准" in page
    assert "owner-review/48-849.md" in page


def test_owner_lists_all_ambiguous_time_candidates_without_score_selection(archive):
    path = archive / "state/catalogue/backfill/backfill/api-football/day/fixtures_one.json"
    doc = json.loads(path.read_bytes())
    from copy import deepcopy

    second = deepcopy(doc["response"][0])
    second["fixture"]["id"] = 11
    second["teams"] = dict(home=dict(id=103, name="Gamma"), away=dict(id=104, name="Delta"))
    second["goals"] = second["score"]["fulltime"] = dict(home=8, away=0)
    doc["response"].append(second)
    doc["results"] = 2
    raw(
        archive,
        path.relative_to(archive),
        doc,
        "https://v3.football.api-sports.io/fixtures?league=48&season=2026",
    )
    seal(archive)
    out = archive.parent / "ambiguous-out"
    result = replay(archive, out, leagues=["48"])
    row = next(r for r in result["official_register"]["rows"] if r["official_team_id"] == "849")
    assert row["candidate_ids"] == []
    assert {r["apif_match_id"] for r in row["possible_matches"]} == {"apif:10", "apif:11"}
    assert "同时有多场" in row["conclusion_zh"]
    assert "Gamma" in (out / "owner-review.html").read_text()


def test_owner_markdown_index_and_competition_pages_have_stable_team_anchors(archive):
    out = archive.parent / "markdown-out"
    replay(archive, out, leagues=["48"])
    index = (out / "owner-review.md").read_text()
    assert "owner-review/48.md" in index
    page = (out / "owner-review/48-849.md").read_text()
    assert '<a id="jc-849"></a>' in page
    assert "加的夫城" in page and "Alpha" in page
    assert "2026-10-31" in page and "切尔西" in page and "1:3" in page
    assert "/response/0" in page and "SHA256" in page


def test_archived_snapshot_cannot_claim_verification_after_its_own_manifest(archive):
    manifest = json.loads((archive / "feed_manifest.json").read_bytes())
    row = dict(
        zip(
            manifest["columns"]["matches"],
            json.loads((archive / "state/matches.jsonl").read_text()),
            strict=True,
        )
    )
    row["result_checked_utc"] = "2026-11-14T03:00:00Z"
    table(archive, "matches", [row])
    preserve_snapshot(archive)
    with pytest.raises(ValueError, match="snapshot verification"):
        load(archive)


def test_exported_candidate_keeps_exact_project_association_proof(archive):
    result = replay(archive, archive.parent / "numbered-proof-out", leagues=["48"])
    candidate = result["apif"]["英联赛杯"]["101"]
    proof = candidate["project_identity_evidence"][0]
    assert proof["official_ids"]["home"] == "849"
    assert proof["project_ids"]["home"] == "1"
    assert proof["matches_pointer"] == "state/matches.jsonl#/line/1"
    assert proof["results_pointer"] == "state/results.jsonl#/line/1"
    assert proof["project_team_pointers"]["home"] == "state/teams.jsonl#/line/2"


def test_consistent_cancellation_is_ineligible_not_global_identity_conflict(archive):
    path = archive / "state/catalogue/catalogue/jc/2026-10/2026-10-31-p1.json"
    doc = json.loads(path.read_bytes())
    doc["value"]["matchResult"][0]["poolStatus"] = "Refund"
    meta = json.loads(path.with_suffix(".json.meta.json").read_bytes())
    raw(archive, path.relative_to(archive), doc, meta["url"])
    manifest = json.loads((archive / "feed_manifest.json").read_bytes())
    match = dict(
        zip(
            manifest["columns"]["matches"],
            json.loads((archive / "state/matches.jsonl").read_text()),
            strict=True,
        )
    )
    match["status"] = "cancelled"
    table(archive, "matches", [match])
    table(archive, "results", [])
    inputs = load(archive)
    team = inputs["teams"]["jc-team:849"]
    assert team["identity_status"] == "creation_proposed"
    assert team["project_team_id"] is None
    assert not team["ambiguous"]
    assert not any("current_snapshot_identity_conflict" in i["reasons"] for i in inputs["issues"])
    assert any("cancelled_fixture_not_identity_witness" in i["reasons"] for i in inputs["issues"])
    result = replay(archive, archive.parent / "cancelled-out", leagues=["48"])
    assert not any(r["candidate_ids"] for r in result["official_register"]["rows"])


def test_later_reserve_label_blocks_real_replay_independently_of_observation_order(archive):
    from copy import deepcopy

    path = archive / "state/catalogue/catalogue/jc/2026-10/2026-10-31-p1.json"
    document = json.loads(path.read_bytes())
    later = dict(document["value"]["matchResult"][0], matchId=2042405, homeTeam="Jong Ajax")
    document["value"]["matchResult"].append(later)
    document["value"]["total"] = 2
    meta = json.loads(path.with_suffix(".json.meta.json").read_bytes())
    head = json.loads((archive / "state/catalogue/heads/jc-heads/2042404.json").read_bytes())
    head["value"].update(sportteryMatchId=2042405, matchDateTime="2026-10-31 08:15")
    raw(
        archive,
        "state/catalogue/heads/jc-heads/2042405.json",
        head,
        kickoffs.API + "getMatchHeadV1.qry?source=web&sportteryMatchId=2042405",
    )
    for reverse in (False, True):
        current = deepcopy(document)
        if reverse:
            current["value"]["matchResult"].reverse()
        raw(archive, path.relative_to(archive), current, meta["url"])
        seal(archive)
        result = replay(archive, archive.parent / f"label-order-{reverse}", leagues=["48"])
        row = next(r for r in result["official_register"]["rows"] if r["official_team_id"] == "849")
        assert row["candidate_ids"] == []
        assert "category_conflict" in row["reasons"]
        conflicts = [
            c for issue in result["unverified"] for c in issue.get("category_conflicts", [])
        ]
        assert any(
            c["label"] == "Jong Ajax"
            and c["match_id"] == "jc:2042405"
            and any(p["path"] == str(path.relative_to(archive)) for p in c["provenance"])
            for c in conflicts
        )


@pytest.mark.parametrize("league_id", [73, 25])
def test_existing_identity_from_other_competition_prevents_duplicate_creation(archive, league_id):
    """Generic/other competition observations bind IDs, not APIF match proposals."""
    catalogue(archive, ids=(2042404, 2042405), leagues=[24, league_id])
    page_path = archive / "state/catalogue/catalogue/jc/2026-10/2026-10-31-p1.json"
    page = json.loads(page_path.read_bytes())
    other = page["value"]["matchResult"][1]
    # Only the other competition is stored; selected cup has no project witness.
    table(
        archive,
        "matches",
        [
            dict(
                match_id="jc:2042405",
                source="jc",
                league=other["leagueNameAbbr"],
                home_team_id=1,
                away_team_id=2,
                kickoff_utc=CLOCK,
                status="finished",
                result_provider="jc",
                result_checked_utc=CAPTURED,
            )
        ],
    )
    table(archive, "results", [dict(match_id="jc:2042405", home_goals=1, away_goals=3)])
    # The public fixture's score is already 1:3.
    seal(archive)
    inputs = load(archive)
    assert {r["match_id"] for r in inputs["official"]} == {"jc:2042404"}
    assert inputs["teams"]["jc-team:849"]["project_team_id"] == "1"
    result = replay(archive, archive.parent / "cross-competition-out", leagues=["48"])
    assert len(result["official_register"]["rows"]) == 2
    assert all(r["identity_proposal"] is None for r in result["official_register"]["rows"])


def test_cross_competition_identity_conflict_is_not_hidden(archive):
    catalogue(archive, ids=(2042404, 2042405), leagues=[24, 73])
    page = json.loads(
        (archive / "state/catalogue/catalogue/jc/2026-10/2026-10-31-p1.json").read_bytes()
    )
    league = page["value"]["matchResult"][1]["leagueNameAbbr"]
    table(
        archive,
        "matches",
        [
            dict(
                match_id=mid,
                source="jc",
                league=comp,
                home_team_id=home,
                away_team_id=away,
                kickoff_utc=CLOCK,
                status="finished",
                result_provider="jc",
                result_checked_utc=CAPTURED,
            )
            for mid, comp, home, away in [
                ("jc:2042404", "英联赛杯", 1, 2),
                ("jc:2042405", league, 2, 1),
            ]
        ],
    )
    table(
        archive,
        "results",
        [dict(match_id=mid, home_goals=1, away_goals=3) for mid in ["jc:2042404", "jc:2042405"]],
    )
    inputs = load(archive)
    assert inputs["teams"]["jc-team:849"]["identity_status"] == "conflict"
    assert inputs["teams"]["jc-team:849"]["project_team_id"] is None


def test_owner_overview_is_compact_and_all_evidence_links_resolve(archive):
    out = archive.parent / "compact-owner"
    replay(archive, out, leagues=["48"])
    overview = (out / "owner-review/48.md").read_text()
    assert "竞彩球队 | 接口球队 | 证据场数 | 结论" in overview
    assert "48-849.md" in overview
    detail = (out / "owner-review/48-849.md").read_text()
    assert "/response/0" in detail and "SHA256" in detail
    assert "state/catalogue/" not in overview


def test_existing_duplicate_identities_are_exported_without_merging(archive):
    catalogue(archive, ids=(2042404, 2042405))
    rows, _ = reader._tables(archive)
    matches = rows["matches"]
    table(archive, "teams", rows["teams"] + [dict(id=3, name="Other project identity", aliases=[])])
    table(archive, "matches", [*matches, dict(matches[0], match_id="jc:2042405", home_team_id=3)])
    table(archive, "results", [*rows["results"], dict(rows["results"][0], match_id="jc:2042405")])
    before = {p.name: p.read_bytes() for p in (archive / "state").glob("*.jsonl")}
    out = archive.parent / "duplicate-review"
    result = replay(archive, out, leagues=["48"])
    item = next(
        r for r in result["existing_identity_duplicates"]["rows"] if r["source_id"] == "jc:849"
    )
    assert {r["project_team_id"] for r in item["identities"]} == {"1", "3"}
    assert item["evidence"] and item["status"] == "review_required_no_merge"
    assert (out / "existing-identity-duplicates.md").is_file()
    assert before == {p.name: p.read_bytes() for p in (archive / "state").glob("*.jsonl")}
