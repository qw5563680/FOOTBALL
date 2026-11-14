"""T68 candidate-only identity proof and explicit runtime target-scope boundaries."""

import importlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parents[1] / "data/fixtures/t68/identity/strict-pair.json"
NOW = "2026-11-14T15:00:00Z"


@pytest.fixture
def mod():
    try:
        return importlib.import_module("zucai.identity_binding")
    except ModuleNotFoundError:
        return None


@pytest.fixture
def inputs():
    return json.loads(FIXTURE.read_text())


def propose(mod, inputs, **kwargs):
    assert mod is not None, "T68 strict candidate-only validator is not implemented"
    return mod.propose_bindings(
        inputs["apif"], inputs["official"], inputs["teams"], observed_utc=NOW, **kwargs
    )


def ready(result):
    return [
        entry
        for entries in result["apif"].values()
        for entry in entries.values()
        if entry.get("evidence_status") == "strict_verified"
    ]


def test_strict_pair_is_candidate_only_with_inspectable_evidence(mod, inputs):
    result = propose(mod, inputs)
    entries = ready(result)
    assert len(entries) == 2
    assert {e["canonical"] for e in entries} == {"甲队", "乙队"}
    assert all(e["status"] == "proposed" for e in entries)
    assert all(e["layer"] == 1 and e["depends_on"] == [] for e in entries)
    evidence = entries[0]["evidence"][0]
    assert evidence["apif_candidates"] == ["apif:10"]
    assert evidence["official_candidates"] == ["jc:20"]
    assert evidence["apif"]["provenance"][0]["sha256"] == "a" * 64
    assert entries[0]["binding_scope"]["leagues"] == ["测试联赛"]
    from zucai.sources.fdorg_verify import _active

    assert _active(result["apif"], "测试联赛") == {}


@pytest.mark.parametrize(
    ("source", "field", "value", "reason"),
    [
        ("apif", "kickoff_utc", "2020-06-01T12:00:01Z", "kickoff_not_exact"),
        ("official", "kickoff_utc", None, "missing_kickoff_evidence"),
        ("official", "time_precision", "date", "missing_kickoff_evidence"),
        ("apif", "score", [3, 1], "regular_time_score_mismatch"),
        ("official", "score", None, "missing_regular_time_score"),
        ("apif", "regular_time_basis", "missing", "missing_regular_time_score"),
        ("apif", "status", "AWD", "not_finished_regular_time"),
        ("apif", "status", "cancelled", "not_finished_regular_time"),
        ("apif", "complete", False, "incomplete_competitor_window"),
        ("official", "complete", False, "incomplete_competitor_window"),
        ("apif", "home_category", None, "category_evidence_missing"),
        ("apif", "home_category", "club/women/senior/first", "category_conflict"),
        ("apif", "home_category", "club/men/senior/reserve", "category_conflict"),
        ("apif", "home_category", "national/men/U21/standard", "category_conflict"),
        ("apif", "home_category", "national/men/U23/olympic", "category_conflict"),
        ("apif", "home_category", "national/men/U23/asian_games", "category_conflict"),
    ],
)
def test_rejects_incomplete_or_incompatible_proof(mod, inputs, source, field, value, reason):
    inputs[source][0][field] = value
    result = propose(mod, inputs)
    assert not ready(result)
    assert any(reason in row["reasons"] for row in result["unverified"])


def test_unplayed_rejected_competitor_prevents_score_disambiguation(mod, inputs):
    inputs["apif"].append(dict(inputs["apif"][0], match_id="apif:11", status="NS", score=None))
    result = propose(mod, inputs)
    assert not ready(result)
    assert any("not_bidirectionally_unique" in row["reasons"] for row in result["unverified"])


def test_reverse_competitors_and_exact_three_hour_boundary_are_retained(mod, inputs):
    inputs["official"].append(
        dict(
            inputs["official"][0],
            match_id="jc:21",
            kickoff_utc="2020-06-01T15:00:00Z",
            score=[0, 0],
        )
    )
    assert not ready(propose(mod, inputs))


def test_formal_anchor_does_not_hide_independent_reverse_conflict(mod, inputs):
    formal = {
        "测试联赛": {"101": {"canonical": "乙队", "canonical_team_id": "2", "status": "approved"}}
    }
    result = propose(mod, inputs, formal=formal)
    assert not ready(result)
    assert any("global_provider_id_conflict" in row["reasons"] for row in result["unverified"])


def test_existing_global_id_conflict_outside_target_scope_blocks(mod, inputs):
    formal = {
        "另一赛事": {
            "101": {
                "canonical": "丙队",
                "status": "auto_verified",
                "binding_scope": {"leagues": ["另一赛事"], "category": "club/men/senior/first"},
            }
        }
    }
    assert not ready(propose(mod, inputs, formal=formal))


def test_duplicate_raw_same_fixture_conflict_is_not_latest_wins(mod, inputs):
    inputs["apif"].append(dict(inputs["apif"][0], score=[5, 4]))
    result = propose(mod, inputs)
    assert not ready(result)
    assert any("conflicting_fixture_originals" in row["reasons"] for row in result["unverified"])


def test_duplicate_canonical_provider_ids_block_whole_witness(mod, inputs):
    inputs["apif"].append(
        dict(
            inputs["apif"][0],
            match_id="apif:11",
            home="103",
            away="104",
            kickoff_utc="2020-06-02T12:00:00Z",
        )
    )
    inputs["official"].append(
        dict(inputs["official"][0], match_id="jc:21", kickoff_utc="2020-06-02T12:00:00Z")
    )
    assert not ready(propose(mod, inputs))


def test_fd_early_year_evidence_uses_same_strict_validator(mod, inputs):
    inputs["official"][0]["match_id"] = "fd:JPN:20200601:alpha:beta"
    assert len(ready(propose(mod, inputs))) == 2


def layered(inputs):
    # The first independent game establishes 101/102. Only the next round can
    # remove the incompatible 101/104 competitor from the second game's window.
    for key, value in (("3", "丙队"), ("4", "丁队")):
        inputs["teams"][key] = {"name": value, "category": "club/men/senior/first"}
    base = inputs["apif"][0]
    inputs["apif"] += [
        dict(base, match_id="apif:11", home="103", away="104", kickoff_utc="2020-06-02T12:00:00Z"),
        dict(
            base,
            match_id="apif:12",
            home="101",
            away="104",
            kickoff_utc="2020-06-02T12:00:00Z",
            status="NS",
            score=None,
        ),
    ]
    inputs["official"].append(
        dict(
            inputs["official"][0],
            match_id="jc:21",
            home="3",
            away="4",
            kickoff_utc="2020-06-02T12:00:00Z",
        )
    )
    return inputs


def test_layers_depend_on_previous_round_and_reject_cascades(mod, inputs):
    result = propose(mod, layered(inputs))
    entries = result["apif"]["测试联赛"]
    assert entries["103"]["layer"] == 2
    assert entries["101"]["candidate_id"] in entries["103"]["depends_on"]
    rejected = propose(mod, inputs, reject=[entries["101"]["candidate_id"]])
    assert rejected["apif"]["测试联赛"]["103"]["evidence_status"] == "withdrawn_dependency"
    assert all(e["canonical_team_id"] not in {"3", "4"} for e in ready(rejected))


def test_previous_candidates_preserve_first_observation_and_zero_new(mod, inputs):
    first = propose(mod, inputs)
    second = mod.propose_bindings(
        inputs["apif"],
        inputs["official"],
        inputs["teams"],
        observed_utc="2026-11-15T00:00:00Z",
        previous=first["apif"],
    )
    assert second["summary"]["new_candidates"] == 0
    assert second["apif"] == first["apif"]


def test_previous_conflicting_candidate_is_preserved_and_blocks(mod, inputs):
    first = propose(mod, inputs)
    previous = deepcopy(first["apif"])
    previous["测试联赛"]["101"]["canonical_team_id"] = "3"
    previous["测试联赛"]["101"]["canonical"] = "丙队"
    result = propose(mod, inputs, previous=previous)
    assert not ready(result)
    assert result["apif"]["测试联赛"]["101"]["conflicting_candidates"]


def test_scope_guard_legacy_and_explicit_target_boundary():
    from zucai.sources import apif_aliases

    assert hasattr(apif_aliases, "binding_scope_allows"), "T68 explicit target guard is missing"
    guard = apif_aliases.binding_scope_allows
    assert guard({"status": "auto_verified"}, "任意")
    entry = {"binding_scope": {"leagues": ["杯赛"], "category": "club/men/senior/first"}}
    assert guard(entry, "杯赛")
    assert not guard(entry, "青年")


def test_scoped_propagation_cannot_activate_another_league():
    from zucai.alias_propagation import _plan

    aliases = {
        "原联赛": {
            "101": {
                "canonical": "甲队",
                "status": "auto_verified",
                "binding_scope": {"leagues": ["原联赛"], "category": "club/men/senior/first"},
            }
        }
    }
    updated, changes = _plan(aliases, "apif", {"101": {"新杯赛"}}, "a" * 64)
    assert updated == aliases
    assert changes == []


def test_unknown_canonical_competitor_is_not_filtered_by_formal_anchor(mod, inputs):
    inputs["official"].append(
        dict(inputs["official"][0], match_id="jc:21", home="unresolved:201", away="unresolved:202")
    )
    formal = {"测试联赛": {"101": {"canonical": "甲队", "status": "auto_verified"}}}
    assert not ready(propose(mod, inputs, formal=formal))


def test_same_provider_requires_explicit_category_and_approval_evidence(mod, inputs):
    assert hasattr(mod, "same_provider_proposals"), "typed same-provider proposal path is missing"
    formal = {
        "来源联赛": {
            "101": {
                "canonical": "甲队",
                "status": "auto_verified",
                "category": "club/men/senior/first",
                "identity_country": "England",
                "evidence": [{"raw_sha256": "c" * 64}],
            }
        }
    }
    inputs["apif"][0]["home_country"] = "England"
    result = mod.same_provider_proposals(inputs["apif"], inputs["teams"], formal, "d" * 64, NOW)
    assert result["测试联赛"]["101"]["status"] == "proposed"
    formal["来源联赛"]["101"].pop("category")
    assert mod.same_provider_proposals(inputs["apif"], inputs["teams"], formal, "d" * 64, NOW) == {}


def test_reconcile_scope_does_not_hide_global_conflicting_formal_id(tmp_path):
    from zucai.db import get_engine, init_db, session_scope
    from zucai.models import Team
    from zucai.sources.apif_reconcile import _formal_entry

    engine = get_engine(tmp_path / "test.db")
    init_db(engine)
    with session_scope(engine) as session:
        session.add_all([Team(name="甲队", aliases=[]), Team(name="乙队", aliases=[])])
        session.flush()
        one = {
            "canonical": "甲队",
            "status": "auto_verified",
            "binding_scope": {"leagues": ["来源"], "category": "club/men/senior/first"},
        }
        entry, origins = _formal_entry(session, {"来源": {"101": one}}, "目标", "101")
        assert entry is None
        with pytest.raises(ValueError, match="冲突"):
            _formal_entry(
                session,
                {
                    "来源": {"101": one},
                    "其他": {"101": {"canonical": "乙队", "status": "auto_verified"}},
                },
                "目标",
                "101",
            )
    engine.dispose()


def test_scope_date_range_is_checked_per_match():
    from zucai.sources.apif_aliases import binding_scope_allows

    entry = {
        "binding_scope": {
            "leagues": ["杯赛"],
            "category": "club/men/senior/first",
            "start_utc": "2020-01-01T00:00:00Z",
            "end_utc": "2020-12-31T23:59:59Z",
        }
    }
    assert binding_scope_allows(entry, "杯赛", kickoff_utc="2020-06-01T00:00:00Z")
    assert not binding_scope_allows(entry, "杯赛", kickoff_utc="2021-01-01T00:00:00Z")


@pytest.fixture
def adapter():
    try:
        return importlib.import_module("zucai.identity_binding_archive")
    except ModuleNotFoundError:
        return None


def raw_page(tmp_path, rows=None):
    import hashlib

    document = json.loads((FIXTURE.parent / "apif-page.json").read_text())
    if rows is not None:
        document["response"] = rows
        document["results"] = len(rows)
    path = tmp_path / "state/catalogue/run/backfill/api-football/day/fixtures_one.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(document).encode()
    path.write_bytes(raw)
    path.with_suffix(".json.meta.json").write_text(
        json.dumps(
            {
                "url": "https://v3.football.api-sports.io/fixtures?league=98&season=2020",
                "status_code": 200,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "fetched_at_utc": "2020-06-02T00:00:00Z",
            }
        )
    )
    return path


def test_archive_retains_cancelled_and_rejected_competitors(adapter, tmp_path):
    assert adapter is not None, "source-verifying archive adapter is missing"
    path = raw_page(tmp_path)
    doc = json.loads(path.read_text())
    extra = deepcopy(doc["response"][0])
    extra["fixture"]["id"] = 11
    extra["fixture"]["status"]["short"] = "AWD"
    raw_page(tmp_path, [doc["response"][0], extra])
    rows, issues, summary = adapter.load_apif(tmp_path, {"98": {"league": "测试联赛"}})
    assert {row["match_id"] for row in rows} == {"apif:10", "apif:11"}
    assert rows[1]["score"] is None
    assert summary["raw_competitor_rows"] == 2


@pytest.mark.parametrize("problem", ["sha", "http", "url", "missing_page", "timestamp"])
def test_bad_apif_archive_never_claims_complete(adapter, tmp_path, problem):
    assert adapter is not None, "source-verifying archive adapter is missing"
    import hashlib

    path = raw_page(tmp_path)
    meta_path = path.with_suffix(".json.meta.json")
    meta, doc = json.loads(meta_path.read_text()), json.loads(path.read_text())
    if problem == "sha":
        meta["sha256"] = "0" * 64
    elif problem == "http":
        meta["status_code"] = 500
    elif problem == "url":
        meta["url"] = meta["url"].replace("season=2020", "season=2019")
    elif problem == "missing_page":
        doc["paging"]["total"] = 2
    else:
        doc["response"][0]["fixture"]["timestamp"] += 60
    if problem in {"missing_page", "timestamp"}:
        path.write_text(json.dumps(doc))
        meta["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    meta_path.write_text(json.dumps(meta))
    rows, issues, _ = adapter.load_apif(tmp_path, {"98": {"league": "测试联赛"}})
    assert issues
    assert not rows or not all(row["complete"] for row in rows)


def test_database_occupancy_cannot_authorize_a_matching_anchor(mod, inputs):
    inputs["official"].append(dict(inputs["official"][0], match_id="jc:21", home="3", away="4"))
    result = propose(mod, inputs, occupied={"101": {"1"}, "102": {"2"}})
    assert not ready(result)
    assert any("not_bidirectionally_unique" in row["reasons"] for row in result["unverified"])


def test_unrelated_missing_clock_date_does_not_block_verified_window(mod, inputs):
    inputs["official"].append(
        dict(inputs["official"][0], match_id="jc:21", kickoff_utc=None, match_date="2019-01-01")
    )
    assert len(ready(propose(mod, inputs))) == 2


def test_unknown_clock_on_adjacent_beijing_day_blocks_cross_midnight(mod, inputs):
    inputs["apif"][0]["kickoff_utc"] = "2020-06-01T15:30:00Z"
    inputs["official"][0]["kickoff_utc"] = "2020-06-01T15:30:00Z"
    inputs["official"].append(
        dict(inputs["official"][0], match_id="jc:21", kickoff_utc=None, match_date="2020-06-02")
    )
    assert not ready(propose(mod, inputs))


def test_rejected_proposal_does_not_reactivate_on_next_replay(mod, inputs):
    first = propose(mod, inputs)
    rejected = propose(mod, inputs, reject=[first["apif"]["测试联赛"]["101"]["candidate_id"]])
    third = propose(mod, inputs, previous=rejected["apif"])
    assert not ready(third)


def test_diagnostic_matches_have_no_approval_eligibility(mod, inputs):
    inputs["apif"][0]["home_category"] = None
    inputs["teams"]["1"]["category"] = None
    result = propose(mod, inputs, diagnostic=True, use_formal_anchors=False)
    assert not ready(result)
    assert result["apif"]["测试联赛"]["101"]["evidence_status"] == "diagnostic_only"


def test_diagnostic_does_not_use_formal_anchors_when_reproducing_known_answers(mod, inputs):
    inputs["official"].append(dict(inputs["official"][0], match_id="jc:21", home="3", away="4"))
    formal = {"测试联赛": {"101": {"canonical": "甲队", "status": "auto_verified"}}}
    result = propose(mod, inputs, diagnostic=True, use_formal_anchors=False, formal=formal)
    assert result["apif"] == {}


def test_missing_page_from_another_run_cannot_complete_capture(adapter, tmp_path):
    assert adapter is not None
    import hashlib
    import shutil

    path = raw_page(tmp_path)
    doc = json.loads(path.read_text())
    doc["paging"]["total"] = 2
    path.write_text(json.dumps(doc))
    meta = json.loads(path.with_suffix(".json.meta.json").read_text())
    meta["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_suffix(".json.meta.json").write_text(json.dumps(meta))
    second = tmp_path / "state/catalogue/different-run/backfill/api-football/day/fixtures_two.json"
    second.parent.mkdir(parents=True)
    shutil.copyfile(path, second)
    doc["paging"]["current"] = 2
    doc["parameters"]["page"] = "2"
    second.write_text(json.dumps(doc))
    meta["url"] += "&page=2"
    meta["sha256"] = hashlib.sha256(second.read_bytes()).hexdigest()
    second.with_suffix(".json.meta.json").write_text(json.dumps(meta))
    rows, _, summary = adapter.load_apif(tmp_path, {"98": {"league": "测试联赛"}})
    assert all(not row["complete"] for row in rows)
    assert summary["complete_scopes"] == 0


def test_duplicate_canonical_name_requires_unique_project_target(mod, inputs):
    inputs["teams"]["5"] = dict(inputs["teams"]["1"])
    assert not ready(propose(mod, inputs))


@pytest.mark.parametrize("problem", ["columns", "duplicate"])
def test_archive_tables_require_fixed_schema_and_unique_keys(adapter, tmp_path, problem):
    import hashlib

    from zucai.cloud_feeds import TABLES

    columns = {name: list(TABLES[name]) for name in ("teams", "matches", "results")}
    rows = {"teams": [[1, "甲队", "[]"]], "matches": [], "results": []}
    if problem == "columns":
        columns["teams"] = ["name", "id", "aliases"]
    else:
        rows["teams"] *= 2
    manifest = {"columns": columns, "counts": {}, "files": {}}
    for name, values in rows.items():
        path = tmp_path / f"state/{name}.jsonl"
        path.parent.mkdir(exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in values))
        manifest["files"][f"state/{name}.jsonl"] = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest["counts"][name] = len(values)
    (tmp_path / "feed_manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="schema|duplicate"):
        adapter._tables(tmp_path)


def test_manifest_sidecars_are_not_mistaken_for_missing_fixture_originals(adapter, tmp_path):
    import hashlib

    path = raw_page(tmp_path)
    files = {
        p.relative_to(tmp_path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (path, path.with_suffix(".json.meta.json"))
    }
    (tmp_path / "feed_manifest.json").write_text(json.dumps({"files": files}))
    rows, issues, summary = adapter.load_apif(tmp_path, {"98": {"league": "测试联赛"}})
    assert not issues
    assert rows[0]["complete"] is True
    assert summary["complete_scopes"] == 1


def test_cli_writes_new_candidate_queue_and_repeat_has_zero_new(mod, inputs, tmp_path, monkeypatch):
    import zucai.identity_binding_archive as archive

    source = dict(inputs, formal={}, occupied={}, issues=[], summary={})
    monkeypatch.setattr(archive, "load_archive", lambda *args, **kwargs: source)
    first, second = tmp_path / "first", tmp_path / "second"
    mod.main(["--archive", str(tmp_path / "input"), "--out", str(first)])
    mod.main(
        [
            "--archive",
            str(tmp_path / "input"),
            "--out",
            str(second),
            "--previous-candidates",
            str(first / "apif.json"),
        ]
    )
    assert json.loads((second / "summary.json").read_text())["new_candidates"] == 0
    assert (first / "apif.json").read_bytes() == (second / "apif.json").read_bytes()
    with pytest.raises(FileExistsError):
        mod.main(["--archive", str(tmp_path / "input"), "--out", str(first)])


def test_same_provider_proposals_are_integrated_without_auto_activation(mod, inputs):
    formal = {
        "来源": {
            "101": {
                "canonical": "甲队",
                "status": "auto_verified",
                "category": "club/men/senior/first",
                "identity_country": "England",
                "evidence": [{"raw_sha256": "c" * 64}],
            }
        }
    }
    inputs["apif"][0]["home_country"] = "England"
    inputs["official"] = []
    result = propose(mod, inputs, formal=formal)
    assert result["apif"]["测试联赛"]["101"]["status"] == "proposed"
    inputs["apif"][0]["home_country"] = "World"
    assert not ready(propose(mod, inputs, formal=formal))


def test_same_provider_cannot_hide_database_occupancy_conflict(mod, inputs):
    formal = {
        "来源": {
            "101": {
                "canonical": "甲队",
                "status": "auto_verified",
                "category": "club/men/senior/first",
                "identity_country": "England",
                "evidence": [{"raw_sha256": "c" * 64}],
            }
        }
    }
    inputs["apif"][0]["home_country"] = "England"
    inputs["official"] = []
    assert not ready(propose(mod, inputs, formal=formal, occupied={"101": {"3"}}))


def test_invalid_scope_cannot_be_approved():
    from zucai.sources.apif_aliases import validate_approvals

    aliases = {
        "来源": {
            "101": {
                "canonical": "甲队",
                "status": "auto_verified",
                "binding_scope": {"leagues": ["来源"], "category": None},
            }
        }
    }
    with pytest.raises(ValueError, match="binding_scope"):
        validate_approvals(aliases)


def test_pending_head_conflict_cannot_fallback_to_database_clock(adapter, tmp_path):
    assert hasattr(adapter, "select_clock_evidence"), (
        "A1 head contradiction must override DB clocks"
    )
    row = {"match_id": 2042404}
    db = [{"basis": "db_same_jc_id_verified", "kickoff_utc": "2026-10-31T15:15:00Z"}]
    plan = {
        "verified": [],
        "pending": [
            {"match_id": 2042404, "reason": "conflicting_jc_kickoff_evidence", "evidence": db}
        ],
    }
    evidence, issues = adapter.select_clock_evidence(row, db, plan, tmp_path)
    assert evidence == []
    assert issues[0]["reasons"] == ["conflicting_jc_kickoff_evidence"]


def test_legacy_catalogue_clock_is_also_blocked_by_verified_head_disagreement(adapter, tmp_path):
    assert hasattr(adapter, "select_clock_evidence")
    import hashlib

    from zucai.sources.sporttery import API

    raw = (FIXTURE.parents[1] / "heads/success.json").read_bytes()
    path = tmp_path / "state/catalogue/legacy/jc-heads/2042404.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    path.with_suffix(".json.meta.json").write_text(
        json.dumps(
            {
                "url": API + "getMatchHeadV1.qry?source=web&sportteryMatchId=2042404",
                "status_code": 200,
                "fetched_at_utc": "2026-11-01T00:00:00Z",
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    )
    row = {
        "match_id": 2042404,
        "match_date": "2026-10-31",
        "home_team_id": 849,
        "away_team_id": 508,
    }
    db = [{"basis": "db_same_jc_id_verified", "kickoff_utc": "2026-10-31T16:15:00Z"}]
    evidence, issues = adapter.select_clock_evidence(
        row, db, {"verified": [], "pending": []}, tmp_path, extra_paths=[path]
    )
    assert evidence == []
    assert issues[0]["reasons"] == ["conflicting_jc_kickoff_evidence"]


def test_scoped_formal_anchor_cannot_disambiguate_outside_its_time_range(mod, inputs):
    # The only row inside scope is unplayed, so it cannot create a layered proposal.
    inputs["apif"].append(
        dict(
            inputs["apif"][0],
            match_id="apif:11",
            kickoff_utc="2020-05-01T12:00:00Z",
            status="NS",
            score=None,
        )
    )
    inputs["official"].append(dict(inputs["official"][0], match_id="jc:21", home="3", away="4"))
    formal = {
        "测试联赛": {
            "101": {
                "canonical": "甲队",
                "status": "auto_verified",
                "binding_scope": {
                    "leagues": ["测试联赛"],
                    "category": "club/men/senior/first",
                    "start_utc": "2020-05-01T00:00:00Z",
                    "end_utc": "2020-05-02T00:00:00Z",
                },
            }
        }
    }
    assert not ready(propose(mod, inputs, formal=formal))


def test_only_existing_link_deduplicates_official_entities(adapter, inputs):
    assert hasattr(adapter, "official_entities"), (
        "approved multi-source entity deduplication is missing"
    )
    rows = [inputs["official"][0], dict(inputs["official"][0], match_id="fd:JPN:fixture")]
    separate, issues = adapter.official_entities(rows, [])
    assert len(separate) == 2 and not issues
    matches = [
        {
            "match_id": "jc:20",
            "counterpart_id": "fd:JPN:fixture",
            "source": "jc",
            "league": "测试联赛",
            "home_team_id": 1,
            "away_team_id": 2,
            "kickoff_utc": inputs["official"][0]["kickoff_utc"],
        }
    ]
    joined, issues = adapter.official_entities(rows, matches)
    assert len(joined) == 1 and not issues
    assert set(joined[0]["entity_members"]) == {"jc:20", "fd:JPN:fixture"}
    rows[1]["score"] = [0, 0]
    conflict, issues = adapter.official_entities(rows, matches)
    assert len(conflict) == 2 and all(not row["complete"] for row in conflict)
    assert issues[0]["reasons"] == ["linked_entity_evidence_conflict"]


def test_legacy_candidate_same_canonical_is_preserved_without_false_conflict(mod, inputs):
    previous = {
        "测试联赛": {
            "101": {
                "canonical": "甲队",
                "status": "auto_verified",
                "evidence": [{"kind": "legacy"}],
            }
        }
    }
    result = propose(mod, inputs, previous=previous)
    assert result["apif"]["测试联赛"]["101"] == previous["测试联赛"]["101"]
    assert result["existing_candidate_reviews"][0]["evidence_status"] == "strict_verified"
    assert result["summary"]["legacy_preserved"] == 1
    assert result["summary"]["withdrawn"] == 0


def test_real_legacy_queue_is_preserved_in_repeat_replay(mod, inputs, tmp_path, monkeypatch):
    import zucai.identity_binding_archive as archive

    legacy = json.loads((FIXTURE.parent / "legacy-queue.json").read_text())
    monkeypatch.setattr(
        archive,
        "load_archive",
        lambda *args, **kwargs: dict(inputs, formal={}, occupied={}, issues=[], summary={}),
    )
    old = tmp_path / "legacy.json"
    old.write_text(json.dumps(legacy))
    first = mod.replay(tmp_path / "input", tmp_path / "one", previous_candidates=old)
    second = mod.replay(
        tmp_path / "input", tmp_path / "two", previous_candidates=tmp_path / "one/apif.json"
    )
    assert second["summary"]["new_candidates"] == 0
    assert second["apif"] == first["apif"]
    assert second["summary"]["legacy_preserved"] == 1
    for league, entries in legacy.items():
        assert all(second["apif"][league][key] == value for key, value in entries.items())


def test_missing_manifest_candidate_queue_is_not_treated_as_empty(
    mod, inputs, tmp_path, monkeypatch
):
    import zucai.identity_binding_archive as archive

    monkeypatch.setattr(
        archive,
        "load_archive",
        lambda *args, **kwargs: dict(inputs, formal={}, occupied={}, issues=[], summary={}),
    )
    (tmp_path / "feed_manifest.json").write_text(
        json.dumps({"files": {"state/alias_candidates/apif.json": "a" * 64}})
    )
    with pytest.raises(ValueError, match="candidate.*missing"):
        mod.replay(tmp_path, tmp_path / "out")


def test_prior_candidate_in_another_league_is_a_conflict_veto_not_anchor(mod, inputs):
    previous = {"其他": {"101": {"canonical": "丙队", "status": "proposed"}}}
    assert not ready(propose(mod, inputs, previous=previous))


@pytest.mark.parametrize("state", ["AET", "PEN"])
def test_apif_regular_score_uses_explicit_fulltime_not_extra_total(adapter, tmp_path, state):
    path = raw_page(tmp_path)
    row = json.loads(path.read_text())["response"][0]
    row["fixture"]["status"]["short"] = state
    row["goals"] = {"home": 4, "away": 3}
    row["score"]["extratime"] = {"home": 2, "away": 2}
    raw_page(tmp_path, [row])
    rows, _, _ = adapter.load_apif(tmp_path, {"98": {"league": "测试联赛"}})
    assert rows[0]["score"] == [2, 1]
    row["score"].pop("fulltime")
    raw_page(tmp_path, [row])
    rows, _, _ = adapter.load_apif(tmp_path, {"98": {"league": "测试联赛"}})
    assert rows[0]["score"] is None
    assert rows[0]["match_id"] == "apif:10"


def test_old_fd_row_without_time_does_not_discard_current_complete_window(
    adapter, mod, inputs, tmp_path, monkeypatch
):
    import hashlib

    path = tmp_path / "fd/JPN.csv"
    path.parent.mkdir()
    path.write_text((FIXTURE.parent / "fd-date-window.csv").read_text())
    path.with_suffix(".csv.meta.json").write_text(
        json.dumps(
            {
                "url": "https://football-data.co.uk/new/JPN.csv",
                "status_code": 200,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "fetched_at_utc": "2020-06-02T00:00:00Z",
            }
        )
    )
    config = tmp_path / "leagues.json"
    config.write_text(
        json.dumps(
            {
                "JPN": {
                    "div_code": "JPN",
                    "league": "测试联赛",
                    "status": "verified",
                    "verified_by": 2,
                    "verified_utc": "2020-01-01T00:00:00Z",
                    "time_basis": "UTC",
                    "publisher_country": "Test",
                    "publisher_league": "Test",
                }
            }
        )
    )
    aliases = tmp_path / "aliases.json"
    aliases.write_text(
        json.dumps(
            {
                "测试联赛": {
                    "Alpha": {"canonical": "甲队", "status": "auto_verified"},
                    "Beta": {"canonical": "乙队", "status": "auto_verified"},
                }
            }
        )
    )
    monkeypatch.setattr(adapter, "FD_LEAGUES", config)
    monkeypatch.setattr(adapter, "FD_ALIASES", aliases)
    from zucai.sources.teams import normalize_team

    for team in inputs["teams"].values():
        team["name"] = normalize_team(team["name"])
    rows, issues = adapter.load_fd(
        path.parent, inputs["teams"], {}, league_codes={"测试联赛": "98"}
    )
    assert not issues
    assert len(rows) == 2 and all(row["complete"] for row in rows)
    inputs["official"] = rows
    assert len(ready(propose(mod, inputs))) == 2


def test_same_provider_reserve_label_vetoes_first_team_category(mod, inputs):
    formal = {
        "来源": {
            "101": {
                "canonical": "甲队",
                "status": "auto_verified",
                "category": "club/men/senior/first",
                "identity_country": "England",
                "evidence": [{"raw_sha256": "c" * 64}],
            }
        }
    }
    inputs["apif"][0].update(home_name="Alpha II", home_country="England")
    inputs["official"] = []
    assert not ready(propose(mod, inputs, formal=formal))


def typed_formal(*, end=None):
    entry = {
        "canonical": "甲队",
        "status": "auto_verified",
        "category": "club/men/senior/first",
        "identity_country": "England",
        "evidence": [{"raw_sha256": "c" * 64}],
    }
    if end:
        entry["binding_scope"] = {
            "leagues": ["来源"],
            "category": entry["category"],
            "start_utc": "2018-01-01T00:00:00Z",
            "end_utc": end,
        }
    return {"来源": {"101": entry}}


def test_review_same_provider_cannot_use_a_conflicting_original(mod, inputs):
    inputs["official"] = []
    inputs["apif"][0]["home_country"] = "England"
    inputs["apif"].append(
        dict(inputs["apif"][0], home_category="club/women/senior/first", home_name="Alpha Women")
    )
    assert not ready(propose(mod, inputs, formal=typed_formal()))


def test_review_same_provider_respects_source_approval_time_range(mod, inputs):
    inputs["official"] = []
    inputs["apif"][0]["home_country"] = "England"
    assert not ready(propose(mod, inputs, formal=typed_formal(end="2018-12-31T23:59:59Z")))


def test_review_conflicting_formal_anchor_cannot_hide_competitor(mod, inputs):
    inputs["apif"][0].update(home="102", away="104")
    inputs["official"][0].update(home="2", away="4")
    inputs["official"].append(dict(inputs["official"][0], match_id="jc:21", home="1", away="3"))
    assert not ready(propose(mod, inputs, formal=typed_formal(), occupied={"101": {"3"}}))


def test_review_normal_import_checks_binding_scope_for_each_fixture(tmp_path):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from zucai.db import get_engine, init_db, session_scope
    from zucai.models import Team
    from zucai.sources.apif_ingest import _Teams

    engine = get_engine(tmp_path / "scope.db")
    init_db(engine)
    entry = typed_formal(end="2018-12-31T23:59:59Z")["来源"]["101"]
    entry["binding_scope"]["leagues"] = ["测试联赛"]
    aliases = {"测试联赛": {"101": entry, "102": dict(entry, canonical="乙队")}}
    with session_scope(engine) as session:
        session.add_all([Team(name="甲队", aliases=[]), Team(name="乙队", aliases=[])])
        session.flush()
        resolver = _Teams(session, aliases)
        game = SimpleNamespace(
            league="测试联赛",
            home_provider_id=101,
            away_provider_id=102,
            home_raw="Alpha",
            away_raw="Beta",
            kickoff_utc=datetime(2020, 6, 1, 12, tzinfo=UTC),
        )
        with pytest.raises(ValueError, match="binding_scope"):
            resolver.sides(session, game, {})
        game.kickoff_utc = datetime(2018, 6, 1, 12, tzinfo=UTC)
        assert all(side.binding for side in resolver.sides(session, game, {}))
        with pytest.raises(ValueError, match="binding_scope"):
            resolver.resolve(session, "测试联赛", 101)
    engine.dispose()


@pytest.mark.parametrize(
    "field,value",
    [("league", "其他"), ("home_team_id", 999), ("kickoff_utc", "2019-01-01T00:00:00Z")],
)
def test_review_entity_checks_every_intermediate_snapshot(adapter, inputs, field, value):
    rows = [inputs["official"][0], dict(inputs["official"][0], match_id="fd:test:20")]
    base = dict(
        league="测试联赛", home_team_id=1, away_team_id=2, kickoff_utc=rows[0]["kickoff_utc"]
    )
    links = [
        dict(base, match_id="jc:20", source="jc", counterpart_id="apif:30"),
        dict(base, match_id="fd:test:20", source="fd", counterpart_id="apif:30"),
        dict(base, match_id="apif:30", source="apif", counterpart_id=None),
    ]
    good, issues = adapter.official_entities(rows, links)
    assert len(good) == 1 and not issues
    links[2][field] = value
    blocked, issues = adapter.official_entities(rows, links)
    assert len(blocked) == 2 and all(not row["complete"] for row in blocked)
    assert issues[0]["reasons"] == ["linked_entity_evidence_conflict"]


def fd_review_input(tmp_path, monkeypatch, adapter, inputs):
    import hashlib

    from zucai.sources.teams import normalize_team

    folder = tmp_path / "fd"
    folder.mkdir()
    config = tmp_path / "leagues.json"
    config.write_text(
        json.dumps(
            {
                "JPN": {
                    "div_code": "JPN",
                    "league": "测试联赛",
                    "status": "verified",
                    "verified_by": 2,
                    "verified_utc": "2020-01-01T00:00:00Z",
                    "time_basis": "UTC",
                    "publisher_country": "Test",
                    "publisher_league": "Test",
                }
            }
        )
    )
    aliases = tmp_path / "aliases.json"
    aliases.write_text(
        json.dumps(
            {
                "测试联赛": {
                    "Alpha": {"canonical": "甲队", "status": "auto_verified"},
                    "Beta": {"canonical": "乙队", "status": "auto_verified"},
                }
            }
        )
    )
    monkeypatch.setattr(adapter, "FD_LEAGUES", config)
    monkeypatch.setattr(adapter, "FD_ALIASES", aliases)
    for team in inputs["teams"].values():
        team["name"] = normalize_team(team["name"])

    def save(name, raw, status=200):
        path = folder / name
        path.write_text(raw)
        path.with_suffix(".csv.meta.json").write_text(
            json.dumps(
                {
                    "url": "https://football-data.co.uk/new/JPN.csv",
                    "status_code": status,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "fetched_at_utc": "2020-06-02T00:00:00Z",
                }
            )
        )
        return path

    save("good.csv", (FIXTURE.parent / "fd-date-window.csv").read_text())
    return folder, save


@pytest.mark.parametrize(
    "bad_date,expected", [("01/06/2020", 0), ("01/01/2019", 2), ("not-a-date", 0)]
)
def test_review_fd_rejected_competitor_remains_visible_in_its_window(
    adapter, mod, inputs, tmp_path, monkeypatch, bad_date, expected
):
    folder, save = fd_review_input(tmp_path, monkeypatch, adapter, inputs)
    save(
        "bad.csv",
        (FIXTURE.parent / "fd-missing-team.csv").read_text().replace("01/06/2020", bad_date),
    )
    rows, issues = adapter.load_fd(folder, inputs["teams"], {}, league_codes={"测试联赛": "98"})
    assert len(rows) >= 3
    inputs["official"] = rows
    assert len(ready(propose(mod, inputs))) == expected
    assert issues


def test_review_fd_redirect_is_not_a_missing_competitor(
    adapter, mod, inputs, tmp_path, monkeypatch
):
    folder, save = fd_review_input(tmp_path, monkeypatch, adapter, inputs)
    save("redirect.csv", "", status=302)
    rows, issues = adapter.load_fd(folder, inputs["teams"], {}, league_codes={"测试联赛": "98"})
    inputs["official"] = rows
    assert len(ready(propose(mod, inputs))) == 2
    assert issues[0]["reasons"] == ["fd_redirect_observation"]


@pytest.mark.parametrize("duplicate", [True, False])
def test_review_prior_t68_witness_conflict_withdraws_transitive_chain(mod, inputs, duplicate):
    layered(inputs)
    first = propose(mod, inputs)
    if duplicate:
        inputs["apif"].append(dict(inputs["apif"][0], score=[9, 9]))
    else:
        inputs["apif"][0]["score"] = [9, 9]
    changed = propose(mod, inputs, previous=first["apif"])
    assert not ready(changed)
    assert (
        changed["apif"]["测试联赛"]["101"]["evidence"]
        == first["apif"]["测试联赛"]["101"]["evidence"]
    )
    assert changed["apif"]["测试联赛"]["101"]["conflicting_candidates"]
    assert changed["apif"]["测试联赛"]["103"]["evidence_status"] == "withdrawn_dependency"


def test_review_missing_replay_scope_is_not_a_positive_contradiction(mod, inputs):
    first = propose(mod, inputs)
    result = mod.propose_bindings([], [], inputs["teams"], previous=first["apif"], observed_utc=NOW)
    assert result["apif"] == first["apif"]


@pytest.mark.parametrize("year,expected", [(2018, 0), (2024, 1)])
def test_review_stats_index_respects_formal_time_scope(tmp_path, year, expected):
    from zucai.db import get_engine, init_db, session_scope
    from zucai.models import Match, Team
    from zucai.stats_link import link_fixtures

    fixtures = FIXTURE.parents[2] / "t31"
    store = json.loads((fixtures / "store.json").read_text())
    aliases = {"欧冠": json.loads((fixtures / "aliases.json").read_text())["英超"]}
    for entry in aliases["欧冠"].values():
        entry["binding_scope"] = dict(
            leagues=["欧冠"],
            category="club/men/senior/first",
            start_utc=f"{year}-01-01T00:00:00Z",
            end_utc=f"{year}-12-31T23:59:59Z",
        )
    raw = dict(
        get="fixtures",
        parameters={"league": "2", "season": "2024"},
        errors=[],
        results=1,
        paging={"current": 1, "total": 1},
        response=[json.loads((fixtures / "fixture.json").read_text())],
    )
    engine = get_engine(tmp_path / "stats.db")
    init_db(engine)
    with session_scope(engine) as session:
        session.add_all(Team(**row) for row in store["teams"])
        session.flush()
        session.add_all(Match(**row) for row in store["matches"])
        session.flush()
        result = link_fixtures(
            session,
            raw,
            league_id=2,
            season=2024,
            aliases=aliases,
            metadata={
                "raw_location": "fixture://t31/fixture.json",
                "raw_sha256": "a" * 64,
                "captured_utc": NOW,
            },
        )
        assert result["linked"] == expected
        if not expected:
            assert "binding_scope" in result["audit"][0]["detail"]
    engine.dispose()


@pytest.mark.parametrize("case", ["bridge", "database", "infer", "target_anchor"])
def test_review_cup_cannot_remove_scope_through_legacy_routes(tmp_path, case):
    from datetime import datetime

    from zucai.db import get_engine, init_db, session_scope
    from zucai.models import Match, Team
    from zucai.sources.api_common import ApiGame
    from zucai.sources.cup_identity import _bridge, resolve_cup_aliases

    fixture = json.loads((FIXTURE.parents[2] / "t20/identity.json").read_text())
    aliases = deepcopy(fixture["domestic_aliases"])
    # Only one typed ID; an old inference pass must not use it to infer its opponent either.
    aliases["合成国内联赛"].pop("9102")
    entry = aliases["合成国内联赛"]["9101"]
    entry["binding_scope"] = dict(
        leagues=["合成国内联赛"],
        category="club/men/senior/first",
        start_utc="2018-01-01T00:00:00Z",
        end_utc="2018-12-31T23:59:59Z",
    )
    if case == "target_anchor":
        aliases = {"欧罗巴": aliases.pop("合成国内联赛")}
        entry["binding_scope"]["leagues"] = ["欧罗巴"]
    database = tmp_path / "cup.db"
    engine = get_engine(database)
    init_db(engine)
    with session_scope(engine) as session:
        session.add_all(
            [
                Team(
                    id=1,
                    name="合成甲",
                    aliases=["apif:欧罗巴:team:9101"] if case == "database" else [],
                ),
                Team(id=2, name="合成乙", aliases=[]),
            ]
        )
        session.flush()
        session.add(
            Match(
                match_id="jc:cup-test",
                source="jc",
                league="欧罗巴",
                home_team_id=1,
                away_team_id=2,
                kickoff_utc=fixture["official"][0]["kickoff_utc"],
                status="scheduled",
            )
        )
    engine.dispose()
    if case in {"bridge", "database"}:
        output = _bridge("apif", database, "欧罗巴", {"9101": "Alpha"}, aliases)
        assert not output.get("9101", {}).get("canonical")
        return
    games = []
    for row in fixture["games"]:
        row["kickoff_utc"] = datetime.fromisoformat(row["kickoff_utc"])
        row["full"], row["half"] = tuple(row["full"]), tuple(row["half"])
        games.append(ApiGame(provider="apif", league="欧罗巴", **row))
    capture = dict(
        games=games,
        scope={"league_id": "3", "season": 2024},
        complete=True,
        raw_sha256="a" * 64,
        sidecar="fixture://t20/identity.json",
        seen_utc="2026-11-03T00:00:00Z",
    )
    output = resolve_cup_aliases("apif", database, aliases, [capture], tmp_path / "audit")
    assert output["欧罗巴"]["9102"]["status"] == "proposed"
    if case == "target_anchor":
        assert output["欧罗巴"]["9101"] == entry
    else:
        assert output["欧罗巴"]["9101"]["status"] == "proposed"


def test_review_rederived_candidate_preserves_old_conflict_observations(mod, inputs):
    first = propose(mod, inputs)
    inputs["apif"][0]["score"] = [9, 9]
    inputs["official"][0]["score"] = [9, 9]
    changed = propose(mod, inputs, previous=first["apif"])
    assert not ready(changed)
    entry = changed["apif"]["测试联赛"]["101"]
    assert entry["evidence_status"] == "withdrawn_conflict"
    assert entry["conflicting_candidates"]
    assert first["apif"]["测试联赛"]["101"]["evidence"][0] in entry["evidence"]


@pytest.mark.parametrize("owner", ["formal", "database"])
def test_review_prior_t68_global_owner_conflict_withdraws_witness(mod, inputs, owner):
    layered(inputs)
    first = propose(mod, inputs)
    kwargs = (
        {"formal": {"另一联赛": {"101": {"canonical": "丙队", "status": "approved"}}}}
        if owner == "formal"
        else {"occupied": {"101": {"3"}}}
    )
    changed = mod.propose_bindings(
        [], [], inputs["teams"], previous=first["apif"], observed_utc=NOW, **kwargs
    )
    assert not ready(changed)
    assert changed["apif"]["测试联赛"]["101"]["evidence_status"] == "withdrawn_conflict"
    assert changed["apif"]["测试联赛"]["101"]["conflicting_owners"]
    assert changed["apif"]["测试联赛"]["103"]["evidence_status"] == "withdrawn_dependency"
