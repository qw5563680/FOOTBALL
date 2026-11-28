"""Independent T70 consistency checks against synthetic offline research records."""

import json
from pathlib import Path

import pytest

from zucai.web_research_validation import parse

FIXTURE = Path(__file__).resolve().parents[1] / "data/fixtures/t70/validation_synthetic.json"


@pytest.fixture
def case():
    return json.loads(FIXTURE.read_bytes())


@pytest.fixture
def structure_facts(case):
    case["structure"] = json.loads(FIXTURE.with_name("structure-facts.synthetic.json").read_bytes())
    case["report"] += (
        "\n北城公布了训练名单（https://club.example/preview），发布时间未知。"
        "\n南城于北京时间 19:30 公布赛前名单（https://fixtures.example/match）。"
    )
    return case


def check(case, *, odds_threshold=0.05):
    return parse(
        json.dumps(case["structure"], ensure_ascii=False),
        case["material"],
        case["provider_citations"],
        report_text=case["report"],
        odds_threshold=odds_threshold,
    )


@pytest.mark.parametrize(
    "reported, matched",
    [("英冠联赛", True), ("英甲联赛", False), ("英冠杯", False), ("英", False)],
)
def test_league_wording_keeps_tier_and_cup_identity(case, reported, matched):
    case["material"]["identity"]["league"] = case["structure"]["identity"]["league"] = "英冠"
    case["report"] = (
        f"比赛身份核对：一致。{reported}，南城客场对北城，开球时间 2026-11-27 14:05 UTC。"
    )
    context = identity_context(case)
    context["fixtures"].append({**context["fixtures"][0], "id": "jc:7001", "league": "英甲联赛"})
    status, _, checks = check(case)
    assert (status == "ok") is matched
    if matched:
        (obs,) = checks["identity_evidence"]["observations"]
        assert obs["reported_league"] == reported and obs["league"] == "英冠"
    else:
        # An explicit different competition cannot turn into a match by trimming its suffix.
        assert checks["identity_evidence"]["result"] != "matched"


def test_source_report_scores_are_checked_even_if_extractor_sent_empty_placeholders(case):
    # The third trial returned a complete research report and two date-only score rows.
    material = case["material"]
    material["recent"]["home"] = [material["recent"]["home"][1]]
    material["recent"]["away"] = [material["recent"]["away"][0]]
    original = material["recent"]["home"][0]
    assert (original["home"], original["away"]) == ("北城", "东城")
    case["report"] += "\n4. 北城最近一场主场 2-0 胜东城，状态回升；南城最近一场主场 3-0 胜北城。"
    empty = dict(
        date_beijing=None,
        home=None,
        away=None,
        home_goals=None,
        away_goals=None,
        urls=[],
        published_utc=None,
    )
    case["structure"]["recent"] = [dict(empty, date_beijing="2026-11-20"), dict(empty)]
    status, row, checks = check(case)
    assert status == "ok" and row["recent"] == []
    assert [r["field"] for r in checks["unsupported"] if r["reason"] == "empty_score_row"] == [
        "recent[0]",
        "recent[1]",
    ]
    assert checks["warning_count"] >= 2
    assert [r["match_id"] for r in checks["recent"]["matched"]] == ["fdorg:4700"]
    (mismatch,) = checks["recent"]["mismatched"]
    assert mismatch["match_id"] == "jc:4800" and mismatch["reason"] == "score_mismatch"
    assert mismatch["claims"][0]["home_goals"] == 3
    assert mismatch["claims"][0]["date_beijing"] == "2026-11-21"
    assert mismatch["claims"][0]["date_basis"] == "latest_archived_fixture"


def test_partial_score_is_not_silently_dropped_and_nonlatest_claim_is_flagged(case):
    case["structure"]["recent"][0]["home_goals"] = None
    status, _, checks = check(case)
    assert (status, checks["reason"]) == ("unparsed", "invalid_structure")
    case["structure"]["recent"] = []
    case["report"] += "\n北城最近一场主场 3-0 胜西城。"
    status, _, checks = check(case)
    assert status == "ok" and checks["recent"]["matched"] == []
    assert "unverified_latest_fixture" in {r["reason"] for r in checks["unsupported"]}


@pytest.mark.parametrize(
    "sentence, reason",
    [
        ("北城最近一场主场 2-0 负于东城。", "conflicting_report_score"),
        ("北城最近一场主场 2-0 胜东城。", "unverified_latest_fixture"),
    ],
)
def test_inconsistent_wording_and_future_score_availability_do_not_corroborate_a_claim(
    case, sentence, reason
):
    game = case["material"]["recent"]["home"][1]
    game["available_utc"] = "2026-11-27T11:58:00Z"  # after the report's 11:50 cutoff
    case["material"]["recent"]["home"] = [game]
    case["structure"]["recent"] = []
    case["report"] += "\n" + sentence
    status, _, checks = check(case)
    assert status == "ok" and checks["recent"]["matched"] == []
    assert reason in {row["reason"] for row in checks["unsupported"]}


def identity_context(case, aliases=(), *, fixture_ids=()):
    material = case["material"]
    target = material["identity"]
    fixture = {
        "id": target["match_id"],
        "league": target["league"],
        "kickoff_utc": target["kickoff_utc"],
        "home_team_id": 1,
        "away_team_id": 2,
        "fixture_ids": [target["match_id"], *fixture_ids],
    }
    material["identity_context"] = {
        "target": {**fixture, "match_id": target["match_id"]},
        "teams": [
            {"id": 1, "name": target["home"], "aliases": list(aliases[0]) if aliases else []},
            {"id": 2, "name": target["away"], "aliases": list(aliases[1]) if aliases else []},
            {"id": 3, "name": "西城", "aliases": []},
        ],
        "fixtures": [fixture],
    }
    return material["identity_context"]


@pytest.mark.parametrize(
    "pair,kickoff,field",
    [
        ("北城与西城", "2026-11-27 22:05", "away"),
        ("南城与北城", "2026-11-27 22:05", "home"),
        ("北城与南城", "2026-11-26 22:05", "date_beijing"),
        ("北城与南城", "2026-11-21 02:00", "date_beijing"),
    ],
)
def test_raw_current_fixture_conflict_cannot_be_hidden_by_correct_table(case, pair, kickoff, field):
    identity_context(case)
    case["report"] = (
        f"比赛身份核对：一致。{pair}于北京时间 {kickoff} 开球。\n"
        "## 近期战绩\n北城和南城在北京时间 2026-11-27 22:05 有比赛。"
    )

    status, row, checks = check(case)

    assert status == "identity_mismatch" and row is None
    evidence = checks["identity_evidence"]
    assert evidence["result"] == "conflict"
    assert field in {item["field"] for item in evidence["issues"]}


@pytest.mark.parametrize(
    "report,field",
    [
        ("比赛身份核对：一致。", "home"),
        ("比赛身份核对：jc:5001，周五001。", "away"),
        ("比赛身份核对：北城 vs 未批准的陌生译名，2026-11-27 22:05（北京时间）。", "away"),
        ("比赛身份核对：北城 vs 南城FC，2026-11-27 22:05（北京时间）。", "away"),
        ("比赛身份核对：北城 vs 南城U21，2026-11-27 22:05（北京时间）。", "away"),
        ("比赛身份核对：北城 vs 南城女足，2026-11-27 22:05（北京时间）。", "away"),
        ("比赛身份核对：北城 vs 南城B队，2026-11-27 22:05（北京时间）。", "away"),
        ("比赛身份核对：北城 vs 南城，2026-11-27 22:05。", "kickoff_utc"),
    ],
)
def test_unverified_raw_evidence_is_specific_and_never_passes(case, report, field):
    identity_context(case)
    case["report"] = report + "\n## 历史交锋\n北城 vs 南城，2026-11-27 22:05（北京时间）。"

    status, row, checks = check(case)

    assert status == "unparsed" and row is None
    assert checks["reason"] == "identity_unverified"
    evidence = checks["identity_evidence"]
    assert evidence["result"] == "unverified"
    assert field in {item["field"] for item in evidence["issues"]}


@pytest.mark.parametrize(
    "league,home,away,home_alias,away_alias,source,source_url",
    [
        (
            "合成甲",
            "北城",
            "南城",
            "North City FC",
            "南城联队",
            "fdorg",
            "https://www.football-data.org/competition/PL",
        ),
        (
            "合成乙",
            "东湾",
            "西河",
            "东湾竞技足球队",
            "West River United",
            "apif",
            "https://v3.football.api-sports.io/fixtures?league=1",
        ),
    ],
)
def test_scoped_names_and_registered_full_names_work_across_leagues(
    case, league, home, away, home_alias, away_alias, source, source_url
):
    for identity in (case["material"]["identity"], case["structure"]["identity"]):
        identity.update(league=league, home=home, away=away)
    scoped = lambda name: {"name": name, "league": league, "source": source}  # noqa: E731
    identity_context(case, ((scoped(home_alias),), (scoped(away_alias),)))
    case["report"] = (
        f"比赛身份核对：一致。{home_alias} vs {away_alias}，{league}，"
        f"2026-11-27 22:05（北京时间）。{source_url}"
    )

    status, row, checks = check(case)

    assert status == "ok"
    assert (row["identity"]["home"], row["identity"]["away"]) == (home, away)
    assert checks["identity_evidence"]["result"] == "matched"


@pytest.mark.parametrize(
    "url,expected,reason",
    [
        ("https://api.football-data.org/v4/matches/80", "ok", None),
        ("https://v3.football.api-sports.io/fixtures?id=80", "unparsed", "identity_unverified"),
    ],
)
def test_match_number_needs_the_independently_bound_namespace(case, url, expected, reason):
    identity_context(case, fixture_ids=("fdorg:80",))
    case["report"] = f"比赛身份核对：一致。北城 vs 南城，2026-11-27。{url}"

    status, _, checks = check(case)

    assert status == expected
    assert checks.get("reason") == reason


def test_source_of_previous_leg_can_conflict_with_copied_target_date(case):
    context = identity_context(case)
    context["fixtures"].append(
        {
            **context["fixtures"][0],
            "id": "jc:4801",
            "kickoff_utc": "2026-11-21T02:00:00Z",
            "fixture_ids": ["fdorg:81"],
        }
    )
    case["report"] = (
        "比赛身份核对：一致。北城 vs 南城，2026-11-27 22:05（北京时间）。"
        "https://api.football-data.org/v4/matches/81"
    )

    status, row, checks = check(case)

    assert status == "identity_mismatch" and row is None
    assert any(
        issue["reason"] == "source_fixture_conflict" and issue["match_id"] == "jc:4801"
        for issue in checks["identity_evidence"]["issues"]
    )


@pytest.mark.parametrize(
    "away,link,status,reason",
    [
        ("共同译名", "", "unparsed", "ambiguous_team"),
        ("共同译名", "/v4/teams/90", "ok", None),
        ("新媒体译名", "/v4/teams/90", "ok", None),
        ("西城", "/v4/teams/90", "identity_mismatch", "name_and_source_team_conflict"),
        ("南城U21", "/v4/teams/90", "identity_mismatch", "team_squad_conflict"),
    ],
)
def test_side_uses_uniquely_linked_provider_team_without_waiving_conflicts(
    case, away, link, status, reason
):
    context = identity_context(case)
    for team in context["teams"][1:]:
        team["aliases"].append({"name": "共同译名", "league": "离线联赛", "source": None})
    context["teams"][1]["provider_ids"] = [{"id": "90", "league": "离线联赛", "source": "fdorg"}]
    reference = f"（https://api.football-data.org{link}）" if link else ""
    case["report"] = (
        f"比赛身份核对：一致。\n主队：北城\n客队：{away}{reference}\n"
        "开球时间：2026-11-27 22:05（北京时间）"
    )
    if away == "新媒体译名":
        case["structure"]["identity"]["away"] = away

    actual, _, checks = check(case)

    assert actual == status
    findings = checks["identity_evidence"]
    if reason is None:
        assert findings["observations"][0]["resolution"]["away"] == "source_team"
    else:
        assert reason in {item["reason"] for item in findings["issues"]}


def test_recent_scores_resolve_approved_names_without_changing_current_identity(case):
    alias = {"name": "北城联队", "league": "离线联赛", "source": None}
    context = identity_context(case, ((alias,), ()))
    match = case["material"]["recent"]["home"][0]
    context["fixtures"].append(
        {
            "id": match["match_id"],
            "league": "离线联赛",
            "kickoff_utc": match["kickoff_utc"],
            "home_team_id": 2,
            "away_team_id": 1,
        }
    )
    first, wrong, *_ = case["structure"]["recent"]
    case["structure"]["recent"] = [first, {**wrong, "away": "北城联队"}]

    status, _, checks = check(case)

    assert status == "ok" and checks["identity_evidence"]["result"] == "matched"
    assert checks["recent"]["matched"] == []
    (mismatch,) = checks["recent"]["mismatched"]
    assert (mismatch["match_id"], mismatch["reason"], len(mismatch["claims"])) == (
        "jc:4800",
        "score_mismatch",
        2,
    )


def test_complete_structure_accepts_empty_basis_and_counterevidence(structure_facts):
    status, row, _ = check(structure_facts)

    assert status == "ok"
    assert row["basis"] == row["counterevidence"] == []
    facts = structure_facts["structure"]["injuries_lineups"]
    assert row["injuries_lineups"]["home"] == facts["home"]
    assert row["injuries_lineups"]["away"][0]["published_utc"] == "2026-11-27T11:30:00+00:00"


@pytest.mark.parametrize(
    "missing", [("basis",), ("counterevidence",), ("basis", "counterevidence")]
)
def test_omitted_evidence_fields_are_invalid_structure(structure_facts, missing):
    for field in missing:
        del structure_facts["structure"][field]

    status, row, checks = check(structure_facts)

    assert status == "unparsed" and row is None
    assert checks["reason"] == "invalid_structure"


@pytest.mark.parametrize("field", ["home", "away", "basis", "counterevidence"])
def test_string_facts_are_invalid_structure(structure_facts, field):
    structure = structure_facts["structure"]
    arrays = structure["injuries_lineups"] if field in {"home", "away"} else structure
    arrays[field] = ["合成事实只有字符串"]

    status, row, checks = check(structure_facts)

    assert status == "unparsed" and row is None
    assert checks["reason"] == "invalid_structure"


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
    [
        ("away", "西城"),
        ("date_beijing", "2026-11-26"),
        ("league", "杯赛"),
        ("jc_num", "周六001"),
    ],
)
def test_extracted_identity_must_match_canonical_material(case, field, value):
    identity_context(case)
    case["structure"]["identity"][field] = value
    status, row, checks = check(case)

    assert status == "identity_mismatch" and row is None
    assert checks["identity"] == [field]


@pytest.mark.parametrize("field,value", [("home", "北城足球俱乐部"), ("away", "南城FC")])
def test_unapproved_structure_names_remain_unverified(case, field, value):
    case["structure"]["identity"][field] = value
    status, row, checks = check(case)

    assert status == "unparsed" and row is None
    assert checks["reason"] == "identity_unverified"
    assert checks["identity_structure"] == [
        {"field": field, "reason": "unapproved_name", "conflict": False}
    ]


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
    case["report"] = case["report"].splitlines()[0]
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
        "## 研究判断\n\n**客胜**。",
        "### 研究判断：\n\n无可靠研究倾向。",
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
