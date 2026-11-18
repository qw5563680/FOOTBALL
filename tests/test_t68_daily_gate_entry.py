"""Real gate entry regressions; all sports inputs originate in offline fixtures."""

import json
import shutil
import sqlite3
from copy import deepcopy
from datetime import date, datetime, timedelta
from hashlib import sha256

import pytest

from test_t68_identity_archive_entry import archive as frozen_archive  # noqa: F401
from test_t68_identity_archive_entry import raw, seal, table
from test_t68_jc_kickoffs import FIXTURES, save_json
from zucai import identity_binding_archive as reader
from zucai import identity_daily_gate as gate
from zucai import jc_catalogue_kickoffs as kickoffs
from zucai.cloud_feeds import TABLES, source_inventory
from zucai.sources.apif_ingest import COUNT_KEYS

CUTOFF = "2026-11-14T16:30:00Z"


def ref(root, path):
    return dict(
        path=path.relative_to(root).as_posix(), sha256=sha256(path.read_bytes()).hexdigest()
    )


def document(root, name, value):
    path = root / name
    save_json(path, value)
    return ref(root, path)


def change(root, reference, update):
    path = root / reference["path"]
    value = json.loads(path.read_bytes())
    update(value)
    save_json(path, value)
    reference.update(ref(root, path))


@pytest.fixture
def bundle(frozen_archive, monkeypatch, request):  # noqa: F811
    archive = frozen_archive
    root = archive.parent
    # Preserve exact project IDs while deliberately using different official labels.
    table(
        archive,
        "teams",
        [dict(id=1, name="川崎前锋", aliases=[]), dict(id=2, name="东京", aliases=[])],
    )
    manifest = json.loads((archive / "feed_manifest.json").read_bytes())
    values = json.loads((archive / "state/matches.jsonl").read_text())
    match = dict(zip(manifest["columns"]["matches"], values, strict=True))
    match.update(league="日职", counterpart_id="apif:10")
    table(archive, "matches", [match])
    pagepath = archive / "state/catalogue/catalogue/jc/2026-10/2026-10-31-p1.json"
    page = json.loads(pagepath.read_bytes())
    page["value"]["matchResult"][0].update(
        leagueId=42, leagueName="日本职业联赛", leagueNameAbbr="日职"
    )
    historical = getattr(request, "param", False)
    old_page = deepcopy(page)
    if historical:
        old_page["value"]["matchResult"][0].update(
            matchId=2042405,
            matchDate="2026-06-30",
            homeTeamId=900,
            homeTeam="Historical official home",
        )
        table(
            archive,
            "teams",
            [
                dict(id=1, name="川崎前锋", aliases=[]),
                dict(id=2, name="东京", aliases=[]),
                dict(id=3, name="Historical project home", aliases=[]),
            ],
        )
        table(
            archive,
            "matches",
            [
                match,
                dict(
                    match,
                    match_id="jc:2042405",
                    home_team_id=3,
                    kickoff_utc="2026-06-30T12:15:00Z",
                    counterpart_id=None,
                ),
            ],
        )
        table(
            archive,
            "results",
            [
                dict(match_id=mid, home_goals=1, away_goals=3, available_utc="2026-11-14T01:00:00Z")
                for mid in ["jc:2042404", "jc:2042405"]
            ],
        )
        head = json.loads((archive / "state/catalogue/heads/jc-heads/2042404.json").read_bytes())
        head["value"].update(
            sportteryMatchId=2042405, sportteryHomeTeamId=900, matchDateTime="2026-06-30 20:15"
        )
        raw(
            archive,
            "state/catalogue/heads/jc-heads/2042405.json",
            head,
            kickoffs.API + "getMatchHeadV1.qry?source=web&sportteryMatchId=2042405",
        )
    empty = deepcopy(page)
    empty["value"].update(matchResult=[], total=0)
    day = date(2025, 11, 1)
    while day <= date(2026, 10, 31):
        raw(
            archive,
            f"state/catalogue/catalogue/jc/{day:%Y-%m}/{day}-p1.json",
            page
            if day == date(2026, 10, 31)
            else old_page
            if historical and day == date(2026, 6, 30)
            else empty,
            kickoffs.API + f"getUniformMatchResultV1.qry?matchBeginDate={day}&matchEndDate={day}"
            "&leagueId=&pageSize=30&pageNo=1&isFix=0&matchPage=1&pcOrWap=1",
        )
        day += timedelta(days=1)
    window = dict(start_beijing="2025-11-01", end_beijing="2026-10-31")
    save_json(archive / "state/catalogue/catalogue/jc.json", dict(status="complete", window=window))
    monkeypatch.setattr(kickoffs, "CATALOGUE_WINDOW", (date(2025, 11, 1), date(2026, 10, 31)))
    apath = archive / "state/catalogue/backfill/backfill/api-football/day/fixtures_one.json"
    apif = json.loads(apath.read_bytes())
    apif["parameters"] = dict(league="98", season="2027")
    apif["response"][0]["league"] = dict(id=98, season=2027)
    apif["response"][0]["teams"]["home"]["id"] = 294
    apif["response"][0]["teams"]["away"]["id"] = 292
    raw(
        archive,
        apath.relative_to(archive),
        apif,
        "https://v3.football.api-sports.io/fixtures?league=98&season=2027",
    )
    held = (
        "raw_sha256,competition_code,league,provider_match_id\n"
        + sha256(apath.read_bytes()).hexdigest()
        + ",98,日职,10\n"
    )
    (archive / "state/audit").mkdir()
    (archive / "state/audit/apif_held_unbound.csv").write_text(held)
    (archive / "ledger.jsonl").write_text("frozen offline ledger\n")
    for name in ["odds", "closing_odds"]:
        table(archive, name, [])
    manifest = json.loads((archive / "feed_manifest.json").read_bytes())
    with sqlite3.connect(":memory:") as db:
        for name, columns in TABLES.items():
            db.execute(f"CREATE TABLE {name} ({','.join(columns)})")
            for line in (archive / f"state/{name}.jsonl").read_text().splitlines():
                values = json.loads(line)
                db.execute(
                    f"INSERT INTO {name} VALUES ({','.join('?' for _ in values)})",
                    [json.dumps(v) if isinstance(v, list) else v for v in values],
                )
        manifest["sources"] = source_inventory(db)
    save_json(archive / "feed_manifest.json", manifest)
    seal(archive)
    season_raw = raw(
        archive,
        "state/catalogue/catalogue/apif-raw.json",
        dict(
            get="leagues",
            parameters={},
            errors=[],
            results=1,
            paging=dict(current=1, total=1),
            response=[
                dict(
                    league=dict(id=98),
                    seasons=[dict(year=2027, start="2026-08-01", end="2027-06-30", current=True)],
                )
            ],
        ),
        "https://v3.football.api-sports.io/leagues",
    )
    shutil.copyfile(season_raw.with_suffix(".json.meta.json"), season_raw.parent / "apif-meta.json")
    seal(archive)
    counts = dict.fromkeys(COUNT_KEYS, 0)
    counts["unchanged"] = 1
    scope = dict(
        code="98",
        season=2027,
        status="complete",
        parsed_matches=1,
        raw={"sha256": sha256(apath.read_bytes()).hexdigest()},
        **counts,
    )
    current = dict(
        report=document(root, "current.json", dict(processed_files=1, scopes=[scope])),
        held_before=ref(root, archive / "state/audit/apif_held_unbound.csv"),
        held_before_manifest=ref(root, archive / "feed_manifest.json"),
    )
    originals = []
    for offset in range(31):
        day = date(2026, 10, 15) + timedelta(days=offset)
        doc = deepcopy(apif)
        doc["parameters"] = {"date": str(day)}
        if day != date(2026, 10, 31):
            doc.update(response=[], results=0)
        path = raw(
            root, f"daily/{day}.json", doc, f"https://v3.football.api-sports.io/fixtures?date={day}"
        )
        meta = json.loads(path.with_suffix(".json.meta.json").read_bytes())
        meta["fetched_at_utc"] = CUTOFF
        save_json(path.with_suffix(".json.meta.json"), meta)
        originals.append(ref(root, path))
    supplements = []
    for number in range(1, 15):
        day = date(2026, 11, number)
        path = raw(
            root,
            f"jc/{day}.json",
            empty,
            kickoffs.API + f"getUniformMatchResultV1.qry?matchBeginDate={day}&matchEndDate={day}"
            "&leagueId=&pageSize=30&pageNo=1&isFix=0&matchPage=1&pcOrWap=1",
        )
        meta = json.loads(path.with_suffix(".json.meta.json").read_bytes())
        meta["fetched_at_utc"] = CUTOFF
        save_json(path.with_suffix(".json.meta.json"), meta)
        supplements.append(ref(root, path))
    persistence = dict(
        repeat_report=document(
            root,
            "repeat.json",
            dict(inserted=0, status="complete", processed_files=1, scopes=[scope]),
        )
    )
    # Independent materialized exports, with actual project schemas and rows.
    for name, key in [
        ("after", "after_manifest"),
        ("restored", "restored_manifest"),
        ("preimport", "unused"),
    ]:
        folder = root / name
        (folder / "state/audit").mkdir(parents=True)
        for rel in [
            "state/teams.jsonl",
            "state/matches.jsonl",
            "state/results.jsonl",
            "state/odds.jsonl",
            "state/closing_odds.jsonl",
            "state/audit/apif_held_unbound.csv",
            "ledger.jsonl",
        ]:
            shutil.copyfile(archive / rel, folder / rel)
        m = json.loads((archive / "feed_manifest.json").read_bytes())
        m["files"] = {rel: digest for rel, digest in m["files"].items() if (folder / rel).is_file()}
        save_json(folder / "feed_manifest.json", m)
        if key != "unused":
            persistence[key] = ref(root, folder / "feed_manifest.json")
    persistence["before_manifest"] = ref(root, archive / "feed_manifest.json")
    snapshot = dict(
        coverage={"日职": {"jc": 2 if historical else 1}},
        relations=[["jc:2042404", "apif:10"]],
        quarantine={},
        training_selection=["jc:2042404", "jc:2042405"] if historical else ["jc:2042404"],
        immutable_sha256={
            "ledger.jsonl": sha256((archive / "ledger.jsonl").read_bytes()).hexdigest()
        },
    )
    for key in ["before_repeat", "after_repeat", "restored"]:
        persistence[key] = document(root, key + ".json", snapshot)
    results = dict(
        fixture_ids=["apif:10"],
        raw_sha256=[r["sha256"] for r in originals],
        inserted=0,
        risk_ids=[],
        outcomes=[dict(fixture_id="apif:10", outcome="unchanged")],
        official_match_ids=["jc:2042404"],
    )
    recent = dict(
        originals=originals,
        official_originals=supplements,
        before_manifest=ref(root, root / "preimport/feed_manifest.json"),
        after_manifest=ref(root, archive / "feed_manifest.json"),
        results=document(root, "recent.json", results),
    )
    current.update(
        before_manifest=recent["before_manifest"], after_manifest=recent["after_manifest"]
    )
    evidence = dict(
        provider_code="98",
        cutoff_utc=CUTOFF,
        archive_sha256=ref(root, archive / "feed_manifest.json")["sha256"],
        current_season_replay=current,
        recent_replay=recent,
        risk_review=document(root, "risks.json", []),
        persistence=persistence,
    )
    result = (archive, root, evidence)
    importer_bundle(result)
    return result


def run(bundle):
    archive, root, evidence = bundle
    return gate.build_register(
        archive, CUTOFF, codes=["98"], evidence={"98": evidence}, evidence_root=root
    )["leagues"][0]


def test_actual_build_and_main_accept_nonempty_consistent_evidence(bundle):
    result = run(bundle)
    assert result["teams"]["bound"] == 2
    assert result["matches"]["linked"] == 1
    assert result["status"] == "ready_for_review", result["evidence"]
    archive, root, evidence = bundle
    document(root, "evidence.json", {"98": evidence})
    gate.main(
        [
            "--archive",
            str(archive),
            "--cutoff",
            CUTOFF,
            "--codes",
            "98",
            "--evidence",
            str(root / "evidence.json"),
            "--out",
            str(root / "gate.json"),
        ]
    )
    assert json.loads((root / "gate.json").read_bytes())["leagues"][0] == result
    assert result["daily_changed"] is False


@pytest.mark.parametrize(
    "attack,section,reason",
    [
        ("same_exports", "persistence", "independent"),
        ("empty_snapshot", "persistence", "snapshot"),
        ("empty_held", "current_season_replay", "held"),
        ("missing_counts", "current_season_replay", "counts"),
        ("held_outcome", "recent_replay", "held_unbound"),
        ("invented_inserted", "recent_replay", "inserted"),
        ("omitted_game", "recent_replay", "known"),
        ("early_api", "recent_replay", "capture"),
        ("hidden_risks", "risk_review", "risk"),
        ("duplicate_binding", "blockers", "non_unique"),
        ("early_jc", "recent_replay", "capture"),
    ],
)
def test_actual_entry_rejects_each_n2_attack(bundle, monkeypatch, attack, section, reason):
    archive, root, evidence = bundle
    if attack == "same_exports":
        for key in ["after_manifest", "restored_manifest"]:
            evidence["persistence"][key] = evidence["persistence"]["before_manifest"]
    elif attack == "empty_snapshot":
        for key in ["before_repeat", "after_repeat", "restored"]:
            change(
                root,
                evidence["persistence"][key],
                lambda d: d.update(coverage={}, relations=[], training_selection=[]),
            )
    elif attack == "empty_held":
        path = root / "empty-held.csv"
        path.write_text("raw_sha256,competition_code,league,provider_match_id\n")
        evidence["current_season_replay"]["held_before"] = ref(root, path)
    elif attack == "missing_counts":
        change(
            root,
            evidence["current_season_replay"]["report"],
            lambda d: [d["scopes"][0].pop(k) for k in COUNT_KEYS],
        )
    elif attack in {"held_outcome", "invented_inserted", "hidden_risks"}:

        def mutate(d):
            if attack == "invented_inserted":
                d["inserted"] = 999
            else:
                d["outcomes"][0]["outcome"] = (
                    "held_unbound" if attack == "held_outcome" else "suspected"
                )
                if attack == "hidden_risks":
                    d["risk_ids"] = ["apif:10"]

        change(root, evidence["recent_replay"]["results"], mutate)
    elif attack == "omitted_game":
        reference = next(r for r in evidence["recent_replay"]["originals"] if "10-31" in r["path"])
        change(root, reference, lambda d: d.update(response=[], results=0))
        path = root / reference["path"]
        meta = json.loads(path.with_suffix(".json.meta.json").read_bytes())
        meta["sha256"] = reference["sha256"]
        save_json(path.with_suffix(".json.meta.json"), meta)
    elif attack in {"early_api", "early_jc"}:
        reference = evidence["recent_replay"][
            "originals" if attack == "early_api" else "official_originals"
        ][-1]
        path = root / reference["path"]
        meta = json.loads(path.with_suffix(".json.meta.json").read_bytes())
        meta["fetched_at_utc"] = "2026-11-01T00:00:00Z"
        save_json(path.with_suffix(".json.meta.json"), meta)
    elif attack == "duplicate_binding":
        formal = json.loads(reader.FORMAL.read_bytes())
        formal["日职"]["9999"] = deepcopy(formal["日职"]["294"])
        document(root, "aliases.json", formal)
        monkeypatch.setattr(reader, "FORMAL", root / "aliases.json")
    result = run(bundle)
    assert result["status"] != "ready_for_review"
    if section == "blockers":
        assert any(reason in value for value in result["blockers"]), result
    else:
        assert section in result["missing_evidence"], result
        assert reason in result["evidence"][section]["reason"], result["evidence"][section]


@pytest.mark.parametrize("bundle", [True], indirect=True)
@pytest.mark.parametrize(
    "problem", [None, "unknown", "duplicate", "empty_reason", "empty_evidence"]
)
def test_historical_unbound_explanation_is_verified_without_creating_binding(bundle, problem):
    archive, root, evidence = bundle
    row = dict(
        official_team_id="900",
        reason_code="no_unique_approved_scoped_binding",
        reason="Historical club has no approved API binding; remains unbound.",
        evidence=ref(root, archive / "state/catalogue/catalogue/jc/2026-06/2026-06-30-p1.json"),
    )
    if problem == "unknown":
        row["official_team_id"] = "99999"
    elif problem == "empty_reason":
        row["reason"] = "   "
    elif problem == "empty_evidence":
        path = root / "empty.txt"
        path.write_text("")
        row["evidence"] = ref(root, path)
    evidence["unbound_explanations"] = document(
        root, "unbound.json", [row, row] if problem == "duplicate" else [row]
    )
    result = run(bundle)
    assert result["teams"]["total"] == 3
    assert result["teams"]["bound"] == 2
    if problem:
        assert "unbound_explanations" in result["missing_evidence"]
    else:
        assert result["status"] == "ready_for_review", result
        assert result["teams"]["unbound"][0]["official_team_id"] == "900"
        assert result["evidence"]["unbound_explanations"]["explained_ids"] == ["900"]


def test_current_roster_unbound_explanation_never_authorizes_exception(bundle, monkeypatch):
    archive, root, evidence = bundle
    formal = json.loads(reader.FORMAL.read_bytes())
    formal["日职"].pop("294")
    document(root, "without-home.json", formal)
    monkeypatch.setattr(reader, "FORMAL", root / "without-home.json")
    evidence["unbound_explanations"] = document(
        root,
        "unbound.json",
        [
            dict(
                official_team_id="849",
                reason="Not approved.",
                evidence=ref(
                    root, archive / "state/catalogue/catalogue/jc/2026-06/2026-06-30-p1.json"
                ),
            )
        ],
    )
    result = run(bundle)
    assert "current_roster_unbound" in result["blockers"]
    assert result["status"] != "ready_for_review"


def test_restored_immutable_bytes_are_verified(bundle):
    archive, root, evidence = bundle
    (root / "restored/ledger.jsonl").write_text("changed")
    result = run(bundle)
    assert "persistence" in result["missing_evidence"]


@pytest.mark.parametrize("mutation", ["score", "side", "time", "league"])
def test_replay_actual_state_must_match_retained_fixture_original(bundle, mutation):
    archive, root, evidence = bundle
    folder = root / "preimport"
    # Both before and after claim unchanged, but the actual exported state contradicts raw.
    for target in [folder, root / "after"]:
        manifest = json.loads((target / "feed_manifest.json").read_bytes())
        name = "results" if mutation == "score" else "matches"
        values = json.loads((target / f"state/{name}.jsonl").read_text())
        row = dict(zip(manifest["columns"][name], values, strict=True))
        if mutation == "score":
            row["home_goals"] = 9
        elif mutation == "side":
            row["home_team_id"], row["away_team_id"] = 2, 1
        elif mutation == "time":
            row["kickoff_utc"] = "2026-10-31T00:15:00Z"
        else:
            row["league"] = "Different league"
        table(target, name, [row])
        # Source hashes independently recalculated from the fixture's actual SQL rows.
        manifest = json.loads((target / "feed_manifest.json").read_bytes())
        with sqlite3.connect(":memory:") as db:
            for table_name, columns in TABLES.items():
                db.execute(f"CREATE TABLE {table_name} ({','.join(columns)})")
                for line in (target / f"state/{table_name}.jsonl").read_text().splitlines():
                    vals = json.loads(line)
                    db.execute(
                        f"INSERT INTO {table_name} VALUES ({','.join('?' for _ in vals)})",
                        [json.dumps(v) if isinstance(v, list) else v for v in vals],
                    )
            manifest["sources"] = source_inventory(db)
        save_json(target / "feed_manifest.json", manifest)
    evidence["recent_replay"]["before_manifest"] = ref(root, folder / "feed_manifest.json")
    evidence["recent_replay"]["after_manifest"] = ref(root, root / "after/feed_manifest.json")
    result = run(bundle)
    assert "recent_replay" in result["missing_evidence"], result
    assert "original" in result["evidence"]["recent_replay"]["reason"]


def test_training_inventory_uses_actual_availability_cutoff(bundle):
    from zucai.identity_gate_evidence import export, snapshot

    archive, root, evidence = bundle
    state = export(root, evidence["persistence"]["before_manifest"])
    state["tables"]["results"][0]["available_utc"] = None
    # No source result was known at the cutoff; a list of finished IDs is not selection.
    assert snapshot(state)["training_selection"] == []


@pytest.mark.parametrize("problem", [None, "score", "side", "clock", "future", "multiple_owners"])
def test_real_entry_validates_auxiliary_fd_endpoint_without_relabeling_archive(bundle, problem):
    archive, root, _ = bundle
    manifest = json.loads((archive / "feed_manifest.json").read_bytes())
    match = dict(
        zip(
            manifest["columns"]["matches"],
            json.loads((archive / "state/matches.jsonl").read_text()),
            strict=True,
        )
    )
    match["counterpart_id"] = "fd:JPN:20261031:kawasaki-frontale:fc-tokyo"
    rows = [match]
    if problem == "multiple_owners":
        rows.append(dict(match, match_id="jc:2042405"))
        table(
            archive,
            "results",
            [
                dict(match_id=mid, home_goals=1, away_goals=3)
                for mid in ["jc:2042404", "jc:2042405"]
            ],
        )
    table(archive, "matches", rows)
    folder = root / "auxiliary"
    folder.mkdir()
    path = folder / "JPN.csv"
    payload = (FIXTURES / "t68/daily_gate/fd.csv").read_text()
    if problem == "score":
        payload = payload.replace(",1,3,A", ",9,3,H")
    elif problem == "side":
        payload = payload.replace("Kawasaki Frontale,FC Tokyo", "FC Tokyo,Kawasaki Frontale")
    elif problem == "clock":
        payload = payload.replace("12:30", "20:30")
    path.write_text(payload)
    save_json(
        path.with_suffix(".csv.meta.json"),
        dict(
            url="https://football-data.co.uk/new/JPN.csv",
            status_code=200,
            sha256=sha256(path.read_bytes()).hexdigest(),
            fetched_at_utc="2026-11-15T01:00:00Z"
            if problem == "future"
            else "2026-11-14T01:00:00Z",
        ),
    )
    before = gate.build_register(archive, CUTOFF, codes=["98"])["leagues"][0]
    assert before["matches"]["linked"] == 0
    assert before["matches"]["unresolved"][0]["reason"] in {
        "stored_relation_original_unverified",
        "non_unique_relation",
        "stored_current_jc_original_missing",
    }
    result = gate.build_register(archive, CUTOFF, codes=["98"], fd_raw_dir=folder)["leagues"][0]
    if problem:
        assert result["matches"]["linked"] == 0
    else:
        assert result["matches"]["linked_by_source"] == {"apif": 0, "fd": 1}
        row = result["matches"]["relations"][0]
        assert row["provenance_scope"] == "auxiliary_fd_originals"
        assert row["time_difference_seconds"] == [900]
        assert row["provenance"][0]["pointer"] == "/csv/line/2"
        assert row["proposal_entity_issues"] == ["linked_entity_evidence_conflict"]


@pytest.mark.parametrize("problem", [None, "stale", "future", "sha", "dual"])
def test_actual_gate_validates_explicit_result_source_link_snapshots(bundle, problem):
    from zucai.result_source_links import RESULT_LINK_COLUMNS, RESULT_LINK_FILE

    archive, root, _ = bundle
    fd_folder = None
    manifest = json.loads((archive / "feed_manifest.json").read_bytes())
    match = dict(
        zip(
            manifest["columns"]["matches"],
            json.loads((archive / "state/matches.jsonl").read_text()),
            strict=True,
        )
    )
    match.update(counterpart_id=None, kickoff_utc="2026-10-31T12:15:00.000000Z")
    paid = dict(match, match_id="apif:10", source="apif")
    if problem == "dual":
        match["counterpart_id"] = "fd:JPN:20261031:kawasaki-frontale:fc-tokyo"
        fd_folder = root / "auxiliary"
        fd_folder.mkdir()
        path = fd_folder / "JPN.csv"
        path.write_bytes((FIXTURES / "t68/daily_gate/fd.csv").read_bytes())
        save_json(
            path.with_suffix(".csv.meta.json"),
            dict(
                url="https://football-data.co.uk/new/JPN.csv",
                status_code=200,
                sha256=sha256(path.read_bytes()).hexdigest(),
                fetched_at_utc="2026-11-14T01:00:00Z",
            ),
        )
    table(archive, "matches", [match, paid])
    table(
        archive,
        "results",
        [dict(match_id=mid, home_goals=1, away_goals=3) for mid in ["jc:2042404", "apif:10"]],
    )
    link = dict(
        provider_match_id="apif:10",
        counterpart_id="jc:2042404",
        verified_utc="2026-11-14T01:00:00.000000Z",
        league="日职",
        home_team_id=1,
        away_team_id=2,
        provider_kickoff_utc=match["kickoff_utc"],
        counterpart_kickoff_utc=match["kickoff_utc"],
    )
    if problem == "stale":
        link["home_team_id"], link["away_team_id"] = 2, 1
    elif problem == "future":
        link["verified_utc"] = "2026-11-15T01:00:00.000000Z"
    path = archive / RESULT_LINK_FILE
    path.write_text(json.dumps([link[k] for k in RESULT_LINK_COLUMNS]) + "\n")
    manifest = json.loads((archive / "feed_manifest.json").read_bytes())
    manifest["result_source_links"] = dict(version=1, columns=list(RESULT_LINK_COLUMNS), count=1)
    save_json(archive / "feed_manifest.json", manifest)
    seal(archive)
    if problem == "sha":
        path.write_text(path.read_text() + "\n")
        with pytest.raises(ValueError, match="SHA|hash|哈希"):
            gate.build_register(archive, CUTOFF, codes=["98"])
        return
    result = gate.build_register(archive, CUTOFF, codes=["98"], fd_raw_dir=fd_folder)["leagues"][0]
    assert result["matches"]["apif_linked"] == (0 if problem in {"stale", "future"} else 1)
    if problem == "dual":
        assert result["matches"]["linked"] == 1
        assert result["matches"]["linked_by_source"] == {"apif": 1, "fd": 1}
    if not problem:
        assert (
            result["matches"]["relations"][0]["stored_evidence"]["result_source_link"]["pointer"]
            == RESULT_LINK_FILE + "#/line/1"
        )


def test_persistence_cannot_lose_an_audit_inventory_with_matching_core_counts(bundle):
    _, root, evidence = bundle
    path = root / "after/state/audit/apif_held_unbound.csv"
    path.write_text(path.read_text().splitlines()[0] + "\n")
    seal(root / "after")
    evidence["persistence"]["after_manifest"] = ref(root, root / "after/feed_manifest.json")
    change(
        root,
        evidence["persistence"]["replay_options"],
        lambda d: d.update(
            after_manifest_sha256=evidence["persistence"]["after_manifest"]["sha256"]
        ),
    )
    result = run(bundle)
    assert "persistence" in result["missing_evidence"]
    assert "state" in result["evidence"]["persistence"]["reason"]


def test_second_run_requires_real_scoped_counts(bundle):
    _, root, evidence = bundle
    change(
        root, evidence["persistence"]["repeat_report"], lambda d: d["scopes"][0].pop("unchanged")
    )
    result = run(bundle)
    assert "persistence" in result["missing_evidence"]
    assert "counts" in result["evidence"]["persistence"]["reason"]


def test_header_only_held_cannot_be_self_certified_by_a_tiny_manifest(bundle):
    _, root, evidence = bundle
    path = root / "empty-held.csv"
    path.write_text("raw_sha256,competition_code,league,provider_match_id\n")
    evidence["current_season_replay"]["held_before"] = ref(root, path)
    evidence["current_season_replay"]["held_before_manifest"] = document(
        root,
        "fake-held-manifest.json",
        {"files": {"empty-held.csv": sha256(path.read_bytes()).hexdigest()}},
    )
    result = run(bundle)
    assert "current_season_replay" in result["missing_evidence"]
    assert "held" in result["evidence"]["current_season_replay"]["reason"]


def fixture_rows(folder, name):
    manifest = json.loads((folder / "feed_manifest.json").read_bytes())
    return [
        dict(zip(manifest["columns"][name], json.loads(line), strict=True))
        for line in (folder / f"state/{name}.jsonl").read_text().splitlines()
    ]


def reseal_fixture(folder):
    manifest = json.loads((folder / "feed_manifest.json").read_bytes())
    with sqlite3.connect(":memory:") as db:
        for name, columns in TABLES.items():
            db.execute(f"CREATE TABLE {name} ({','.join(columns)})")
            for row in fixture_rows(folder, name):
                vals = [row[c] for c in columns]
                db.execute(
                    f"INSERT INTO {name} VALUES ({','.join('?' for _ in vals)})",
                    [json.dumps(v) if isinstance(v, list) else v for v in vals],
                )
        manifest["sources"] = source_inventory(db)
    save_json(folder / "feed_manifest.json", manifest)
    seal(folder)


def refresh_bundle_exports(bundle):
    archive, root, evidence = bundle
    reseal_fixture(archive)
    original = json.loads((archive / "feed_manifest.json").read_bytes())
    for name in ["preimport", "after", "restored"]:
        folder = root / name
        for path in (folder / "state").rglob("*"):
            if path.is_file():
                path.write_bytes((archive / path.relative_to(folder)).read_bytes())
        manifest = deepcopy(original)
        manifest["created_utc"] = json.loads((folder / "feed_manifest.json").read_bytes())[
            "created_utc"
        ]
        manifest["files"] = {p: h for p, h in manifest["files"].items() if (folder / p).exists()}
        save_json(folder / "feed_manifest.json", manifest)

    def refresh(value):
        if isinstance(value, dict):
            if set(value) == {"path", "sha256"}:
                value.update(ref(root, root / value["path"]))
            else:
                for item in value.values():
                    refresh(item)
        elif isinstance(value, list):
            for item in value:
                refresh(item)

    refresh(evidence)
    evidence["archive_sha256"] = ref(root, archive / "feed_manifest.json")["sha256"]
    for section in [evidence["current_season_replay"], evidence["persistence"]]:
        change(
            root,
            section["replay_options"],
            lambda d, section=section: d.update(
                before_manifest_sha256=section["before_manifest"]["sha256"],
                after_manifest_sha256=section["after_manifest"]["sha256"],
            ),
        )


def test_combined_legacy_and_explicit_edges_cannot_claim_two_jc_fixtures(bundle):
    from zucai.result_source_links import RESULT_LINK_COLUMNS, RESULT_LINK_FILE

    archive, _, _ = bundle
    path = archive / "state/catalogue/catalogue/jc/2026-10/2026-10-31-p1.json"
    page = json.loads(path.read_bytes())
    page["value"]["matchResult"].append(dict(page["value"]["matchResult"][0], matchId=2042405))
    page["value"]["total"] = 2
    raw(
        archive,
        path.relative_to(archive),
        page,
        json.loads(path.with_suffix(".json.meta.json").read_bytes())["url"],
    )
    head = json.loads((archive / "state/catalogue/heads/jc-heads/2042404.json").read_bytes())
    head["value"]["sportteryMatchId"] = 2042405
    raw(
        archive,
        "state/catalogue/heads/jc-heads/2042405.json",
        head,
        kickoffs.API + "getMatchHeadV1.qry?source=web&sportteryMatchId=2042405",
    )
    original = fixture_rows(archive, "matches")[0]
    original["kickoff_utc"] = "2026-10-31T12:15:00.000000Z"
    table(
        archive,
        "matches",
        [
            original,
            dict(original, match_id="jc:2042405", counterpart_id=None),
            dict(original, match_id="apif:10", source="apif", counterpart_id=None),
        ],
    )
    table(
        archive,
        "results",
        [
            dict(match_id=mid, home_goals=1, away_goals=3)
            for mid in ["jc:2042404", "jc:2042405", "apif:10"]
        ],
    )
    link = dict(
        provider_match_id="apif:10",
        counterpart_id="jc:2042405",
        verified_utc="2026-11-14T01:00:00.000000Z",
        league="日职",
        home_team_id=1,
        away_team_id=2,
        provider_kickoff_utc=original["kickoff_utc"],
        counterpart_kickoff_utc=original["kickoff_utc"],
    )
    (archive / RESULT_LINK_FILE).write_text(
        json.dumps([link[k] for k in RESULT_LINK_COLUMNS]) + "\n"
    )
    manifest = json.loads((archive / "feed_manifest.json").read_bytes())
    manifest["result_source_links"] = dict(version=1, columns=list(RESULT_LINK_COLUMNS), count=1)
    save_json(archive / "feed_manifest.json", manifest)
    seal(archive)
    result = gate.build_register(archive, CUTOFF, codes=["98"])["leagues"][0]
    assert result["matches"]["linked"] == 0, result["matches"]["relations"]
    assert "non_unique_relation" in result["blockers"]
    assert result["matches"]["relation_conflicts"][0]["members"] == [
        "apif:10",
        "jc:2042404",
        "jc:2042405",
    ]


@pytest.mark.parametrize("bundle", [True], indirect=True)
def test_historical_snapshot_conflict_remains_blocked_despite_explanations(bundle):
    archive, root, evidence = bundle
    path = archive / "state/catalogue/catalogue/jc/2026-06/2026-06-30-p1.json"
    page = json.loads(path.read_bytes())
    page["value"]["matchResult"][0].update(awayTeamId=901, awayTeam="Historical away")
    raw(
        archive,
        path.relative_to(archive),
        page,
        json.loads(path.with_suffix(".json.meta.json").read_bytes())["url"],
    )
    head_path = archive / "state/catalogue/heads/jc-heads/2042405.json"
    head = json.loads(head_path.read_bytes())
    head["value"]["sportteryAwayTeamId"] = 901
    raw(
        archive,
        head_path.relative_to(archive),
        head,
        json.loads(head_path.with_suffix(".json.meta.json").read_bytes())["url"],
    )
    table(
        archive,
        "teams",
        fixture_rows(archive, "teams") + [dict(id=4, name="Historical project away", aliases=[])],
    )
    matches = fixture_rows(archive, "matches")
    matches[1]["away_team_id"] = 4
    table(archive, "matches", matches)
    results = fixture_rows(archive, "results")
    results[1]["home_goals"] = 9
    table(archive, "results", results)
    refresh_bundle_exports(bundle)
    evidence["unbound_explanations"] = document(
        root,
        "unbound-conflicts.json",
        [
            dict(
                official_team_id=tid,
                reason_code="no_unique_approved_scoped_binding",
                reason="Historical only; cannot waive a contradiction.",
                evidence=ref(root, path),
            )
            for tid in ["900", "901"]
        ],
    )
    result = run(bundle)
    assert "official_identity_conflict" in result["blockers"], result
    assert result["status"] != "ready_for_review", result["evidence"]
    assert {r["official_team_id"] for r in result["teams"]["identity_conflicts"]} == {"900", "901"}


@pytest.mark.parametrize("bundle", [False, True], indirect=True)
def test_recent_jc_only_participants_cannot_use_historical_unbound_exception(bundle):
    archive, root, evidence = bundle
    historical = len(fixture_rows(archive, "teams")) == 3
    tid = 900 if historical else 777
    reference = evidence["recent_replay"]["official_originals"][4]
    path = root / reference["path"]
    page = json.loads(path.read_bytes())
    original = json.loads(
        (archive / "state/catalogue/catalogue/jc/2026-10/2026-10-31-p1.json").read_bytes()
    )["value"]["matchResult"][0]
    page["value"].update(
        matchResult=[dict(original, matchId=2042406, matchDate="2026-11-05", homeTeamId=tid)],
        total=1,
    )
    save_json(path, page)
    reference.update(ref(root, path))
    meta_path = path.with_suffix(".json.meta.json")
    meta = json.loads(meta_path.read_bytes())
    meta["sha256"] = reference["sha256"]
    save_json(meta_path, meta)
    change(
        root,
        evidence["recent_replay"]["results"],
        lambda d: d["official_match_ids"].append("jc:2042406"),
    )
    if historical:
        evidence["unbound_explanations"] = document(
            root,
            "unbound.json",
            [
                dict(
                    official_team_id="900",
                    reason_code="no_unique_approved_scoped_binding",
                    reason="Historical club without approval",
                    evidence=ref(
                        root, archive / "state/catalogue/catalogue/jc/2026-06/2026-06-30-p1.json"
                    ),
                )
            ],
        )
    result = run(bundle)
    assert result["teams"]["total"] == (3 if historical else 2)
    assert "current_official_participants_unbound" in result["blockers"], result
    assert result["current_official_participants"]["unbound"][0]["official_team_id"] == str(tid)
    assert result["status"] == "blocked"


def importer_bundle(bundle, *, retain_results=False, supplemental=False):
    """Actual offline importer and repeat; only collector envelope is assembled here."""
    from datetime import datetime

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from zucai.models import Base
    from zucai.result_source_identity import export_result_identities
    from zucai.result_source_links import export_result_links
    from zucai.sources.api_football import ApiFootball
    from zucai.sources.apif_ingest import import_apif_games

    archive, root, evidence = bundle
    path = archive / "state/catalogue/backfill/backfill/api-football/day/fixtures_one.json"
    if supplemental:
        doc = json.loads(path.read_bytes())
        extra = deepcopy(doc["response"][0])
        when = datetime.fromisoformat("2026-10-20T12:15:00+00:00")
        extra["fixture"].update(id=11, date=when.isoformat(), timestamp=int(when.timestamp()))
        doc["response"].append(extra)
        doc["results"] = 2
        raw(
            archive,
            path.relative_to(archive),
            doc,
            "https://v3.football.api-sports.io/fixtures?league=98&season=2027",
        )
        for reference in evidence["recent_replay"]["originals"]:
            daily_path = root / reference["path"]
            daily_doc = json.loads(daily_path.read_bytes())
            if daily_doc["parameters"]["date"] == "2026-10-20":
                daily_doc.update(response=[extra], results=1)
                save_json(daily_path, daily_doc)
                reference.update(ref(root, daily_path))
                sidecar = daily_path.with_suffix(".json.meta.json")
                meta = json.loads(sidecar.read_bytes())
                meta["sha256"] = reference["sha256"]
                save_json(sidecar, meta)
    parser = ApiFootball(leagues={"98": {"league": "日职"}})
    try:
        games = parser.parse(path.read_bytes(), league_id="98", season=2027)
    finally:
        parser.session.close()
    assert len(games) == (2 if supplemental else 1)
    source_hash = sha256(path.read_bytes()).hexdigest()
    aliases = json.loads(reader.FORMAL.read_bytes())
    seen = datetime.fromisoformat(
        json.loads(path.with_suffix(".json.meta.json").read_bytes())["fetched_at_utc"]
    )
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        for name in TABLES:
            rows = fixture_rows(archive, name)
            for row in rows:
                for column in Base.metadata.tables[name].columns:
                    if str(column.type) == "JSON" and isinstance(row.get(column.name), str):
                        row[column.name] = json.loads(row[column.name])
            if rows:
                connection.execute(Base.metadata.tables[name].insert(), rows)
    connection = engine.raw_connection()

    def save_state(folder, created):
        manifest = json.loads((folder / "feed_manifest.json").read_bytes())
        manifest["created_utc"] = created
        for name, columns in TABLES.items():
            rows = connection.execute(
                f"SELECT {','.join(columns)} FROM {name} ORDER BY {columns[0]}"
            ).fetchall()
            (folder / f"state/{name}.jsonl").write_text(
                "".join(
                    json.dumps(list(r), ensure_ascii=False, separators=(",", ":")) + "\n"
                    for r in rows
                )
            )
            manifest["counts"][name] = len(rows)
        manifest["sources"] = source_inventory(connection)
        for key, fn in [
            ("result_source_links", export_result_links),
            ("result_source_identities", export_result_identities),
        ]:
            block = fn(connection, folder)
            if block:
                manifest[key] = block
        save_json(folder / "feed_manifest.json", manifest)
        seal(folder)
        return ref(root, folder / "feed_manifest.json")

    pre = save_state(root / "preimport", "2026-11-14T01:05:00Z")
    reports = []
    for suffix, imported, target, created in [
        ("first", "2026-11-14T01:15:00Z", archive, "2026-11-14T01:20:00Z"),
        ("repeat", "2026-11-14T01:22:00Z", root / "after", "2026-11-14T01:25:00Z"),
    ]:
        with Session(engine) as session:
            result = import_apif_games(
                session,
                games,
                seen=seen,
                assume=timedelta(hours=3),
                aliases=aliases,
                retain_results=retain_results,
                imported_utc=datetime.fromisoformat(imported),
            )
            session.commit()
        assert not result["rejected"] and not result["suspected"] and not result["held"], result
        after = save_state(target, created)
        counts = {key: result[key] for key in COUNT_KEYS}
        run_id = "synthetic-importer-" + suffix
        report = dict(
            counts,
            status="complete",
            processed_files=1,
            run_id=run_id,
            scopes=[
                dict(
                    counts,
                    code="98",
                    season=2027,
                    status="complete",
                    parsed_matches=len(games),
                    raw={"sha256": source_hash},
                )
            ],
            files=[dict(result, raw_sha256=source_hash, seen_utc=seen.isoformat())],
        )
        invocation = dict(
            run_id=run_id,
            provider_code="98",
            season=2027,
            retain_results=retain_results,
            assume_known_after_seconds=10800,
            imported_utc=imported,
            source_sha256=source_hash,
            provenance="Synthetic offline import_apif_games invocation; not production evidence.",
        )
        invocation_ref = document(root, "importer-" + suffix + "-invocation.json", invocation)
        options = dict(
            invocation,
            cutoff_utc=CUTOFF,
            before_manifest_sha256=pre["sha256"],
            after_manifest_sha256=after["sha256"],
            invocation_evidence=invocation_ref,
            formal_aliases_sha256=sha256(reader.FORMAL.read_bytes()).hexdigest(),
        )
        reports.append(
            (
                document(root, "importer-" + suffix + "-report.json", report),
                document(root, "importer-" + suffix + "-options.json", options),
                result,
            )
        )
        pre = after
    restored = save_state(root / "restored", "2026-11-14T01:30:00Z")
    connection.close()
    engine.dispose()
    evidence["archive_sha256"] = ref(root, archive / "feed_manifest.json")["sha256"]
    current = evidence["current_season_replay"]
    current.update(
        report=reports[0][0],
        replay_options=reports[0][1],
        before_manifest=ref(root, root / "preimport/feed_manifest.json"),
        after_manifest=ref(root, archive / "feed_manifest.json"),
        held_before_manifest=ref(root, archive / "feed_manifest.json"),
    )
    persistence = evidence["persistence"]
    persistence.update(
        repeat_report=reports[1][0],
        replay_options=reports[1][1],
        before_manifest=ref(root, archive / "feed_manifest.json"),
        after_manifest=ref(root, root / "after/feed_manifest.json"),
        restored_manifest=restored,
    )
    for key in ["before_repeat", "after_repeat", "restored"]:
        state = json.loads((root / persistence[key]["path"]).read_bytes())
        if retain_results or supplemental:
            state["coverage"]["日职"]["apif"] = len(games) if retain_results else 1

        persistence[key] = document(root, key + ".json", state)
    recent = evidence["recent_replay"]
    recent.update(
        before_manifest=current["before_manifest"], after_manifest=current["after_manifest"]
    )
    results = json.loads((root / recent["results"]["path"]).read_bytes())
    results.update(
        fixture_ids=["apif:10", "apif:11"] if supplemental else ["apif:10"],
        raw_sha256=[r["sha256"] for r in recent["originals"]],
        inserted=reports[0][2]["inserted"],
        outcomes=[dict(fixture_id="apif:10", outcome="inserted" if retain_results else "unchanged")]
        + ([dict(fixture_id="apif:11", outcome="inserted")] if supplemental else []),
    )
    recent["results"] = document(root, "recent.json", results)
    recent_importer_bundle(bundle, retain_results=retain_results)
    return reports


@pytest.mark.parametrize("retain,supplemental", [(False, False), (True, False), (False, True)])
def test_genuine_importer_current_and_repeat_reports_reach_actual_entry(
    bundle, retain, supplemental
):
    reports = importer_bundle(bundle, retain_results=retain, supplemental=supplemental)
    first, repeat = reports[0][2], reports[1][2]
    if retain:
        assert first["inserted"] == 1 and first["linked_existing"] == 0
        assert repeat["unchanged"] == 1 and repeat["inserted"] == 0
    else:
        assert first["linked_existing"] == repeat["linked_existing"] == 1
        assert first["unchanged"] == 0
    if supplemental:
        assert first["inserted"] == 1 and first["assumed"] == 1
        assert repeat["unchanged"] == 1 and repeat["assumed"] == 0
    result = run(bundle)
    assert result["status"] == "ready_for_review", result["evidence"]


@pytest.mark.parametrize("counter", ["updated", "linked_existing", "assumed"])
@pytest.mark.parametrize("section", ["current_season_replay", "persistence"])
def test_current_scope_cannot_forge_other_importer_counts(bundle, counter, section):
    _, root, evidence = bundle
    change(
        root,
        evidence[section]["report" if section == "current_season_replay" else "repeat_report"],
        lambda d: d["scopes"][0].update({counter: 999}),
    )
    result = run(bundle)
    assert section in result["missing_evidence"]
    assert counter in result["evidence"][section]["reason"]


@pytest.mark.parametrize(
    "problem",
    [
        "missing",
        "wrong_run",
        "wrong_export",
        "naive_time",
        "pre_capture",
        "after_cutoff",
        "unrecorded_options",
    ],
)
def test_importer_replay_requires_recorded_options_and_actual_clock(bundle, problem):
    _, root, evidence = bundle
    section = evidence["current_season_replay"]
    if problem == "missing":
        section.pop("replay_options")
    else:
        options = json.loads((root / section["replay_options"]["path"]).read_bytes())
        if problem == "wrong_run":
            options["run_id"] = "unrelated"
        elif problem == "wrong_export":
            options["before_manifest_sha256"] = "0" * 64
        elif problem == "unrecorded_options":
            options["retain_results"] = True
        else:
            options["imported_utc"] = {
                "naive_time": "2026-11-14T01:15:00",
                "pre_capture": "2026-11-14T00:59:00Z",
                "after_cutoff": "2026-11-14T17:00:00Z",
            }[problem]
            change(
                root,
                options["invocation_evidence"],
                lambda d: d.update(imported_utc=options["imported_utc"]),
            )
        section["replay_options"] = document(root, "tampered-options.json", options)
    result = run(bundle)
    assert "current_season_replay" in result["missing_evidence"]
    assert any(
        word in result["evidence"]["current_season_replay"]["reason"]
        for word in ["replay_options", "imported_utc", "timezone"]
    )


def recent_importer_bundle(bundle, *, retain_results=False):
    """Separate, actual daily replay with its real capture/import clocks."""
    from datetime import datetime

    from sqlalchemy.orm import Session

    from zucai.identity_gate_evidence import export, restored_memory
    from zucai.result_source_identity import export_result_identities
    from zucai.result_source_links import export_result_links
    from zucai.sources.api_football import ApiFootball
    from zucai.sources.apif_ingest import import_apif_games

    _, root, evidence = bundle
    recent = evidence["recent_replay"]
    before = export(root, recent["before_manifest"])
    folder = root / "daily-after"
    shutil.copytree(before["folder"], folder, dirs_exist_ok=True)
    with restored_memory(before) as (engine, connection):
        for original in recent["originals"]:
            path = root / original["path"]
            meta = json.loads(path.with_suffix(".json.meta.json").read_bytes())
            parser = ApiFootball(leagues={"98": {"league": "日职"}})
            try:
                games = parser.parse(path.read_bytes())
            finally:
                parser.session.close()
            with Session(engine) as session:
                import_apif_games(
                    session,
                    games,
                    seen=datetime.fromisoformat(meta["fetched_at_utc"]),
                    assume=timedelta(hours=3),
                    aliases=json.loads(reader.FORMAL.read_bytes()),
                    retain_results=retain_results,
                    imported_utc=datetime.fromisoformat(CUTOFF),
                )
                session.commit()
        manifest = json.loads((folder / "feed_manifest.json").read_bytes())
        manifest["created_utc"] = CUTOFF
        for name, columns in TABLES.items():
            rows = connection.execute(
                f"SELECT {','.join(columns)} FROM {name} ORDER BY {columns[0]}"
            ).fetchall()
            (folder / f"state/{name}.jsonl").write_text(
                "".join(json.dumps(list(r), ensure_ascii=False) + "\n" for r in rows)
            )
            manifest["counts"][name] = len(rows)
        manifest["sources"] = source_inventory(connection)
        for key, fn in [
            ("result_source_links", export_result_links),
            ("result_source_identities", export_result_identities),
        ]:
            block = fn(connection, folder)
            if block:
                manifest[key] = block
        save_json(folder / "feed_manifest.json", manifest)
        seal(folder)
    recent["after_manifest"] = ref(root, folder / "feed_manifest.json")
    recent["replay_options"] = document(
        root,
        "recent-options.json",
        dict(
            provider_code="98",
            imported_utc=CUTOFF,
            retain_results=retain_results,
            assume_known_after_seconds=10800,
            formal_aliases_sha256=sha256(reader.FORMAL.read_bytes()).hexdigest(),
            before_manifest_sha256=recent["before_manifest"]["sha256"],
            after_manifest_sha256=recent["after_manifest"]["sha256"],
        ),
    )


def _later_fixture_archive(bundle, *, conflicting_page=False):
    """A stored current-season JC fixture later than the catalogue, read from the archive."""
    archive, _, _ = bundle
    stored = fixture_rows(archive, "matches")[0]
    later = dict(
        stored,
        match_id="jc:9900187",
        kickoff_utc="2026-11-06T12:15:00+00:00",
        counterpart_id="apif:11",
    )
    table(archive, "matches", [stored, later])
    result = fixture_rows(archive, "results")[0]
    table(archive, "results", [result, dict(result, match_id="jc:9900187")])
    apath = archive / "state/catalogue/backfill/backfill/api-football/day/fixtures_one.json"
    apif = json.loads(apath.read_bytes())
    second = deepcopy(apif["response"][0])
    clock = "2026-11-06T12:15:00+00:00"
    second["fixture"].update(
        id=11, date=clock, timestamp=int(datetime.fromisoformat(clock).timestamp())
    )
    apif["response"].append(second)
    apif["results"] = 2
    raw(
        archive,
        apath.relative_to(archive),
        apif,
        "https://v3.football.api-sports.io/fixtures?league=98&season=2027",
    )
    (archive / "state/audit/apif_held_unbound.csv").write_text(
        "raw_sha256,competition_code,league,provider_match_id\n"
        + sha256(apath.read_bytes()).hexdigest()
        + ",98,日职,10\n"
    )
    page = json.loads(
        (archive / "state/catalogue/catalogue/jc/2026-10/2026-10-31-p1.json").read_bytes()
    )
    for day in ["2026-11-06"] + (["2026-11-07"] if conflicting_page else []):
        document = deepcopy(page)
        document["value"]["matchResult"][0].update(matchId=9900187, matchDate=day)
        raw(
            archive,
            f"state/catalogue/supplement/jc/2026-11/{day}-p1.json",
            document,
            kickoffs.API + f"getUniformMatchResultV1.qry?matchBeginDate={day}&matchEndDate={day}"
            "&leagueId=&pageSize=30&pageNo=1&isFix=0&matchPage=1&pcOrWap=1",
        )
    reseal_fixture(archive)
    loaded = reader.load_archive(archive, leagues=["98"])
    register = gate.build_register(archive, CUTOFF, codes=["98"])["leagues"][0]
    return loaded, register["matches"], register["blockers"]


def test_later_official_page_read_from_the_archive_links_the_stored_fixture(bundle):
    loaded, matches, blockers = _later_fixture_archive(bundle)
    assert [r["match_id"] for r in loaded["later_official"]] == ["jc:9900187"]
    assert "jc:9900187" not in loaded["later_official_conflicts"]
    assert (matches["total"], matches["linked"], matches["unresolved"]) == (2, 2, [])
    assert "current_season_fixtures_unresolved" not in blockers


def test_contradicting_page_of_the_same_fixture_keeps_it_unresolved(bundle):
    """Reviewer finding on PR 187: a second page dated differently must not be dropped silently."""
    loaded, matches, blockers = _later_fixture_archive(bundle, conflicting_page=True)
    assert loaded["later_official"] == []
    assert loaded["later_official_conflicts"]["jc:9900187"] == ["stored_official_date_conflict"]
    assert matches["linked"] == 1
    assert matches["unresolved"] == [
        dict(
            jc_id="jc:9900187",
            provider_match_id=None,
            reason="stored_current_jc_original_conflict",
            conflicts=["stored_official_date_conflict"],
        )
    ]
    assert "current_season_fixtures_unresolved" in blockers
