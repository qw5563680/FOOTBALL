"""Permanent actual-entry regressions from the fresh PR143 whole-branch review."""

import json
from copy import deepcopy

import pytest

from test_t68_daily_gate_entry import bundle as gate_bundle
from test_t68_daily_gate_entry import run
from test_t68_identity_archive_entry import CAPTURED, CLOCK, raw, seal, table
from test_t68_identity_archive_entry import archive as archive_fixture
from test_t68_jc_kickoffs import catalogue
from zucai import identity_binding_archive as reader
from zucai import identity_categories
from zucai.identity_binding import replay

frozen_archive = archive_fixture


archive = archive_fixture


def test_p1_stored_witness_outside_catalogue(archive):
    # Target catalogue match is not stored; a different stored JC match has a
    # fully authenticated head with exactly the same official numeric IDs.
    table(
        archive,
        "matches",
        [
            dict(
                match_id="jc:2042405",
                source="jc",
                league="英联赛杯",
                home_team_id=1,
                away_team_id=2,
                kickoff_utc="2026-10-30T12:15:00Z",
                status="finished",
                result_provider="jc",
                result_checked_utc=CAPTURED,
            )
        ],
    )
    table(archive, "results", [dict(match_id="jc:2042405", home_goals=1, away_goals=3)])
    head = json.loads((archive / "state/catalogue/heads/jc-heads/2042404.json").read_bytes())
    head["value"].update(sportteryMatchId=2042405, matchDateTime="2026-10-30 20:15")
    raw(
        archive,
        "state/catalogue/heads/jc-heads/2042405.json",
        head,
        __import__("zucai.sources.sporttery", fromlist=["API"]).API
        + "getMatchHeadV1.qry?source=web&sportteryMatchId=2042405",
    )
    seal(archive)
    from zucai import jc_catalogue_kickoffs as k

    old = dict(k._catalogue(archive)[0][0], match_id=2042405, match_date="2026-10-30")
    assert (
        k._head(archive / "state/catalogue/heads/jc-heads/2042405.json", old, archive)["basis"]
        == "jc_head_raw_verified"
    )
    result = replay(archive, archive.parent / "outside-out", leagues=["48"])
    row = next(r for r in result["official_register"]["rows"] if r["official_team_id"] == "849")
    assert row["project_team_id"] == "1", row["identity_status"]


@pytest.mark.parametrize(
    "issue",
    [
        "official_marker",
        "wrong_official_exception",
        "clean",
        "exact_exception",
        "same_provider_only",
    ],
)
def test_same_provider_veto_cannot_be_bypassed(archive, monkeypatch, issue):
    # Actual archive entry with national teams: same-provider reuse legitimately
    # uses formal national categories and does not require a country field.
    catalogue(archive, leagues=[72])
    table(
        archive,
        "matches",
        [
            dict(
                match_id="jc:2042404",
                source="jc",
                league="世界杯",
                home_team_id=1,
                away_team_id=2,
                kickoff_utc=CLOCK,
                status="finished",
                result_provider="jc",
                result_checked_utc=CAPTURED,
            )
        ],
    )
    pagepath = archive / "state/catalogue/catalogue/jc/2026-10/2026-10-31-p1.json"
    page = json.loads(pagepath.read_bytes())
    if issue == "official_marker":
        page["value"]["matchResult"][0]["homeTeam"] = "Test U21"
        raw(
            archive,
            pagepath.relative_to(archive),
            page,
            json.loads(pagepath.with_suffix(".json.meta.json").read_bytes())["url"],
        )
    apath = archive / "state/catalogue/backfill/backfill/api-football/day/fixtures_one.json"
    doc = json.loads(apath.read_bytes())
    doc["parameters"] = {"league": "1", "season": "2026"}
    doc["response"][0]["league"] = {"id": 1, "season": 2026}
    if issue == "same_provider_only":
        from datetime import datetime, timedelta

        when = datetime.fromisoformat(CLOCK) + timedelta(minutes=30)
        doc["response"][0]["fixture"].update(date=when.isoformat(), timestamp=int(when.timestamp()))
    if issue in {"wrong_official_exception", "exact_exception"}:
        doc["response"][0]["teams"]["home"]["name"] = "Test U21"
    raw(
        archive,
        apath.relative_to(archive),
        doc,
        "https://v3.football.api-sports.io/fixtures?league=1&season=2026",
    )
    formal = {
        "欧国联": {
            "101": {
                "canonical": "Project home",
                "status": "auto_verified",
                "category": "national/men/senior/standard",
                "evidence": [{"raw_sha256": "a" * 64}],
            }
        }
    }
    f = archive.parent / "formal.json"
    f.write_text(json.dumps(formal))
    monkeypatch.setattr(reader, "FORMAL", f)
    if issue in {"wrong_official_exception", "exact_exception"}:
        from hashlib import sha256

        evidence = archive.parent / "proof.txt"
        evidence.write_text("Synthetic offline exact other official team review")
        entry = dict(
            competition_id="1",
            official_team_id="849" if issue == "exact_exception" else "999999",
            provider_team_id="101",
            category="national/men/senior/standard",
            allowed_labels=["Test U21"],
            status="approved",
            approved_by="qw5563680",
            approved_utc="2026-11-14T02:00:00Z",
            approval_pr="https://github.com/qw5563680/FOOTBALL/pull/143",
            review_url="https://github.com/qw5563680/FOOTBALL/pull/143#synthetic-review",
            evidence=[
                dict(
                    path=evidence.name,
                    sha256=sha256(evidence.read_bytes()).hexdigest(),
                    observed_utc=CAPTURED,
                )
            ],
        )
        exceptions = archive.parent / "exceptions.json"
        exceptions.write_text(json.dumps(dict(version=1, exceptions=[entry])))
        monkeypatch.setattr(identity_categories, "EXCEPTIONS", exceptions)
    seal(archive)
    result = replay(archive, archive.parent / "same-provider-out", leagues=["1"])
    row = next(r for r in result["official_register"]["rows"] if r["official_team_id"] == "849")
    assert bool(row["candidate_ids"]) == (
        issue in {"clean", "exact_exception", "same_provider_only"}
    ), row["api_candidates"]
    if issue == "same_provider_only":
        assert row["api_candidates"][0]["same_provider_evidence"]
        page = (archive.parent / "same-provider-out/owner-review/1-849.md").read_text()
        assert "/欧国联/101" in page and "fixtures_one.json" in page
        assert "同源编号依据" in page


bundle = gate_bundle


def test_persistence_detects_real_scoreboard_ledger_mutation(bundle):
    archive, root, evidence = bundle
    for folder in [
        archive,
        root / "preimport",
        root / "after",
        root / "restored",
        root / "daily-after",
    ]:
        score = folder / "scoreboard/ledger.jsonl"
        score.parent.mkdir(parents=True, exist_ok=True)
        score.write_text('{"synthetic_offline_ledger":"original"}\n')
    (root / "restored/scoreboard/ledger.jsonl").write_text(
        '{"synthetic_offline_ledger":"CHANGED"}\n'
    )
    result = run(bundle)
    assert result["status"] != "ready_for_review", result["evidence"]["persistence"]


def test_conditional_previous_candidate_honors_rejection_without_current_scope(archive):
    first = replay(archive, archive.parent / "first", leagues=["48"])
    candidate = next(
        e for entries in first["apif"].values() for e in entries.values() if e.get("candidate_id")
    )
    assert candidate["evidence_status"] == "conditional_verified"
    result = replay(
        archive,
        archive.parent / "second",
        leagues=["39"],
        previous_candidates=archive.parent / "first/apif.json",
        reject=[candidate["candidate_id"]],
    )
    kept = result["apif"]["英联赛杯"][candidate["provider_team_id"]]
    assert kept["evidence_status"] == "rejected", kept["evidence_status"]


def test_equivalent_boundary_claims_require_actual_handling(bundle):
    from datetime import datetime, timedelta

    from test_t68_daily_gate_entry import CUTOFF, document, ref
    from zucai.identity_daily_gate import RISKS, _boundary_cases

    archive, root, evidence = bundle
    facts = reader.load_archive(archive, leagues=["98"])
    facts.update(configs={"98": {"league": "日职"}}, gate_cutoff=CUTOFF)
    base = json.loads(
        (
            archive / "state/catalogue/backfill/backfill/api-football/day/fixtures_one.json"
        ).read_bytes()
    )
    cases = []
    for category in sorted(RISKS):
        doc = deepcopy(base)
        safe = doc["response"][0]
        risky = deepcopy(safe)
        risky["fixture"]["id"] = 20
        if category == "partial_pages":
            doc["paging"]["total"] = 2
        elif category == "unbound":
            risky["teams"]["home"]["id"] = 999999
        elif category == "reversed":
            risky["teams"]["home"], risky["teams"]["away"] = (
                risky["teams"]["away"],
                risky["teams"]["home"],
            )
        elif category == "rejected":
            risky["fixture"]["status"]["short"] = "AWD"
        elif category == "cross_day":
            when = datetime.fromisoformat(risky["fixture"]["date"]) + timedelta(days=1)
            risky["fixture"].update(date=when.isoformat(), timestamp=int(when.timestamp()))
        doc["response"].append(risky)
        doc["results"] = 2
        path = raw(
            root,
            f"boundary/{category}.json",
            doc,
            "https://v3.football.api-sports.io/fixtures?league=98&season=2027",
        )
        reference = ref(root, path)
        cases.append(
            dict(
                category=category,
                fixture_ids=["apif:10"],
                original=reference,
                source_sha256=reference["sha256"],
                outcome="FAILED; NOT EXECUTED",
            )
        )
    document(root, "boundary-cases.json", cases)
    accepted = False
    try:
        _boundary_cases(facts, "98", cases, root)
        accepted = True
    except ValueError:
        pass
    assert not accepted, (
        "Unsupported/nonexecuted boundary claims accepted; selected IDs may be safe rows."
    )


@pytest.mark.parametrize("alter", [None, "changed", "removed", "added"])
def test_actual_scoreboard_inventory_is_preserved(bundle, alter):
    from hashlib import sha256

    from test_t68_daily_gate_entry import change

    archive, root, evidence = bundle
    files = {
        "scored.jsonl": b"",
        "ledger.jsonl": b'{"synthetic_offline":"original"}\n',
        "scoreboard.md": b"offline fixture\n",
    }
    files["summary.json"] = json.dumps(
        dict(schema_version=1, files={p: sha256(v).hexdigest() for p, v in files.items()})
    ).encode()
    for folder in [
        archive,
        root / "preimport",
        root / "after",
        root / "restored",
        root / "daily-after",
    ]:
        (folder / "scoreboard").mkdir(exist_ok=True)
        for name, data in files.items():
            (folder / "scoreboard" / name).write_bytes(data)
    for key in ["before_repeat", "after_repeat", "restored"]:
        change(
            root,
            evidence["persistence"][key],
            lambda d: d["immutable_sha256"].update(
                {f"scoreboard/{p}": sha256(v).hexdigest() for p, v in files.items()}
            ),
        )
    if alter == "changed":
        (root / "restored/scoreboard/ledger.jsonl").write_bytes(
            b'{"synthetic_offline":"changed"}\n'
        )
    elif alter == "removed":
        (root / "restored/scoreboard/ledger.jsonl").unlink()
    elif alter == "added":
        (root / "restored/scoreboard/unexpected.json").write_text("{}")
    result = run(bundle)
    assert (result["status"] == "ready_for_review") == (alter is None), result.get("evidence", {})


def test_equivalent_boundary_samples_are_reexecuted(bundle):
    """Six actual isolated importer/guard runs, then dishonest saved outcomes fail."""
    import shutil
    from datetime import datetime, timedelta
    from hashlib import sha256

    from sqlalchemy.orm import Session

    from test_t68_daily_gate_entry import CUTOFF, document, recent_importer_bundle, ref
    from zucai.identity_daily_gate import RISKS, _boundary_cases
    from zucai.identity_gate_evidence import export, outcome, restored_memory
    from zucai.sources.api_football import ApiFootball
    from zucai.sources.apif_ingest import import_apif_games

    archive, root, evidence = bundle
    facts = reader.load_archive(archive, leagues=["98"])
    facts.update(configs={"98": {"league": "日职"}}, gate_cutoff=CUTOFF)
    template = json.loads(
        (
            archive / "state/catalogue/backfill/backfill/api-football/day/fixtures_one.json"
        ).read_bytes()
    )
    cases = []
    for category in sorted(RISKS):
        doc = deepcopy(template)
        row = doc["response"][0]
        row["fixture"]["id"] = 20
        if category == "partial_pages":
            doc["paging"]["total"] = 2
        elif category == "unbound":
            row["teams"]["home"]["id"] = 999999
        elif category == "reversed":
            row["teams"]["home"], row["teams"]["away"] = row["teams"]["away"], row["teams"]["home"]
        elif category == "rejected":
            row["fixture"]["status"]["short"] = "AWD"
        else:
            second = deepcopy(row)
            second["fixture"]["id"] = 21
            if category == "cross_day":
                when = datetime.fromisoformat(second["fixture"]["date"]) + timedelta(days=1)
                second["fixture"].update(date=when.isoformat(), timestamp=int(when.timestamp()))
            doc["response"].append(second)
            doc["results"] = 2
        path = raw(
            root,
            f"actual-boundaries/{category}.json",
            doc,
            "https://v3.football.api-sports.io/fixtures?league=98&season=2027",
        )
        original = ref(root, path)
        recent = deepcopy(evidence["recent_replay"])
        recent["originals"] = [original]
        before = export(root, recent["before_manifest"])
        if category == "partial_pages":
            folder = root / f"boundary-after-{category}"
            shutil.copytree(before["folder"], folder)
            recent["after_manifest"] = ref(root, folder / "feed_manifest.json")
            pending = {
                f"apif:{r['fixture']['id']}": "partial_pages_rejected" for r in doc["response"]
            }
        else:
            # Generate saved exports using the ordinary importer, independently
            # of the verifier; its second execution must reproduce them exactly.
            recent_importer_bundle((archive, root, dict(evidence, recent_replay=recent)))
            folder = root / f"boundary-after-{category}"
            shutil.copytree(root / "daily-after", folder)
            recent["after_manifest"] = ref(root, folder / "feed_manifest.json")
            options = json.loads((root / recent["replay_options"]["path"]).read_bytes())
            options["after_manifest_sha256"] = recent["after_manifest"]["sha256"]
            recent["replay_options"] = document(root, f"{category}-options.json", options)
            parser = ApiFootball(leagues={"98": {"league": "日职"}})
            try:
                games = parser.parse(path.read_bytes())
                pending = {f"apif:{r['provider_match_id']}": "rejected" for r in parser.rejected}
            finally:
                parser.session.close()
            with restored_memory(before) as (engine, _), Session(engine) as session:
                result = import_apif_games(
                    session,
                    games,
                    seen=datetime.fromisoformat(CAPTURED),
                    assume=timedelta(hours=3),
                    aliases=json.loads(reader.FORMAL.read_bytes()),
                    retain_results=False,
                    imported_utc=datetime.fromisoformat(CUTOFF),
                )
                for key, label in [
                    ("held", "held_unbound"),
                    ("suspected", "suspected"),
                    ("rejected", "rejected"),
                ]:
                    pending.update({f"apif:{r['provider_match_id']}": label for r in result[key]})
        after = export(root, recent["after_manifest"])
        outcomes = []
        for r in doc["response"]:
            mid = f"apif:{r['fixture']['id']}"
            old, new = outcome(before, mid), outcome(after, mid)
            label = pending.get(mid)
            if label is None:
                label = (
                    "inserted"
                    if mid not in before["matches"] and mid in after["matches"]
                    else "unchanged"
                    if old == new
                    else "updated"
                    if old
                    else "inserted"
                    if new[0]["match_id"] == mid
                    else "linked"
                )
            outcomes.append(dict(fixture_id=mid, outcome=label))
        recent["results"] = document(
            root,
            f"{category}-outcomes.json",
            dict(outcomes=outcomes, inserted=sum(r["outcome"] == "inserted" for r in outcomes)),
        )
        cases.append(
            dict(
                category=category,
                fixture_ids=[r["fixture_id"] for r in outcomes],
                source_sha256=original["sha256"],
                original=original,
                outcome="verified",
                replay=recent,
            )
        )
    _boundary_cases(facts, "98", cases, root)
    # The verifier must reject real-looking labels when the importer held a row.
    held = next(c for c in cases if c["category"] == "unbound")
    report = json.loads((root / held["replay"]["results"]["path"]).read_bytes())
    assert report["outcomes"][0]["outcome"] == "suspected"
    report["outcomes"][0]["outcome"] = "rejected"
    held["replay"]["results"] = document(root, "dishonest-boundary.json", report)
    with pytest.raises(ValueError, match="actual importer"):
        _boundary_cases(facts, "98", cases, root)
    # Every proof really points at the source bytes, not a label-only attachment.
    assert all(
        sha256((root / c["original"]["path"]).read_bytes()).hexdigest() == c["source_sha256"]
        for c in cases
    )
