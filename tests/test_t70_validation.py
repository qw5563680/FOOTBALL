"""Independent T70 consistency checks against synthetic offline research records."""

import json
from pathlib import Path

import pytest

from zucai.web_research_validation import parse

FIXTURE = Path(__file__).resolve().parents[1] / "data/fixtures/t70/validation_synthetic.json"


@pytest.fixture
def case():
    return json.loads(FIXTURE.read_bytes())


def check(case, *, odds_threshold=0.05):
    return parse(
        json.dumps(case["structure"], ensure_ascii=False),
        case["material"],
        case["provider_citations"],
        report_text=case["report"],
        odds_threshold=odds_threshold,
    )


def test_report_checks_both_teams_and_does_not_change_the_research_judgement(case):
    status, row, checks = check(case)

    assert status == "ok"
    assert row["cutoff_utc"] == "2026-11-27T11:50:00+00:00"
    assert row["judgment"] == "h" and row["probabilities"] == {"h": 0.47, "d": 0.29, "a": 0.24}
    assert row["basis"][0]["urls"] == ["https://club.example/preview"]
    assert row["citations"] == [
        "https://club.example/preview",
        "https://fixtures.example/match",
        "https://club.example/latest",
    ]
    assert checks["unverified_citations"] == ["https://fixture-extractor.invalid/missing"]
    assert [(r["match_id"], r["reason"]) for r in checks["recent"]["mismatched"]] == [
        ("jc:4800", "score_mismatch")
    ]
    assert len(checks["recent"]["mismatched"][0]["claims"]) == 2
    assert [r["match_id"] for r in checks["recent"]["matched"]] == ["fdorg:4700"]
    assert [r["reason"] for r in checks["recent"]["unknown"]] == ["not_in_archive"]
    assert [r["outcome"] for r in checks["odds"]] == ["h"]
    assert checks["odds"][0]["difference"] == 0.06
    assert {r["reason"] for r in checks["unsupported"]} >= {"no_source", "unknown_publication"}
    assert checks["warning_count"] >= 4


@pytest.mark.parametrize(
    "change, reason", [("reverse", "home_away_reversed"), ("date", "date_mismatch")]
)
def test_reversed_identity_and_wrong_beijing_date_are_specific_recent_mismatches(
    case, change, reason
):
    recent = case["structure"]["recent"][0]
    if change == "reverse":
        recent["home"], recent["away"] = recent["away"], recent["home"]
        recent["home_goals"], recent["away_goals"] = recent["away_goals"], recent["home_goals"]
    else:
        recent["date_beijing"] = "2026-11-20"
    case["structure"]["recent"] = [recent]
    status, _, checks = check(case)

    assert status == "ok"
    assert [(r["match_id"], r["reason"]) for r in checks["recent"]["mismatched"]] == [
        ("jc:4800", reason)
    ]


def test_ambiguous_repeat_pair_and_non_jc_odds_are_not_claimed_as_verified(case):
    record = case["material"]["recent"]["home"][0]
    case["material"]["recent"]["home"].append(
        {**record, "match_id": "fd:duplicate", "home_goals": 2, "source": "fd"}
    )
    case["structure"]["market"]["operator"] = "other"
    status, _, checks = check(case)

    assert status == "ok"
    assert {r["reason"] for r in checks["recent"]["unknown"]} == {
        "ambiguous_archive",
        "not_in_archive",
    }
    assert checks["odds"] == []
    assert checks["odds_status"] == "different_or_unknown_operator"


def test_recent_claims_can_be_checked_against_older_archived_evidence(case):
    case["material"]["verification_recent"] = {
        "home": [
            {
                "match_id": "fd:4600",
                "kickoff_utc": "2026-11-16T12:00:00Z",
                "home": "北城",
                "away": "西城",
                "home_goals": 1,
                "away_goals": 1,
                "source": "fd",
                "available_utc": "2026-11-16T18:00:00Z",
            },
            {
                "match_id": "fd:4601",
                "kickoff_utc": "2026-11-16T12:00:00Z",
                "home": "北城",
                "away": "西城",
                "home_goals": 4,
                "away_goals": 4,
                "source": "fd",
                "available_utc": "2026-11-28T18:00:00Z",
            },
        ],
        "away": [],
    }
    status, _, checks = check(case)

    assert status == "ok"
    assert checks["recent"]["unknown"] == []
    assert [row["match_id"] for row in checks["recent"]["matched"]] == ["fdorg:4700", "fd:4600"]


def test_nested_annotation_style_provider_citations_support_claims(case):
    case["provider_citations"][0] = {
        "type": "url_citation",
        "url_citation": {"url": "https://club.example/preview", "title": "离线示例"},
    }
    status, row, _ = check(case)

    assert status == "ok"
    assert row["basis"][0]["urls"] == ["https://club.example/preview"]


@pytest.mark.parametrize(
    "raw",
    [
        "比赛身份核对：不一致，赛事不是同一场。",
        "比赛身份对不上。\n客队换了。",
        "## **比赛身份对不上**",
    ],
)
def test_raw_identity_conflict_cannot_be_overwritten_by_structure_conversion(case, raw):
    case["report"] = raw
    status, row, checks = check(case)

    assert status == "identity_mismatch" and row is None
    assert "identity" in checks


@pytest.mark.parametrize(
    "field, value",
    [("away", "西城"), ("date_beijing", "2026-11-26"), ("league", "杯赛"), ("jc_num", "周六001")],
)
def test_extracted_identity_must_match_canonical_material(case, field, value):
    case["structure"]["identity"][field] = value
    status, row, checks = check(case)

    assert status == "identity_mismatch" and row is None
    assert checks["identity"] == [field]


@pytest.mark.parametrize(
    "invalid", ["```json\n{}\n```", "[]", '{"bad":NaN}', '{"judgment":"h","judgment":"a"}', "{}"]
)
def test_only_complete_unique_key_finite_json_is_accepted(case, invalid):
    status, row, _ = parse(invalid, case["material"], [])

    assert status == "unparsed" and row is None


@pytest.mark.parametrize(
    "cutoff", ["2026-11-27T12:00:01Z", "2026-11-27T14:05:00Z", "2026-11-27T11:50:00"]
)
def test_research_cutoff_must_be_timestamped_and_available_at_request(case, cutoff):
    case["structure"]["cutoff_utc"] = cutoff
    status, row, _ = check(case)

    assert status == "unparsed" and row is None


def test_jc_snapshot_observed_after_request_is_not_used_even_if_prematch(case):
    case["material"]["odds_snapshot_utc"] = "2026-11-27T12:01:00Z"
    status, _, checks = check(case)

    assert status == "ok"
    assert checks["odds"] == []
    assert checks["odds_status"] == "no_prematch_snapshot"


def test_post_cutoff_fact_is_rejected(case):
    case["structure"]["basis"][0]["published_utc"] = "2026-11-27T11:50:01Z"
    status, row, _ = check(case)

    assert status == "unparsed" and row is None


def test_fabricated_urls_mark_facts_unsupported_and_never_look_up_links(case):
    case["provider_citations"] = []
    case["report"] = "比赛身份核对：一致"
    case["structure"]["basis"][0]["urls"] = [
        "https://club.example/preview",
        "https://username:password@host.example/prematch",
        "javascript:alert(1)",
    ]
    status, row, checks = check(case)

    assert status == "ok"
    assert row["basis"][0]["urls"] == [] and row["citations"] == []
    assert "https://club.example/preview" in checks["unverified_citations"]
    assert all("password" not in value for value in checks["unverified_citations"])
    assert {"field": "basis[0]", "reason": "no_source"} in checks["unsupported"]


def test_no_reliable_tendency_and_optional_probabilities(case):
    case["structure"].update(judgment=None, probabilities=None, basis_type=None)
    status, row, _ = check(case)

    assert status == "ok" and row["judgment"] is None and row["probabilities"] is None


@pytest.mark.parametrize(
    "statement",
    [
        "研究判断：无可靠倾向",
        "- **研究判断**：**客胜**（客队占优）",
        "1. 研究判断: 平。双方都可能进球。",
        "研究判断：主胜\n研究判断：客胜",
    ],
)
def test_explicit_original_judgment_cannot_be_changed_by_extraction(case, statement):
    case["report"] += "\n" + statement
    status, row, checks = check(case)

    assert status == "unparsed" and row is None
    assert checks["reason"] == "raw_report_conflict"


def test_explicitly_withheld_probabilities_cannot_be_invented_by_extraction(case):
    case["report"] += "\n- **研究判断的概率：**「不给」。信息不足。"
    status, row, checks = check(case)

    assert status == "unparsed" and row is None
    assert checks["reason"] == "raw_report_conflict"


def test_original_verdict_fields_match_without_confusing_other_sections(case):
    case["report"] += (
        "\n- 主要依据：竞彩研究判断：客胜。"
        "\n- 市场最高概率项：平。"
        "\n- **研究判断：主胜**。"
        "\n- 研究判断的概率：不给。"
    )
    case["structure"]["probabilities"] = None
    status, row, _ = check(case)

    assert status == "ok" and row["judgment"] == "h" and row["probabilities"] is None


@pytest.mark.parametrize(
    "probabilities", [{"h": 0.3, "d": 0.3, "a": 0.3}, {"h": True, "d": 0, "a": 0}]
)
def test_invalid_probability_triplet_is_never_rescaled(case, probabilities):
    case["structure"]["probabilities"] = probabilities
    status, row, _ = check(case)

    assert status == "unparsed" and row is None
