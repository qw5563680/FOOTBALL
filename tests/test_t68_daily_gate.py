"""The daily switch gate cannot manufacture denominators or accept declarations."""

import importlib
import json
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlencode

import pytest

FIXTURE = Path(__file__).parents[1] / "data/fixtures/t68/daily_gate/facts.json"


@pytest.fixture
def facts():
    return json.loads(FIXTURE.read_bytes())


def gate(facts, **kwargs):
    module = importlib.import_module("zucai.identity_daily_gate")
    return module.assess_league(facts, "307", "2026-11-14T12:00:00Z", **kwargs)


def test_missing_evidence_stays_insufficient_with_real_counts(facts):
    result = gate(facts)
    assert result["status"] == "insufficient_evidence"
    assert result["teams"]["total"] == result["teams"]["bound"] == 2
    assert result["matches"]["total"] == result["matches"]["linked"] == 1
    assert result["recent_window"] == {"start_beijing": "2026-10-15", "end_beijing": "2026-11-13"}
    assert {"current_season_replay", "recent_replay", "risk_review", "persistence"} <= set(
        result["missing_evidence"]
    )
    assert result["daily_changed"] is False


def test_zero_denominator_is_not_100_percent(facts):
    facts["official"] = []
    result = gate(facts)
    assert result["teams"]["binding_rate"] is None
    assert result["matches"]["link_rate"] is None
    assert "official_window_empty" in result["missing_evidence"]


def test_reason_for_every_unbound_team_does_not_authorize_daily(facts):
    facts["formal"] = {}
    result = gate(facts)
    assert result["teams"]["bound"] == 0
    assert len(result["teams"]["unbound"]) == 2
    assert all(row["reason"] for row in result["teams"]["unbound"])
    assert result["status"] != "ready_for_review"
    assert "current_roster_unbound" in result["blockers"]


def test_complete_booleans_cannot_replace_verified_artifacts(facts):
    result = gate(
        facts,
        evidence={
            key: {"complete": True}
            for key in ("current_season_replay", "recent_replay", "risk_review", "persistence")
        },
    )
    assert result["status"] == "insufficient_evidence"
    assert len(result["missing_evidence"]) >= 4


def test_scope_and_proposed_bindings_do_not_count(facts):
    facts["formal"]["测试联赛"]["10"]["status"] = "proposed"
    result = gate(facts)
    assert result["teams"]["bound"] == 1
    assert result["matches"]["linked"] == 0


def test_duplicate_relation_cannot_be_counted_twice(facts):
    extra = dict(facts["official"][0], match_id="jc:21")
    facts["official"].append(extra)
    facts["matches"].append(dict(facts["matches"][0], match_id="jc:21"))
    result = gate(facts)
    assert result["matches"]["total"] == 2
    assert result["matches"]["linked"] == 0
    assert "non_unique_relation" in result["blockers"]


def receipt(tmp_path, name, content):
    path = tmp_path / name
    raw = json.dumps(content).encode()
    path.write_bytes(raw)
    return dict(path=name, sha256=sha256(raw).hexdigest())


def daily_originals(tmp_path, *, omit=None, empty=False, first_day=date(2026, 10, 1)):
    from zucai.sources.base import RawStore

    template = json.loads((FIXTURE.parents[2] / "t14/apif.json").read_bytes())
    refs, ids = [], []
    for offset in range(31):
        day = first_day + timedelta(days=offset)
        if day == omit:
            continue
        doc = deepcopy(template)
        doc["parameters"] = {"date": day.isoformat()}
        if day.day == 20 and not empty:
            row = doc["response"][0]
            row["league"]["id"] = 307
            row["teams"]["home"]["id"] = 10
            row["teams"]["away"]["id"] = 11
            when = datetime(2026, 10, 20, 12, tzinfo=UTC)
            row["fixture"].update(id=10, timestamp=int(when.timestamp()), date=when.isoformat())
            ids.append("apif:10")
        else:
            doc["response"] = []
            doc["results"] = 0
        raw = json.dumps(doc).encode()
        path = RawStore(tmp_path).save(
            "api-football",
            "fixtures",
            raw,
            "json",
            status_code=200,
            url="https://v3.football.api-sports.io/fixtures?date=" + day.isoformat(),
            captured_at=datetime(2026, 11, 14, tzinfo=UTC),
        )
        refs.append(
            dict(path=path.relative_to(tmp_path).as_posix(), sha256=sha256(raw).hexdigest())
        )
    outcomes = [{"fixture_id": mid, "outcome": "inserted"} for mid in ids]
    result = receipt(
        tmp_path,
        "replay.json",
        dict(
            fixture_ids=ids,
            raw_sha256=[r["sha256"] for r in refs],
            inserted=len(ids),
            risk_ids=[],
            outcomes=outcomes,
            official_match_ids=["jc:20"],
        ),
    )
    return dict(originals=refs, results=result)


def test_complete_30_days_checks_both_utc_boundary_dates(facts, tmp_path):
    from zucai.identity_daily_gate import _recent_replay

    section = daily_originals(tmp_path)
    result = _recent_replay(facts, "307", section, tmp_path, date(2026, 10, 2), date(2026, 10, 31))
    assert result["distinct_fixtures"] == 1
    assert len(result["utc_dates"]) == 31


def test_one_missing_utc_boundary_cannot_be_complete(facts, tmp_path):
    from zucai.identity_daily_gate import _recent_replay

    section = daily_originals(tmp_path, omit=date(2026, 10, 1))
    with pytest.raises(ValueError, match="missing UTC"):
        _recent_replay(facts, "307", section, tmp_path, date(2026, 10, 2), date(2026, 10, 31))


def test_empty_30_days_needs_explicit_recent_active_window(facts, tmp_path):
    from zucai.identity_daily_gate import _recent_replay

    section = daily_originals(tmp_path, empty=True)
    with pytest.raises(ValueError, match="zero fixture"):
        _recent_replay(facts, "307", section, tmp_path, date(2026, 10, 2), date(2026, 10, 31))


def test_replay_risks_cannot_be_hidden_by_empty_risk_ids(facts, tmp_path):
    from zucai.identity_daily_gate import _recent_replay

    section = daily_originals(tmp_path)
    result = json.loads((tmp_path / "replay.json").read_bytes())
    result["outcomes"][0]["outcome"] = "rejected"
    section["results"] = receipt(tmp_path, "replay.json", result)
    with pytest.raises(ValueError, match="risk inventory"):
        _recent_replay(facts, "307", section, tmp_path, date(2026, 10, 2), date(2026, 10, 31))


def test_equivalent_samples_use_fresh_complete_season_denominator(facts, tmp_path):
    from zucai.identity_daily_gate import RISKS, _recent_replay
    from zucai.sources.base import RawStore

    docs = daily_originals(tmp_path)
    ref = next(
        r for r in docs["originals"] if json.loads((tmp_path / r["path"]).read_bytes())["response"]
    )
    doc = json.loads((tmp_path / ref["path"]).read_bytes())
    doc["parameters"] = {"league": "307", "season": "2026"}
    doc["response"][0]["teams"]["home"]["id"] = 10
    doc["response"][0]["teams"]["away"]["id"] = 11
    raw = json.dumps(doc).encode()
    path = RawStore(tmp_path).save(
        "api-football",
        "fixtures",
        raw,
        "json",
        status_code=200,
        url="https://v3.football.api-sports.io/fixtures?league=307&season=2026",
        captured_at=datetime(2026, 11, 1, tzinfo=UTC),
    )
    original = dict(path=path.relative_to(tmp_path).as_posix(), sha256=sha256(raw).hexdigest())
    facts["apif"][0]["provenance"] = [dict(original, fetched_at_utc="2026-11-01T00:00:00Z")]
    cases = []
    for category in sorted(RISKS):
        boundary = deepcopy(doc)
        row = boundary["response"][0]
        if category == "partial_pages":
            boundary["paging"]["total"] = 2
        elif category == "unbound":
            row["teams"]["home"]["id"] = 551
        elif category == "reversed":
            row["teams"]["home"]["id"], row["teams"]["away"]["id"] = 11, 10
        elif category == "rejected":
            row["fixture"]["status"]["short"] = "AWD"
        else:
            other = deepcopy(row)
            other["fixture"]["id"] = 11
            if category == "cross_day":
                when = datetime.fromisoformat(other["fixture"]["date"]) + timedelta(days=1)
                other["fixture"].update(timestamp=int(when.timestamp()), date=when.isoformat())
            boundary["response"].append(other)
            boundary["results"] = 2
        boundary_raw = json.dumps(boundary).encode()
        boundary_path = RawStore(tmp_path).save(
            "api-football",
            category,
            boundary_raw,
            "json",
            status_code=200,
            url="https://v3.football.api-sports.io/fixtures?league=307&season=2026",
            captured_at=datetime(2026, 11, 1, tzinfo=UTC),
        )
        boundary_ref = dict(
            path=boundary_path.relative_to(tmp_path).as_posix(),
            sha256=sha256(boundary_raw).hexdigest(),
        )
        cases.append(
            dict(
                category=category,
                fixture_ids=["apif:10"],
                source_sha256=boundary_ref["sha256"],
                original=boundary_ref,
                outcome="offline boundary regression, synthetic fixture",
            )
        )
    section = dict(
        mode="equivalent",
        originals=[original],
        boundary_cases=receipt(tmp_path, "boundary.json", cases),
        results=receipt(
            tmp_path,
            "equivalent.json",
            dict(
                fixture_ids=["apif:10"],
                official_match_ids=["jc:20"],
                raw_sha256=[original["sha256"]],
                inserted=1,
                risk_ids=[],
                outcomes=[dict(fixture_id="apif:10", outcome="inserted")],
            ),
        ),
    )
    result = _recent_replay(facts, "307", section, tmp_path, date(2026, 10, 2), date(2026, 10, 31))
    assert result["distinct_fixtures"] == 1
    assert result["expected_distinct_fixtures"] == 1
    facts["apif"][0]["complete"] = False
    with pytest.raises(ValueError, match="denominator"):
        _recent_replay(facts, "307", section, tmp_path, date(2026, 10, 2), date(2026, 10, 31))


def test_current_season_replay_cannot_report_complete_with_held(facts, tmp_path):
    from zucai.identity_daily_gate import _current_replay

    report = dict(
        processed_files=1,
        scopes=[
            dict(
                code="307",
                season=2026,
                status="complete",
                parsed_matches=1,
                raw={"sha256": "a" * 64},
                held_unbound=1,
            )
        ],
    )
    audit = tmp_path / "held.csv"
    audit.write_text("raw_sha256,league,provider_match_id\na,测试联赛,10\n")
    section = dict(
        report=receipt(tmp_path, "current.json", report),
        held_before=dict(path="held.csv", sha256=sha256(audit.read_bytes()).hexdigest()),
    )
    with pytest.raises(ValueError, match="completely replayed"):
        _current_replay(facts, "307", section, tmp_path)


def test_persistence_requires_materialized_files_not_only_equal_manifest_claims(tmp_path):
    from zucai.identity_daily_gate import _persistence

    repeat = dict(inserted=0, status="complete", processed_files=1, scopes=[dict(code="307")])
    state = dict(
        coverage={},
        relations=[],
        quarantine=[],
        training_selection=[],
        immutable_sha256={"ledger.jsonl": "c" * 64},
    )
    manifest = dict(counts={}, columns={}, sources={}, files={"state/matches.jsonl": "d" * 64})
    section = dict(repeat_report=receipt(tmp_path, "repeat.json", repeat))
    for key in ("before_repeat", "after_repeat", "restored"):
        section[key] = receipt(tmp_path, key + ".json", state)
    for key in ("before_manifest", "after_manifest", "restored_manifest"):
        section[key] = receipt(tmp_path, key + ".json", manifest)
    with pytest.raises((OSError, ValueError)):
        _persistence(section, tmp_path, "307")


def test_replay_receipt_cannot_hide_rejected_raw_fixture(facts, tmp_path):
    from zucai.identity_daily_gate import _recent_replay

    section = daily_originals(tmp_path)
    old = next(
        ref
        for ref in section["originals"]
        if json.loads((tmp_path / ref["path"]).read_bytes())["response"]
    )
    path = tmp_path / old["path"]
    doc = json.loads(path.read_bytes())
    doc["response"][0]["fixture"]["status"]["short"] = "AWD"
    path.write_text(json.dumps(doc))
    old_sha = old["sha256"]
    old["sha256"] = sha256(path.read_bytes()).hexdigest()
    meta_path = path.with_suffix(".json.meta.json")
    meta = json.loads(meta_path.read_bytes())
    meta["sha256"] = old["sha256"]
    meta_path.write_text(json.dumps(meta))
    replay = json.loads((tmp_path / "replay.json").read_bytes())
    replay["raw_sha256"] = [old["sha256"] if s == old_sha else s for s in replay["raw_sha256"]]
    section["results"] = receipt(tmp_path, "replay.json", replay)
    with pytest.raises(ValueError, match="risk inventory"):
        _recent_replay(facts, "307", section, tmp_path, date(2026, 10, 2), date(2026, 10, 31))


@pytest.mark.parametrize("midmonth", [False, True])
def test_complete_offline_register_is_review_only_and_restoration_tamper_blocks(
    facts, tmp_path, midmonth
):
    from zucai.identity_daily_gate import assess_league

    recent = daily_originals(
        tmp_path, first_day=date(2026, 10, 14) if midmonth else date(2026, 10, 1)
    )
    if midmonth:
        recent["official_originals"] = official_supplement(tmp_path)
    current = receipt(
        tmp_path,
        "current.json",
        dict(
            processed_files=1,
            scopes=[
                dict(
                    code="307",
                    season=2026,
                    status="complete",
                    parsed_matches=1,
                    raw={"sha256": "a" * 64},
                )
            ],
        ),
    )
    held = tmp_path / "held.csv"
    held.write_text("raw_sha256,league,provider_match_id\na,测试联赛,10\n")
    repeat = receipt(
        tmp_path,
        "repeat.json",
        dict(inserted=0, status="complete", processed_files=1, scopes=[dict(code="307")]),
    )
    persistence = dict(repeat_report=repeat)
    state_bytes = json.dumps(facts["matches"]).encode()
    ledger = b"frozen offline fixture\n"
    ledger_sha = sha256(ledger).hexdigest()
    for name, key in (
        ("before", "before_manifest"),
        ("after", "after_manifest"),
        ("restored", "restored_manifest"),
    ):
        folder = tmp_path / name
        (folder / "state").mkdir(parents=True)
        (folder / "state/matches.jsonl").write_bytes(state_bytes)
        (folder / "ledger.jsonl").write_bytes(ledger)
        manifest = dict(
            counts={"matches": 1},
            columns={"matches": ["fixture"]},
            sources={},
            files={"state/matches.jsonl": sha256(state_bytes).hexdigest()},
        )
        persistence[key] = receipt(tmp_path, name + "/feed_manifest.json", manifest)
    facts["manifest"]["sha256"] = persistence["before_manifest"]["sha256"]
    snapshot = dict(
        coverage={"teams": 2, "matches": 1},
        relations=[["jc:20", "apif:10"]],
        quarantine=[],
        training_selection=[],
        immutable_sha256={"ledger.jsonl": ledger_sha},
    )
    for key in ("before_repeat", "after_repeat", "restored"):
        persistence[key] = receipt(tmp_path, key + "-snapshot.json", snapshot)
    evidence = dict(
        provider_code="307",
        cutoff_utc="2026-11-14T00:00:00Z" if midmonth else "2026-11-01T00:00:00Z",
        archive_sha256=facts["manifest"]["sha256"],
        current_season_replay=dict(
            report=current,
            held_before=dict(path="held.csv", sha256=sha256(held.read_bytes()).hexdigest()),
        ),
        recent_replay=recent,
        risk_review=receipt(tmp_path, "risks.json", []),
        persistence=persistence,
    )
    result = assess_league(
        facts, "307", evidence["cutoff_utc"], evidence=evidence, evidence_root=tmp_path
    )
    assert result["status"] == "ready_for_review"
    assert result["daily_changed"] is False
    assert result["online_permission"] == "reviewer_verification_required"
    assert result["official_window"]["end_beijing"] == "2026-10-31"
    assert result["evidence"]["recent_replay"]["official_match_ids"] == ["jc:20"]
    if midmonth:
        assert result["recent_window"]["end_beijing"] == "2026-11-13"
        held.write_text(held.read_text() + "a,测试联赛,999\n")
        evidence["current_season_replay"]["held_before"]["sha256"] = sha256(
            held.read_bytes()
        ).hexdigest()
        proof = receipt(
            tmp_path, "pending-review.json", {"reason": "still waiting for missing original"}
        )
        evidence["current_season_replay"]["held_explanations"] = receipt(
            tmp_path,
            "pending.json",
            [
                dict(
                    fixture_id="apif:999",
                    reason="missing current original",
                    disposition="still_held",
                    evidence=proof,
                )
            ],
        )
        pending = assess_league(
            facts, "307", evidence["cutoff_utc"], evidence=evidence, evidence_root=tmp_path
        )
        assert pending["status"] == "blocked"
        assert "current_season_held_unresolved" in pending["blockers"]
        assert pending["evidence"]["current_season_replay"]["held_before"] == 2
    (tmp_path / "restored/ledger.jsonl").write_bytes(b"modified")
    result = assess_league(
        facts, "307", evidence["cutoff_utc"], evidence=evidence, evidence_root=tmp_path
    )
    assert result["status"] == "insufficient_evidence"
    assert "persistence" in result["missing_evidence"]


def official_supplement(tmp_path, *, omit=None, extra_match=False):
    from zucai.sources.base import RawStore
    from zucai.sources.results import API

    template = json.loads((FIXTURE.parents[2] / "t61/jc-empty.json").read_bytes())
    real = json.loads((FIXTURE.parents[2] / "t65/jc-one-result-real.json").read_bytes())["value"][
        "matchResult"
    ][0]
    refs = []
    for number in range(1, 14):
        day = date(2026, 11, number)
        if day == omit:
            continue
        doc = deepcopy(template)
        value = doc["value"]
        value["leagueList"] = [
            dict(leagueId=77, leagueAllName="测试联赛全名", leagueAbbName="测试联赛")
        ]
        if extra_match and number == 5:
            row = dict(
                real,
                matchDate=day.isoformat(),
                matchId=21,
                leagueId=77,
                leagueName="测试联赛全名",
                leagueNameAbbr="测试联赛",
                homeTeamId=501,
                awayTeamId=502,
            )
            value.update(matchResult=[row], total=1, pages=1)
        query = dict(
            matchBeginDate=day.isoformat(),
            matchEndDate=day.isoformat(),
            leagueId="",
            pageSize=30,
            pageNo=1,
            isFix=0,
            matchPage=1,
            pcOrWap=1,
        )
        raw = json.dumps(doc).encode()
        path = RawStore(tmp_path).save(
            "jc-results",
            "catalogue",
            raw,
            "json",
            status_code=200,
            url=API + "getUniformMatchResultV1.qry?" + urlencode(query),
            captured_at=datetime(2026, 11, 14, tzinfo=UTC),
        )
        refs.append(
            dict(path=path.relative_to(tmp_path).as_posix(), sha256=sha256(raw).hexdigest())
        )
    return refs


@pytest.mark.parametrize(
    "problem", ["missing_day", "sha", "wrong_date", "partial", "missing_team", "extra_match"]
)
def test_recent_official_supplement_is_verified_not_declared(facts, tmp_path, problem):
    from zucai.identity_daily_gate import _recent_replay

    section = daily_originals(tmp_path, first_day=date(2026, 10, 14))
    section["official_originals"] = official_supplement(
        tmp_path,
        omit=date(2026, 11, 3) if problem == "missing_day" else None,
        extra_match=problem in {"missing_team", "extra_match"},
    )
    if problem in {"sha", "wrong_date", "partial", "missing_team"}:
        ref = section["official_originals"][4]
        path = tmp_path / ref["path"]
        doc = json.loads(path.read_bytes())
        if problem == "wrong_date":
            doc["value"].update(
                total=1,
                pages=1,
                matchResult=[
                    dict(
                        matchId=21,
                        matchDate="2026-11-06",
                        leagueId=77,
                        leagueName="测试联赛全名",
                        leagueNameAbbr="测试联赛",
                        homeTeamId=501,
                        awayTeamId=502,
                    )
                ],
            )
        elif problem == "partial":
            doc["value"].update(total=31, pages=2)
        elif problem == "missing_team":
            doc["value"]["matchResult"][0].pop("awayTeamId")
        else:
            doc["extra"] = "tamper"
        path.write_text(json.dumps(doc))
        ref["sha256"] = sha256(path.read_bytes()).hexdigest()
        if problem != "sha":
            meta_path = path.with_suffix(".json.meta.json")
            meta = json.loads(meta_path.read_bytes())
            meta["sha256"] = ref["sha256"]
            meta_path.write_text(json.dumps(meta))
    with pytest.raises(ValueError):
        _recent_replay(facts, "307", section, tmp_path, date(2026, 10, 15), date(2026, 11, 13))


@pytest.mark.parametrize("season", ["2026", "2025", ""])
def test_prior_held_ids_cannot_disappear_or_guess_another_season(facts, tmp_path, season):
    from zucai.identity_daily_gate import _current_replay

    report = receipt(
        tmp_path,
        "current.json",
        dict(
            processed_files=1,
            scopes=[
                dict(
                    code="307",
                    season=2026,
                    status="complete",
                    parsed_matches=1,
                    raw={"sha256": "a" * 64},
                )
            ],
        ),
    )
    audit = tmp_path / "held.csv"
    audit.write_text(
        "raw_sha256,league,provider_match_id,kickoff_utc,season\n"
        + "a" * 64
        + ",测试联赛,999,2026-10-21T12:00:00Z,"
        + season
        + "\n"
    )
    section = dict(
        report=report,
        held_before=dict(path="held.csv", sha256=sha256(audit.read_bytes()).hexdigest()),
    )
    with pytest.raises(ValueError, match="unresolved|unexplained|missing"):
        _current_replay(facts, "307", section, tmp_path)


def test_recent_supplement_adds_actual_official_ids_without_extending_12_months(facts, tmp_path):
    from zucai.identity_daily_gate import _recent_replay

    section = daily_originals(tmp_path, first_day=date(2026, 10, 14))
    section["official_originals"] = official_supplement(tmp_path, extra_match=True)
    replay = json.loads((tmp_path / "replay.json").read_bytes())
    replay["official_match_ids"] = ["jc:20", "jc:21"]
    section["results"] = receipt(tmp_path, "replay.json", replay)
    result = _recent_replay(facts, "307", section, tmp_path, date(2026, 10, 15), date(2026, 11, 13))
    assert result["official_match_ids"] == ["jc:20", "jc:21"]
    assert result["official_window"]["official_team_ids"] == ["501", "502"]
    assert facts["catalogue_window"]["end_beijing"] == "2026-10-31"
    assert len(facts["official"]) == 1


def test_held_other_season_requires_matching_verified_original_and_remains_counted(facts, tmp_path):
    from zucai.identity_daily_gate import _current_replay

    facts["apif"].append(
        dict(facts["apif"][0], match_id="apif:999", season=2025, provenance=[{"sha256": "c" * 64}])
    )
    report = receipt(
        tmp_path,
        "current.json",
        dict(
            processed_files=1,
            scopes=[
                dict(
                    code="307",
                    season=2026,
                    status="complete",
                    parsed_matches=1,
                    raw={"sha256": "a" * 64},
                )
            ],
        ),
    )
    audit = tmp_path / "held.csv"
    audit.write_text(
        "raw_sha256,league,provider_match_id,season\n" + "c" * 64 + ",测试联赛,999,2025\n"
    )
    section = dict(
        report=report,
        held_before=dict(path="held.csv", sha256=sha256(audit.read_bytes()).hexdigest()),
    )
    result = _current_replay(facts, "307", section, tmp_path)
    assert result["held_before"] == 1 and result["held_before_ids"] == ["apif:999"]
    assert result["excluded_other_season_ids"] == ["apif:999"]
    assert result["resolved_ids"] == []
    facts["apif"].append(dict(facts["apif"][0], match_id="apif:999"))
    report_doc = json.loads((tmp_path / "current.json").read_bytes())
    report_doc["scopes"][0]["parsed_matches"] = 2
    section["report"] = receipt(tmp_path, "current.json", report_doc)
    with pytest.raises(ValueError, match="contradict"):
        _current_replay(facts, "307", section, tmp_path)


def test_missing_held_id_can_be_explained_but_not_counted_as_resolved(facts, tmp_path):
    from zucai.identity_daily_gate import _current_replay

    report = receipt(
        tmp_path,
        "current.json",
        dict(
            processed_files=1,
            scopes=[
                dict(
                    code="307",
                    season=2026,
                    status="complete",
                    parsed_matches=1,
                    raw={"sha256": "a" * 64},
                )
            ],
        ),
    )
    audit = tmp_path / "held.csv"
    audit.write_text("raw_sha256,league,provider_match_id\n" + "a" * 64 + ",测试联赛,999\n")
    proof = receipt(
        tmp_path,
        "missing-fixture-review.json",
        dict(reason="complete source no longer returns the previously held ID; keep pending"),
    )
    decisions = receipt(
        tmp_path,
        "explanations.json",
        [
            dict(
                fixture_id="apif:999",
                disposition="still_held",
                reason="needs source review",
                evidence=proof,
            )
        ],
    )
    result = _current_replay(
        facts,
        "307",
        dict(
            report=report,
            held_before=dict(path="held.csv", sha256=sha256(audit.read_bytes()).hexdigest()),
            held_explanations=decisions,
        ),
        tmp_path,
    )
    assert result["held_before_ids"] == ["apif:999"] and result["resolved_ids"] == []
    assert result["explained_unresolved"][0]["fixture_id"] == "apif:999"


def test_held_missing_league_scope_cannot_silently_leave_denominator(facts, tmp_path):
    from zucai.identity_daily_gate import _current_replay

    report = receipt(
        tmp_path,
        "current.json",
        dict(
            processed_files=1,
            scopes=[
                dict(
                    code="307",
                    season=2026,
                    status="complete",
                    parsed_matches=1,
                    raw={"sha256": "a" * 64},
                )
            ],
        ),
    )
    audit = tmp_path / "held.csv"
    audit.write_text("raw_sha256,league,provider_match_id\n" + "a" * 64 + ",,999\n")
    with pytest.raises(ValueError, match="scope|missing"):
        _current_replay(
            facts,
            "307",
            dict(
                report=report,
                held_before=dict(path="held.csv", sha256=sha256(audit.read_bytes()).hexdigest()),
            ),
            tmp_path,
        )
