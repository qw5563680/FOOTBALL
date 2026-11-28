import pytest

from zucai.web_research_identity_evidence import extract_identity_evidence


@pytest.mark.parametrize(
    "pair, when",
    [
        ("布莱克本主场对查尔顿", "2026-11-28 12:30 UTC"),
        ("查尔顿客场对阵布莱克本", "2026-11-28T12:30Z"),
        ("布莱克本 vs 查尔顿", "2026-11-28 20:30 北京时间"),
    ],
)
def test_equivalent_fixture_wording_means_the_same_home_away_and_instant(pair, when):
    (claim,) = extract_identity_evidence(
        f"- **比赛身份核对**：一致。英冠，{pair}，开球时间 {when}。"
    )
    assert (claim["home"], claim["away"]) == ("布莱克本", "查尔顿")
    assert claim["kickoff_utc"] == "2026-11-28T12:30:00+00:00"


def test_missing_or_conflicting_zone_does_not_prove_kickoff():
    for clock in ("20:30", "12:30 UTC（北京时间）"):
        (claim,) = extract_identity_evidence(
            f"比赛身份核对：一致。英冠，北城主场对南城，开球时间 2026-11-28 {clock}。"
        )
        assert "kickoff_utc" not in claim


def test_cites_matchup_and_kickoff_without_borrowing_other_sections():
    report = (
        "- **数据截止时间**：2026-11-28 15:44（北京时间）\n\n"
        "- **比赛身份核对**：一致。海湾水手 vs 岛城FC，测试超第6轮，"
        "2026-11-28 16:40（北京时间），足球场。\n\n"
        "- **关键伤停与首发**\n\n"
        "  主队：他队 https://example.test/other-team\n"
        "- **反证**：最近一次交锋（2026-04-19）。\n"
        "- **引用列表**\n  - https://example.test/unpositioned"
    )
    start = report.index("- **比赛身份核对**")
    other = report.index("- **关键伤停与首发**")
    cited = "https://example.test/fixture/current"
    annotations = [
        {"url_citation": {"url": cited, "start_index": start, "end_index": other + 4}},
        {
            "url_citation": {
                "url": "https://example.test/history",
                "start_index": other,
                "end_index": other + 4,
            }
        },
        {
            "url_citation": {
                "url": "https://example.test/cutoff",
                "start_index": 0,
                "end_index": other,
            }
        },
    ]

    (claim,) = extract_identity_evidence(report, annotations)

    assert (claim["home"], claim["away"], claim["league"]) == ("海湾水手", "岛城FC", "测试超")
    assert claim["date_beijing"] == "2026-11-28"
    assert claim["kickoff_utc"] == "2026-11-28T08:40:00+00:00"
    assert claim["urls"] == [cited]
    assert "足球场" in claim["text"] and "他队" not in claim["text"]


def test_ordered_kickoff_sentence_and_matchup_date_are_accepted():
    (sentence,) = extract_identity_evidence(
        "比赛身份核对：一致。北城与南城于北京时间 2026-11-27 22:05 开球。\n"
        "北城主页公布了近况（https://example.test/latest）。"
    )
    assert (sentence["home"], sentence["away"]) == ("北城", "南城")
    assert sentence["date_beijing"] == "2026-11-27"
    assert sentence["kickoff_utc"] == "2026-11-27T14:05:00+00:00"
    assert sentence["urls"] == []

    (dated,) = extract_identity_evidence(
        "比赛身份核对：一致。测试联赛，2026-11-03，合成主队 VS 合成客队。"
    )
    assert (dated["home"], dated["away"], dated["league"]) == ("合成主队", "合成客队", "测试联赛")
    assert dated["date_beijing"] == "2026-11-03" and "kickoff_utc" not in dated


def test_labeled_sides_bind_only_their_own_source_links():
    report = (
        "### 本场比赛\n"
        "赛事：测试联赛\n"
        "主队：North City（https://example.test/teams/18）\n"
        "客队：South City（https://example.test/teams/19）\n"
        "开球时间：2026-11-27T14:05:00Z\n"
        "出处：https://example.test/fixtures/99\n"
        "近期战绩：East City vs West City，2026-11-20。\n"
        "来源：https://example.test/fixtures/past"
    )

    (claim,) = extract_identity_evidence(report)

    assert (claim["home"], claim["away"]) == ("North City", "South City")
    assert claim["team_urls"] == {
        "home": ["https://example.test/teams/18"],
        "away": ["https://example.test/teams/19"],
    }
    assert claim["urls"][-1] == "https://example.test/fixtures/99"
    assert claim["kickoff_utc"] == "2026-11-27T14:05:00+00:00"
    assert "East City" not in claim["text"]


def test_same_paragraph_previous_leg_cannot_supply_the_current_fixture():
    (claim,) = extract_identity_evidence(
        "比赛身份核对：一致。北城 VS 南城。"
        "上一回合北城 VS 南城在 2026-11-20 21:30（北京时间）开球。"
    )
    assert (claim["home"], claim["away"]) == ("北城", "南城")
    assert "date_beijing" not in claim

    (previous_as_current,) = extract_identity_evidence(
        "比赛身份核对：一致。本场对阵北城 VS 南城，2026-11-20 21:30（北京时间）。"
    )
    assert (previous_as_current["home"], previous_as_current["away"]) == ("北城", "南城")
    assert previous_as_current["date_beijing"] == "2026-11-20"


def test_identifier_and_generic_agreement_do_not_create_identity_fields():
    claims = extract_identity_evidence(
        "比赛身份核对：一致。比赛编号：jc:5001。\n"
        "近期对战：北城 vs 南城，2026-11-27 22:05（北京时间）。"
    )

    (claim,) = claims
    assert claim["claimed_match_id"] == "jc:5001"
    assert not {"home", "away", "date_beijing"} & claim.keys()
    assert not claim["urls"]


def test_explicit_identity_conflict_remains_visible():
    for report in ("## **比赛身份对不上**", "- **比赛身份核对**：不一致，赛事不是同一场。"):
        (claim,) = extract_identity_evidence(report)
        assert claim["explicit_mismatch"]


def test_competing_pair_lines_do_not_share_kickoffs_or_links():
    correct, wrong = extract_identity_evidence(
        "比赛身份核对：一致。"
        "北城 VS 南城于北京时间2026-11-27 22:05 开球（https://example.test/current）。"
        "北城 VS 西城（https://example.test/other）。"
    )

    assert correct["away"] == "南城"
    assert correct["kickoff_utc"] == "2026-11-27T14:05:00+00:00"
    assert correct["urls"] == ["https://example.test/current"]
    assert wrong["away"] == "西城"
    assert "kickoff_utc" not in wrong
    assert wrong["urls"] == ["https://example.test/other"]


def test_generic_agreement_cannot_consume_another_paragraph_or_section():
    for following in ("\n\n", "反证："):
        (claim,) = extract_identity_evidence(
            "比赛身份核对：一致。" + following + "北城 VS 南城，2026-11-27 22:05（北京时间）。"
        )
        assert not {"home", "away", "date_beijing"} & claim.keys()
