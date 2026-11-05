"""T39 deadline decisions use first remote-reachable report commits, never calculation time."""

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from sfc_publication_support import REF, commit_report, git, prepare_remote
from test_sfc_daily import files, freeze
from test_sfc_report import complete_issue as complete_issue
from zucai.cloud_feeds import export_feeds
from zucai.db import session_scope
from zucai.models import SfcIssue
from zucai.sfc_digest import section
from zucai.sfc_reports import publish_reports, read_reports

CASES = json.loads(
    (Path(__file__).parents[1] / "data/fixtures/t39/sfc-publication-cases.json").read_bytes()
)
GENERATED = datetime.fromisoformat(CASES["generated_utc"])
NOW = datetime(2026, 11, 2, 10, tzinfo=UTC)


@pytest.fixture
def publication_archive(complete_issue, tmp_path):
    engine, _, batch = complete_issue
    with session_scope(engine) as session:
        session.get(SfcIssue, "26156").sale_end_utc = CASES["sale_end_utc"]
    archive = tmp_path / "publication"
    export_feeds(Path(engine.url.database), archive, date(2026, 11, 2), "offline")
    freeze(archive, tmp_path / "forecasts", batch, "early", GENERATED)
    prepare_remote(archive, "2026-11-02T09:28:00+00:00")
    return archive


def digest(archive, now=NOW):
    feed = json.loads((archive / "feed_manifest.json").read_bytes())
    return "\n".join(section(archive, feed, {}, now, lambda name: name))


def test_candidate_retains_probabilities_but_cannot_claim_frozen_publication(publication_archive):
    archive = publication_archive
    forecasts = files(archive / "forecasts")
    assert publish_reports(archive, now=GENERATED) == ["26156"]
    report = json.loads((archive / "sfc/26156/sfc.json").read_bytes())
    manifest = json.loads((archive / "sfc/26156/manifest.json").read_bytes())
    for document in (report, manifest):
        assert document["generated_utc"] == CASES["generated_utc"]
        assert document["published_utc"] is None
        assert datetime.fromisoformat(document["sale_end_utc"]) == datetime.fromisoformat(
            CASES["sale_end_utc"]
        )
        assert document["sale_state"] == "deadline_unconfirmed"
    assert all(row["probabilities"] == {"h": 0.5, "d": 0.3, "a": 0.2} for row in report["rows"])
    assert read_reports(archive) == []
    assert len(read_reports(archive, include_unconfirmed=True)) == 1
    for text in ((archive / "sfc/26156/report.md").read_text(), digest(archive)):
        assert "无法确认赶上停售（算完 17:29，存好时间未知）" in text
        assert text.count("无冻结概率") == 14
        assert "复式" not in text and "3+1+0" not in text and r"50\.00%" not in text
    csv = (archive / "sfc/26156/report.csv").read_text(encoding="utf-8-sig")
    assert "selection" not in csv.splitlines()[0]
    assert files(archive / "forecasts") == forecasts


@pytest.mark.parametrize("case", CASES["cases"])
def test_only_first_remote_publication_before_deadline_qualifies(publication_archive, case):
    from zucai.sfc_reports import confirm_publication

    archive = publication_archive
    publish_reports(archive, now=GENERATED)
    candidate = files(archive / "sfc/26156/history/early")
    original_forecasts = files(archive / "forecasts")
    commit = None
    if case["published_utc"]:
        commit = commit_report(archive, case["published_utc"])
    summary = archive.parent / "summary.md"
    confirmed = confirm_publication(archive, published_ref=REF, summary=summary)
    assert bool(confirmed) is bool(commit)
    report = json.loads((archive / "sfc/26156/sfc.json").read_bytes())
    manifest = json.loads((archive / "sfc/26156/manifest.json").read_bytes())
    for document in (report, manifest):
        assert document["generated_utc"] == CASES["generated_utc"]
        assert document["published_utc"] == case["published_utc"]
        assert datetime.fromisoformat(document["sale_end_utc"]) == datetime.fromisoformat(
            CASES["sale_end_utc"]
        )
        assert document["sale_state"] == case["status"]
        assert document["publication_commit"] == commit
    if commit:
        evidence = report["publication_evidence"]
        assert evidence["candidate_path"] == "sfc/26156/history/early/sfc.json"
        assert evidence["candidate_sha256"] == hashlib.sha256(candidate["sfc.json"]).hexdigest()
        assert evidence["commit"] == commit
    text = digest(archive)
    markdown = (archive / "sfc/26156/report.md").read_text()
    if case["status"] == "published_before_deadline":
        assert "复式项" in text and r"50\.00%" in text
        assert len(read_reports(archive)) == 1
        assert "算完 17:29" in text and "存好 17:29" in text and "停售 17:30" in text
    else:
        for output in (text, markdown):
            assert output.count("无冻结概率") == 14
            assert "复式" not in output and "3+1+0" not in output and r"50\.00%" not in output
        assert read_reports(archive) == []
        if commit:
            clock = "17:32" if case["published_utc"].endswith("09:32:00+00:00") else "17:30"
            assert f"本期错过停售（停售 17:30，算完 17:29，存好 {clock}）" in text
    assert files(archive / "sfc/26156/history/early") == candidate
    assert files(archive / "forecasts") == original_forecasts
    assert (
        "算完" in summary.read_text()
        and "存好" in summary.read_text()
        and "停售" in summary.read_text()
    )
    before = files(archive)
    assert confirm_publication(archive, published_ref=REF) == []
    assert files(archive) == before


def test_local_unpushed_commit_is_not_publication_evidence(publication_archive):
    from zucai.sfc_reports import confirm_publication

    archive = publication_archive
    publish_reports(archive, now=GENERATED)
    commit_report(archive, "2026-11-02T09:29:30+00:00", push=False)
    assert confirm_publication(archive, published_ref=REF) == []
    assert "无法确认赶上停售" in digest(archive)
    with pytest.raises(ValueError, match="remote|远程"):
        confirm_publication(archive, published_ref="HEAD")


def test_digest_can_read_git_evidence_when_confirmation_push_failed(publication_archive):
    archive = publication_archive
    publish_reports(archive, now=GENERATED)
    commit_report(archive, "2026-11-02T09:32:00+00:00")
    before = files(archive)
    assert "本期错过停售（停售 17:30，算完 17:29，存好 17:32）" in digest(archive)
    assert files(archive) == before


def test_later_commits_do_not_move_first_publication_time(publication_archive):
    from zucai.sfc_reports import confirm_publication

    archive = publication_archive
    publish_reports(archive, now=GENERATED)
    first_commit = commit_report(archive, "2026-11-02T09:29:30+00:00")
    commit_report(archive, "2026-11-02T09:32:00+00:00")
    confirm_publication(archive, published_ref=REF)
    report = json.loads((archive / "sfc/26156/sfc.json").read_bytes())
    assert report["publication_commit"] == first_commit
    assert report["published_utc"] == "2026-11-02T09:29:30+00:00"
    assert report["sale_state"] == "published_before_deadline"
    # A digest cutoff before remote publication must not expose those probabilities.
    assert r"50\.00%" not in digest(archive, now=GENERATED)


@pytest.mark.parametrize(
    "published,accepted",
    [("2026-11-02T09:29:00+00:00", True), ("2026-11-02T09:28:59+00:00", False)],
)
def test_git_second_precision_accepts_only_same_second_truncation(
    publication_archive, published, accepted
):
    from zucai.sfc_publication import with_publication
    from zucai.sfc_reports import confirm_publication

    archive = publication_archive
    generated = datetime(2026, 11, 2, 9, 29, 0, 750000, tzinfo=UTC)
    publish_reports(archive, now=generated)
    candidate = files(archive / "sfc/26156/history/early")
    commit = commit_report(archive, published)
    # Exercise both read-only digest recovery and durable confirmation against
    # the real local remote's second-precision committer timestamp.
    before = digest(archive)
    assert ("已确认赶上停售" in before) is accepted
    assert bool(confirm_publication(archive, published_ref=REF)) is accepted
    report = json.loads((archive / "sfc/26156/sfc.json").read_bytes())
    manifest = json.loads((archive / "sfc/26156/manifest.json").read_bytes())
    for document in (report, manifest):
        assert document["generated_utc"] == "2026-11-02T09:29:00.750000+00:00"
        assert document["published_utc"] == (published if accepted else None)
    assert files(archive / "sfc/26156/history/early") == candidate
    assert bool(read_reports(archive)) is accepted
    if not accepted:
        evidence = {
            "candidate_path": "sfc/26156/history/early/sfc.json",
            "candidate_sha256": hashlib.sha256(candidate["sfc.json"]).hexdigest(),
            "commit": commit,
            "published_utc": published,
            "ref": REF,
        }
        with pytest.raises(ValueError, match="早于概率计算完成时间"):
            with_publication(report, evidence)


def test_fractional_publication_timestamp_does_not_get_git_truncation_tolerance(
    publication_archive,
):
    from zucai.sfc_publication import with_publication

    archive = publication_archive
    publish_reports(archive, now=datetime(2026, 11, 2, 9, 29, 0, 750000, tzinfo=UTC))
    report = json.loads((archive / "sfc/26156/sfc.json").read_bytes())
    with pytest.raises(ValueError, match="早于概率计算完成时间"):
        with_publication(report, {"published_utc": "2026-11-02T09:29:00.500000+00:00"})


def test_first_commit_with_different_bytes_cannot_certify_local_candidate(publication_archive):
    from zucai.report.sfc import write_sfc_report
    from zucai.sfc_reports import confirm_publication

    archive = publication_archive
    publish_reports(archive, now=GENERATED)
    history = archive / "sfc/26156/history/early"
    candidate = files(history)
    # A different, hash-consistent report was available remotely.
    report = json.loads(candidate["sfc.json"])
    report["run_id"] = "different-evidence"
    alternate = archive.parent / "alternate"
    write_sfc_report(report, alternate)
    for name, raw in files(alternate).items():
        (history / name).write_bytes(raw)
        (archive / "sfc/26156" / name).write_bytes(raw)
    commit_report(archive, "2026-11-02T09:29:30+00:00")
    for name, raw in candidate.items():
        (history / name).write_bytes(raw)
        (archive / "sfc/26156" / name).write_bytes(raw)
    assert confirm_publication(archive, published_ref=REF) == []
    assert "无法确认赶上停售" in digest(archive)
    assert git(archive, "status", "--porcelain")


def test_calculation_timestamp_records_completion_before_export(publication_archive, monkeypatch):
    import zucai.sfc_reports as reports

    class Clock(datetime):
        instants = iter(
            [
                GENERATED,
                datetime(2026, 11, 2, 9, 29, 10, tzinfo=UTC),
                datetime(2026, 11, 2, 9, 29, 11, tzinfo=UTC),
            ]
        )

        @classmethod
        def now(cls, tz):
            return cls.fromtimestamp(next(cls.instants).timestamp(), tz)

    monkeypatch.setattr(reports, "datetime", Clock)
    reports.publish_reports(publication_archive)
    report = json.loads((publication_archive / "sfc/26156/sfc.json").read_bytes())
    assert report["generated_utc"] == "2026-11-02T09:29:10+00:00"
    assert report["published_utc"] is None


def test_generation_cli_summary_discloses_unknown_publication(publication_archive, monkeypatch):
    import zucai.sfc_reports as reports

    class Clock(datetime):
        @classmethod
        def now(cls, tz):
            return cls.fromtimestamp(GENERATED.timestamp(), tz)

    summary = publication_archive.parent / "actions-summary.md"
    monkeypatch.setattr(reports, "datetime", Clock)
    monkeypatch.setattr(
        "sys.argv",
        ["sfc_reports", "--archive", str(publication_archive), "--summary", str(summary)],
    )
    reports.main()
    assert summary.exists()
    text = summary.read_text()
    assert "无法确认赶上停售（算完 17:29，存好时间未知）" in text
    assert "算完 UTC `2026-11-02T09:29:00+00:00`" in text
    assert "停售 UTC `2026-11-02T09:30:00.000000Z`" in text


def test_manifest_must_explicitly_record_unknown_publication(publication_archive):
    archive = publication_archive
    publish_reports(archive, now=GENERATED)
    path = archive / "sfc/26156/history/early/manifest.json"
    manifest = json.loads(path.read_bytes())
    del manifest["published_utc"]
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="元数据"):
        read_reports(archive, include_unconfirmed=True)


def test_invalid_publication_schema_cannot_bypass_eligibility(publication_archive, tmp_path):
    from zucai.report.sfc import write_sfc_report

    archive = publication_archive
    publish_reports(archive, now=GENERATED)
    report = json.loads((archive / "sfc/26156/sfc.json").read_bytes())
    report["publication_schema"] = 0
    stage = tmp_path / "invalid-schema"
    write_sfc_report(report, stage)
    for name, raw in files(stage).items():
        (archive / "sfc/26156" / name).write_bytes(raw)
        (archive / "sfc/26156/history/early" / name).write_bytes(raw)
    with pytest.raises(ValueError, match="元数据"):
        read_reports(archive)


@pytest.mark.parametrize("missing", ["remote", "history"])
def test_missing_remote_or_shallow_history_cannot_establish_first_publication(
    publication_archive, missing
):
    from zucai.sfc_reports import confirm_publication

    archive = publication_archive
    publish_reports(archive, now=GENERATED)
    first = commit_report(archive, "2026-11-02T09:29:30+00:00")
    if missing == "remote":
        git(archive, "update-ref", "-d", REF)
    else:
        (archive / ".git/shallow").write_text(first + "\n")
    assert confirm_publication(archive, published_ref=REF) == []
    assert "无法确认赶上停售" in digest(archive)


def test_failed_confirmation_restores_current_and_preserves_candidates(
    publication_archive, monkeypatch
):
    import zucai.sfc_reports as reports

    archive = publication_archive
    publish_reports(archive, now=GENERATED)
    commit_report(archive, "2026-11-02T09:29:30+00:00")
    before = files(archive)
    original = reports._atomic_write
    failed = False

    def fail_once(path, raw):
        nonlocal failed
        if path.name == "manifest.json" and path.parent.name == "26156" and not failed:
            failed = True
            raise OSError("offline confirmation write failure")
        return original(path, raw)

    monkeypatch.setattr(reports, "_atomic_write", fail_once)
    with pytest.raises(OSError, match="confirmation write failure"):
        reports.confirm_publication(archive, published_ref=REF)
    assert files(archive) == before
    assert len(reports.confirm_publication(archive, published_ref=REF)) == 1


def test_confirmation_cannot_change_the_original_probability_evidence(
    publication_archive, tmp_path
):
    from zucai.report.sfc import write_sfc_report
    from zucai.sfc_reports import confirm_publication

    archive = publication_archive
    publish_reports(archive, now=GENERATED)
    commit_report(archive, "2026-11-02T09:29:30+00:00")
    path = confirm_publication(archive, published_ref=REF)[0]
    report = json.loads((archive / path / "sfc.json").read_bytes())
    report["rows"][0]["probabilities"] = {"h": 0.6, "d": 0.2, "a": 0.2}
    rendered = tmp_path / "tampered"
    write_sfc_report(report, rendered)
    for name, raw in files(rendered).items():
        (archive / path / name).write_bytes(raw)
        (archive / "sfc/26156" / name).write_bytes(raw)
    with pytest.raises(ValueError, match="改变了原报告证据"):
        read_reports(archive)


@pytest.mark.parametrize("publish", [False, True])
def test_unconfirmed_or_late_report_never_becomes_a_settled_diagnostic(
    publication_archive, publish
):
    from test_sfc_daily import install_results
    from zucai.scoreboard.command import run
    from zucai.sfc_reports import confirm_publication

    archive = publication_archive
    publish_reports(archive, now=GENERATED)
    if publish:
        commit_report(archive, "2026-11-02T09:32:00+00:00")
        confirm_publication(archive, published_ref=REF)
    database = archive.parent / "db"
    install_results(database)
    export_feeds(database, archive, date(2026, 11, 3), "offline-results")
    summary = run(archive, as_of=datetime(2026, 11, 3, 12, tzinfo=UTC))
    assert summary["sfc"]["issues"] == 0
    assert not (archive / "sfc/settled.jsonl").read_bytes()


@pytest.mark.parametrize("case", [CASES["cases"][0], CASES["cases"][1], CASES["cases"][3]])
def test_legacy_digest_uses_git_without_rewriting_historical_reports(
    publication_archive, tmp_path, case
):
    from zucai.report.sfc import write_sfc_report

    archive = publication_archive
    publish_reports(archive, now=GENERATED)
    report = json.loads((archive / "sfc/26156/sfc.json").read_bytes())
    for key in (
        "publication_schema",
        "published_utc",
        "sale_end_utc",
        "publication_commit",
        "publication_evidence",
    ):
        del report[key]
    report["sale_state"] = "third_party_deadline_pending_verification"
    stage = tmp_path / "legacy"
    write_sfc_report(report, stage)
    for name, raw in files(stage).items():
        (archive / "sfc/26156" / name).write_bytes(raw)
        (archive / "sfc/26156/history/early" / name).write_bytes(raw)
    if case["published_utc"]:
        commit_report(archive, case["published_utc"])
    before = files(archive)
    text = digest(archive)
    if case["status"] == "published_before_deadline":
        assert "已确认赶上停售" in text and r"50\.00%" in text
    else:
        assert text.count("无冻结概率") == 14
        assert "复式" not in text and "3+1+0" not in text
    assert files(archive) == before
