"""Real gate entry regressions; all sports inputs originate in offline fixtures."""

import json
import shutil
import sqlite3
from copy import deepcopy
from datetime import date, timedelta
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
            matchDate="2026-09-30",
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
                    kickoff_utc="2026-09-30T12:15:00Z",
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
            sportteryMatchId=2042405, sportteryHomeTeamId=900, matchDateTime="2026-09-30 20:15"
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
            if historical and day == date(2026, 9, 30)
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
    return archive, root, evidence


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
        reason="Historical club has no approved API binding; remains unbound.",
        evidence=ref(root, archive / "state/catalogue/catalogue/jc/2026-09/2026-09-30-p1.json"),
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
                evidence=ref(root, archive / "state/teams.jsonl"),
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
