"""Default-strength probabilities remain research evidence, never SFC main picks."""

import copy
import csv
import hashlib
import io
import json
from datetime import timedelta

import pytest

from test_sfc_report import NOW, build
from test_sfc_report import complete_issue as complete_issue
from zucai.report.sfc import _csv, _markdown, disclosed_rows, pick_nine_lines
from zucai.scoreboard.sfc import read_settled, render_diagnostics, settle
from zucai.sfc_digest import _issue_lines

METHOD = "weakest_quartile_half_shrinkage"


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False).encode()


def metadata(home=False, away=False):
    return dict(
        unknown_team_prior=home or away,
        home_unknown_team=home,
        away_unknown_team=away,
        prior_method=METHOD if home or away else None,
    )


def published(report):
    return {
        **report,
        "sale_state": "published_before_deadline",
        "published_utc": NOW.isoformat(),
        "publication_commit": "a" * 40,
        "sale_end_utc": report["source"]["sale_end_utc"],
    }


def legacy_unknown_report(complete_issue, *, home=False, away=True):
    engine, issue, batch = complete_issue
    report = published(build(engine, issue, batch))
    # Model-era SFC rows carry no disclosure; the original forecast is authoritative.
    for row in report["rows"]:
        row.pop("prior_disclosure", None)
    report["forecast_batch"]["predictions"][0]["metadata"] = metadata(home, away)
    return issue, report


@pytest.mark.parametrize("home,away", [(False, True), (True, False), (True, True)])
def test_sfc_retains_prior_probabilities_but_excludes_confidence_ranking(
    complete_issue, home, away
):
    engine, issue, batch = complete_issue
    baseline = build(engine, issue, batch)
    for row in batch["predictions"]:
        row["metadata"] = metadata()
    batch["predictions"][0]["metadata"] = metadata(home, away)
    before = canonical(batch)
    report = build(engine, issue, batch)
    assert report["rows"][0]["probabilities"] == baseline["rows"][0]["probabilities"]
    assert report["combo"] == baseline["combo"]
    assert report["confidence_order"] == baseline["confidence_order"][1:]
    evidence = report["rows"][0]["prior_disclosure"]
    assert evidence["used"] is True
    assert evidence["home"] is home and evidence["away"] is away
    assert evidence["method"] == METHOD
    assert canonical(batch) == before


def test_old_sfc_report_discloses_prior_and_pick_nine_excludes_it_without_mutation(
    complete_issue,
):
    issue, report = legacy_unknown_report(complete_issue)
    original = canonical(report)
    lines = "\n".join(pick_nine_lines(report))
    assert "| 1 |" not in lines
    assert "| 10 |" in lines
    assert "默认实力" in lines and "客队" in lines and METHOD in lines.replace(r"\_", "_")
    rendered = _markdown(report).decode()
    order = next(line for line in rendered.splitlines() if "单场最高项置信度顺序" in line)
    assert "：2、3、4、5、6、7、8、9、10、11、12、13、14。" in order
    assert "默认实力" in rendered and "客队" in rendered and METHOD in rendered.replace(r"\_", "_")
    rows = list(csv.DictReader(io.StringIO(_csv(report).decode("utf-8-sig"))))
    assert rows[0]["p_home"] == "0.5"
    assert "默认实力" in rows[0]["reason"] and METHOD in rows[0]["reason"]
    assert canonical(report) == original


def test_pick_nine_diagnoses_insufficient_known_team_rows(complete_issue):
    _, report = legacy_unknown_report(complete_issue)
    for row in report["forecast_batch"]["predictions"][:6]:
        row["metadata"] = metadata(True, True)
    lines = "\n".join(pick_nine_lines(report))
    assert "只有 8 场" in lines
    assert "默认实力" in lines and METHOD in lines.replace(r"\_", "_")
    assert "| 原场号 |" not in lines


def test_known_sfc_probabilities_and_pick_nine_eligibility_are_unchanged(complete_issue):
    engine, issue, batch = complete_issue
    baseline = published(build(engine, issue, batch))
    for row in batch["predictions"]:
        row["metadata"] = metadata()
    report = published(build(engine, issue, batch))
    assert report["confidence_order"] == baseline["confidence_order"]
    assert report["combo"] == baseline["combo"]
    assert [r["probabilities"] for r in report["rows"]] == [
        r["probabilities"] for r in baseline["rows"]
    ]
    assert pick_nine_lines(report) == pick_nine_lines(baseline)


def test_daily_sfc_reads_prior_from_old_embedded_forecast(complete_issue):
    issue, report = legacy_unknown_report(complete_issue)
    state = {
        "issue_no": issue.issue_no,
        "sale_end_utc": issue.sale_end_utc,
        "snapshot": copy.deepcopy(issue.snapshot),
    }
    original = canonical(report)
    lines = _issue_lines("state/sfc/test.json", state, [("sfc/test", report)], [], NOW, str)
    text = "\n".join(lines)
    assert "默认实力" in text and "客队" in text and METHOD in text.replace(r"\_", "_")
    row = next(line for line in lines if line.startswith("| 1 |"))
    assert "50.00%" in row.replace(r"\.", ".") and "默认实力" in row
    assert canonical(report) == original


def archived_legacy_settlement(complete_issue, tmp_path):
    _, report = legacy_unknown_report(complete_issue)
    report["forecast_batch"]["predictions"][1]["metadata"] = metadata()
    archive = tmp_path / "settlement-archive"
    source = archive / "sfc/old/sfc.json"
    source.parent.mkdir(parents=True)
    source.write_bytes(canonical(report))
    rows = [
        {
            **{k: row[k] for k in ("seq", "match_id", "probabilities", "top_outcome", "selection")},
            "home": row["matched_identity"]["home"],
            "away": row["matched_identity"]["away"],
            "actual": "h",
            "single_hit": True,
            "available_utc": (NOW + timedelta(days=19)).isoformat(),
        }
        for row in report["rows"]
    ]
    record = dict(
        issue_no=report["issue_no"],
        report_path="sfc/old/sfc.json",
        report_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        report_generated_utc=report["generated_utc"],
        settled_utc=(NOW + timedelta(days=19)).isoformat(),
        single_hits=14,
        single_evaluated=14,
        combo_all_hit=True,
        rows=rows,
    )
    raw = canonical(record) + b"\n"
    (archive / "sfc/settled.jsonl").write_bytes(raw)
    return archive, report, record, raw


def test_old_sfc_settlement_is_stratified_without_rewriting_records(complete_issue, tmp_path):
    archive, report, record, raw = archived_legacy_settlement(complete_issue, tmp_path)
    original = canonical(record)
    rows, actual_raw = read_settled(
        archive, {"sfc": {"settled_sha256": hashlib.sha256(raw).hexdigest()}}
    )
    assert actual_raw == raw
    assert rows[0]["prior_groups"] == {
        "default_strength": {"n": 1, "evaluated": 1, "hits": 1},
        "known_teams": {"n": 1, "evaluated": 1, "hits": 1},
        "unrecorded": {"n": 12, "evaluated": 12, "hits": 12},
    }
    assert rows[0]["rows"][0]["prior_disclosure"]["away"] is True
    assert (archive / "sfc/settled.jsonl").read_bytes() == raw
    assert (archive / "sfc/old/sfc.json").read_bytes() == canonical(report)
    assert canonical(record) == original


def test_sfc_scoreboard_groups_old_and_new_rows_without_changing_totals(
    complete_issue, tmp_path, monkeypatch
):
    import zucai.scoreboard.sfc as module

    archive, report, record, raw = archived_legacy_settlement(complete_issue, tmp_path)
    monkeypatch.setattr(module, "read_reports", lambda archive: [("sfc/old", report)])
    at = NOW + timedelta(days=20)
    added, summary = settle(archive, {}, [record], at)
    assert added == []
    assert summary["issues"] == 1 and summary["average_single_hits"] == 14
    assert summary["prior_groups"]["default_strength"] == {"n": 1, "evaluated": 1, "hits": 1}
    assert summary["prior_groups"]["unrecorded"]["n"] == 12
    text = render_diagnostics({"sfc": summary})
    assert "默认实力" in text and "双方已知" in text and "未记录" in text
    results = {
        row["match_id"]: {
            **row["matched_identity"],
            "status": "finished",
            "home_goals": 1,
            "away_goals": 0,
            "available_source": "captured",
            "available_utc": (at - timedelta(days=1)).isoformat(),
        }
        for row in report["rows"]
    }
    added, fresh = settle(archive, results, [], at)
    assert len(added) == 1
    assert added[0]["rows"][0]["prior_disclosure"]["away"] is True
    assert fresh["prior_groups"] == summary["prior_groups"]
    assert added[0]["single_hits"] == record["single_hits"] == 14
    assert (archive / "sfc/settled.jsonl").read_bytes() == raw


def test_sfc_settlement_does_not_trust_changed_source_report(complete_issue, tmp_path):
    archive, _, _, raw = archived_legacy_settlement(complete_issue, tmp_path)
    (archive / "sfc/old/sfc.json").write_text("{}")
    with pytest.raises(ValueError, match="引用报告 SHA256"):
        read_settled(archive, {"sfc": {"settled_sha256": hashlib.sha256(raw).hexdigest()}})


def test_conflicting_frozen_known_marker_does_not_clear_prior_exclusion(complete_issue):
    _, report = legacy_unknown_report(complete_issue)
    report["rows"][0]["prior_disclosure"] = {
        "used": True,
        "home": False,
        "away": True,
        "method": METHOD,
        "evidence": "explicit_sides",
    }
    report["forecast_batch"]["predictions"][0]["metadata"] = metadata()
    assert disclosed_rows(report)[0]["prior_disclosure"]["used"] is True
    assert "| 1 |" not in "\n".join(pick_nine_lines(report))


@pytest.mark.parametrize("mismatch", ["identity", "probabilities"])
def test_old_sfc_side_evidence_requires_the_same_identity_and_probability(complete_issue, mismatch):
    _, report = legacy_unknown_report(complete_issue)
    original = report["forecast_batch"]["predictions"][0]
    if mismatch == "identity":
        original["away"] = "另一支球队"
    else:
        original["model_probabilities"] = {"h": 0.4, "d": 0.3, "a": 0.3}
    assert disclosed_rows(report)[0]["prior_disclosure"]["used"] is None


def test_old_settlement_keeps_affirmative_prior_and_previous_issue_strata(complete_issue, tmp_path):
    archive, report, record, _ = archived_legacy_settlement(complete_issue, tmp_path)
    record["rows"][1]["prior_disclosure"] = {
        "used": True,
        "home": True,
        "away": False,
        "method": METHOD,
        "evidence": "explicit_sides",
    }
    raw = canonical(record) + b"\n"
    (archive / "sfc/settled.jsonl").write_bytes(raw)
    records, _ = read_settled(archive, {"sfc": {"settled_sha256": hashlib.sha256(raw).hexdigest()}})
    assert records[0]["rows"][1]["prior_disclosure"]["used"] is True
    state = {
        "issue_no": str(int(report["issue_no"]) + 1),
        "sale_end_utc": report["sale_end_utc"],
        "snapshot": copy.deepcopy(report["rows"]),
    }
    lines = _issue_lines("state/sfc/next.json", state, [], records, NOW + timedelta(days=20), str)
    assert "使用默认实力 2 场（已计分 2、命中 2）" in "\n".join(lines)
    assert "未记录 12 场" in "\n".join(lines)
    assert (archive / "sfc/settled.jsonl").read_bytes() == raw
