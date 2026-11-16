"""T68 第八节第 7 项：创建提案对每个官方编号给出 新建/关联既有球队/待核 三结论。"""

from copy import deepcopy

from test_t68_canonical_approval import creation  # noqa: F401
from test_t68_identity_archive_entry import archive as frozen_archive  # noqa: F401
from test_t68_identity_archive_entry import seal, table
from zucai.identity_canonical import _existing_team_verdict, propose


def _verdicts(archive):
    proposal = propose(archive, codes=["48"])
    return proposal["existing_team_verdicts"]


def test_new_when_there_is_no_existing_team_evidence(creation):  # noqa: F811
    archive, _db = creation
    assert _verdicts(archive)["508"]["status"] == "new"


def test_existing_team_via_same_chinese_name_without_official_evidence(creation):  # noqa: F811
    archive, _db = creation
    table(archive, "teams", [dict(id=3, name="切尔西", aliases=[])])
    seal(archive)
    verdict = _verdicts(archive)["508"]
    assert verdict["status"] == "existing"
    assert verdict["basis"] == "same_chinese_name_without_official_evidence"
    assert verdict["project_team_id"] == "3"


def _linked_facts(frozen_archive, apif_score=(1, 3), bind_opponent=True):  # noqa: F811
    from zucai.identity_binding_archive import _tables, load_archive

    facts = load_archive(frozen_archive, leagues=["48"])
    team = deepcopy(facts["teams"]["jc-team:508"])
    team["identity_status"] = "creation_proposed"
    team.pop("project_team_id", None)
    facts["teams"]["jc-team:508"] = team
    opponent = deepcopy(facts["teams"]["jc-team:849"])
    if not bind_opponent:
        opponent.pop("project_team_id", None)
    facts["teams"]["jc-team:849"] = opponent
    tables, _ = _tables(frozen_archive)
    tables = {name: [dict(row) for row in rows] for name, rows in tables.items()}
    jc_row = next(row for row in tables["matches"] if row["match_id"] == "jc:2042404")
    jc_row["counterpart_id"] = "apif:10"
    apif_row = dict(jc_row, match_id="apif:10", source="apif", counterpart_id=None)
    tables["matches"].append(apif_row)
    jc_result = next(row for row in tables["results"] if row["match_id"] == "jc:2042404")
    tables["results"].append(
        dict(
            jc_result,
            match_id="apif:10",
            home_goals=apif_score[0],
            away_goals=apif_score[1],
        )
    )
    return facts, tables, team, "jc-team:508"


def test_existing_team_via_match_evidence(frozen_archive):  # noqa: F811
    facts, tables, team, key = _linked_facts(frozen_archive)
    verdict = _existing_team_verdict(facts, tables, team, key)
    assert verdict["status"] == "existing"
    assert verdict["basis"] == "match_evidence"
    assert verdict["project_team_id"] == "2"
    evidence = verdict["evidence"][0]
    assert evidence["jc_match_id"] == "jc:2042404"
    assert evidence["provider_match_id"] == "apif:10"
    assert evidence["side"] == "away"
    assert evidence["score"] == [1, 3]
    assert evidence["opponent_project_team_id"] == "1"


def test_pending_review_when_scores_differ(frozen_archive):  # noqa: F811
    facts, tables, team, key = _linked_facts(frozen_archive, apif_score=(0, 0))
    verdict = _existing_team_verdict(facts, tables, team, key)
    assert verdict["status"] == "pending_review"
    reasons = [item["missing"] for item in verdict["missing"]]
    assert any("常规时间比分不一致或缺失" in missing for missing in reasons)


def test_pending_review_when_opponent_identity_unestablished(frozen_archive):  # noqa: F811
    facts, tables, team, key = _linked_facts(frozen_archive, bind_opponent=False)
    verdict = _existing_team_verdict(facts, tables, team, key)
    assert verdict["status"] == "pending_review"
    reasons = [item["missing"] for item in verdict["missing"]]
    assert any("对手身份未确定" in missing for missing in reasons)


def test_similar_but_not_identical_names_never_link(frozen_archive):  # noqa: F811
    facts, tables, team, key = _linked_facts(frozen_archive)
    # No counterpart: the only route left is the same-name rule, which must not
    # fire for a merely similar name.
    tables["matches"] = [
        row for row in tables["matches"] if row["match_id"] not in {"jc:2042404", "apif:10"}
    ]
    facts["teams"]["project-team:3"] = {
        "name": "切尔西女足",
        "project_team_id": "3",
        "identity_key": "project-team:3",
        "provenance": [],
    }
    verdict = _existing_team_verdict(facts, tables, team, key)
    assert verdict["status"] == "new"
