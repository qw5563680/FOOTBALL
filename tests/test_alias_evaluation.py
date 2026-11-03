import json
from copy import deepcopy
from pathlib import Path

from test_alias_proposals import inventory
from test_alias_proposals import proposal_archive as proposal_archive
from test_cloud_apis import environment as environment
from zucai.cli import main

ROOT = Path(__file__).parents[1]
FIXTURE = json.loads((ROOT / "data/fixtures/t23/evaluation.json").read_bytes())


def data():
    return dict(
        audits={
            "identity_matches": deepcopy(FIXTURE["games"]),
            "identity_scopes": [
                {
                    key: row[key]
                    for key in (
                        "league_id",
                        "league",
                        "season",
                        "raw_sha256",
                        "complete",
                        "scope_size",
                    )
                }
                for row in FIXTURE["games"]
            ],
        },
        matches=deepcopy(FIXTURE["official"]),
    )


def test_two_consistent_matches_are_compared_with_existing_strict_graph_not_double_counted():
    from zucai.alias_evaluation import evaluate_rules

    source = data()
    original = deepcopy(source)
    result = evaluate_rules(source, {}, {"3": {"league": "欧罗巴"}})
    assert result["status"] == "complete"
    assert result["two_match_candidates"] == 2
    assert result["already_strict"] == 2 and result["additional_teams"] == 0
    assert not result["eligible_for_next_task"]
    assert source == original


def test_ambiguous_time_window_and_inconsistent_names_cannot_authorize_relaxation():
    from zucai.alias_evaluation import evaluate_rules

    source = data()
    source["matches"][1]["home"] = "合成丙"
    result = evaluate_rules(source, {}, {"3": {"league": "欧罗巴"}})
    assert result["conflicts"] and not result["eligible_for_next_task"]
    source = data()
    source["matches"].append(dict(source["matches"][0], match_id="jc:competing"))
    result = evaluate_rules(source, {}, {"3": {"league": "欧罗巴"}})
    assert result["two_match_candidates"] == 0


def test_missing_scope_cannot_be_reported_as_a_zero_conflict_success():
    from zucai.alias_evaluation import evaluate_rules

    source = data()
    source["audits"]["identity_matches"][1]["scope_size"] = "2"
    result = evaluate_rules(source, {}, {"3": {"league": "欧罗巴"}})
    assert result["status"] == "insufficient_evidence"
    assert result["missing_scopes"] == ["3/2026"]
    assert not result["eligible_for_next_task"]


def test_evaluation_cli_is_read_only_and_discloses_old_archive_limitations(
    proposal_archive, tmp_path
):
    before = inventory(proposal_archive)
    out = tmp_path / "evaluation.json"
    assert (
        main(
            [
                "aliases",
                "evaluate",
                "--archive",
                str(proposal_archive),
                "--leagues",
                "3",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    report = json.loads(out.read_bytes())
    assert (
        report["status"] == "insufficient_evidence" and report["production_rule_changed"] is False
    )
    assert inventory(proposal_archive) == before


def test_latest_scope_replay_and_empty_capture_cannot_use_stale_success(tmp_path):
    import csv
    from types import SimpleNamespace

    from zucai.alias_evaluation import evaluate_rules
    from zucai.apif_identity_metrics import record_matches
    from zucai.sources.fd_verify import _utc

    path = tmp_path / "apif_identity_matches.csv"

    def capture(row, complete=True):
        game = SimpleNamespace(
            provider_match_id=int(row["provider_match_id"]),
            competition_code=row["league_id"],
            league=row["league"],
            kickoff_utc=_utc(row["kickoff_utc"]),
            home_provider_id=int(row["home_provider_id"]),
            away_provider_id=int(row["away_provider_id"]),
            home_raw=row["home"],
            away_raw=row["away"],
        )
        return dict(
            games=[game],
            scope={"league_id": row["league_id"], "season": int(row["season"])},
            complete=complete,
            raw_sha256=row["raw_sha256"],
        )

    first, second = [capture(row) for row in FIXTURE["games"]]
    record_matches(path, [first, second])
    changed = deepcopy(second)
    changed["raw_sha256"] = "c" * 64
    changed["games"][0].kickoff_utc = changed["games"][0].kickoff_utc.replace(month=9)
    record_matches(path, [changed])
    record_matches(path, [second])  # Re-observing an old hash must become latest.

    def evaluate():
        source = data()
        source["audits"]["identity_matches"] = list(csv.DictReader(path.open()))
        scopes = path.with_name("apif_identity_scopes.csv")
        if scopes.exists():
            source["audits"]["identity_scopes"] = list(csv.DictReader(scopes.open()))
        return evaluate_rules(source, {}, {"3": {"league": "欧罗巴"}})

    assert evaluate()["two_match_candidates"] == 2
    record_matches(path, [{**second, "games": [], "complete": False, "raw_sha256": "d" * 64}])
    result = evaluate()
    assert result["status"] == "insufficient_evidence" and result["missing_scopes"] == ["3/2026"]
