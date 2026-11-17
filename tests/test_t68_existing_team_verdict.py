"""T68 第八节第 7 项（2026-11-16 更正版）：创建提案与 apply 的三结论规则。"""

import pytest

from test_t68_canonical_approval import approved, creation  # noqa: F401
from test_t68_identity_archive_entry import archive as frozen_archive  # noqa: F401
from test_t68_identity_archive_entry import seal, table
from zucai.identity_canonical import _existing_team_verdict, propose

KICKOFF = "2026-10-31T12:15:00+00:00"
AVAILABLE = "2026-11-14T01:00:00Z"


def _verdicts(archive):
    proposal = propose(archive, codes=["48"])
    return proposal["existing_team_verdicts"]


def _unit(team=None, facts=None, tables=None, configs=None, countries=None, index=None):
    team = team or {
        "official_team_id": "508",
        "name": "切尔西",
        "official_name_evidence": [],
    }
    facts = facts or {
        "teams": {},
        "official": [],
        "formal": {},
        "apif": [{"competition_id": "48"}],
    }
    tables = tables or {"matches": [], "results": []}
    return _existing_team_verdict(
        facts,
        tables,
        team,
        "jc-team:508",
        configs=configs or {},
        countries=countries or {},
        index=index or {},
        code="48",
    )


def test_new_requires_positive_basis(creation):  # noqa: F811
    """Unique witnessed provider id and no existing indication is the only path to 新建."""
    archive, _db = creation
    verdict = _verdicts(archive)["508"]
    assert verdict["status"] == "new"
    assert verdict["basis"] == "unique_witnessed_provider_team_without_existing_indication"


def test_pending_review_without_unique_provider_id():
    verdict = _unit(index={})
    assert verdict["status"] == "pending_review"
    assert "未给出唯一且见证齐全的接口球队编号" in verdict["missing"][0]


def test_pending_review_when_archive_lacks_provider_originals():
    facts = {"teams": {}, "official": [], "formal": {}, "apif": []}
    verdict = _unit(facts=facts)
    assert verdict["status"] == "pending_review"
    assert any("归档里没有该赛事的接口原件" in item for item in verdict["missing"])


def test_pending_review_when_same_country_fd_teams_live_in_candidate_league():
    """A candidate-status route must be covered by the country mapping too."""
    facts = {
        "teams": {
            "project-team:9": {
                "name": "Old home",
                "project_team_id": "9",
                "identity_key": "project-team:9",
                "provenance": [],
            }
        },
        "official": [],
        "formal": {},
        "apif": [{"competition_id": "48"}],
    }
    tables = {
        "matches": [{"source": "fd", "league": "候选联赛", "home_team_id": 9, "away_team_id": 8}],
        "results": [],
    }
    configs = {"48": {"league": "英联赛杯"}, "999": {"league": "候选联赛"}}
    countries = {"48": "England", "999": "England"}
    verdict = _unit(
        facts=facts,
        tables=tables,
        configs=configs,
        countries=countries,
        index={"508": [{"provider_team_id": "102", "evidence_status": "strict_verified"}]},
    )
    assert verdict["status"] == "pending_review"
    assert any("候选联赛" in item for item in verdict["missing"])


def test_undetermined_league_country_counts_as_same_country():
    facts = {
        "teams": {
            "project-team:9": {
                "name": "Old home",
                "project_team_id": "9",
                "identity_key": "project-team:9",
                "provenance": [],
            }
        },
        "official": [],
        "formal": {},
        "apif": [{"competition_id": "48"}],
    }
    tables = {
        "matches": [
            {"source": "fd", "league": "无国家登记联赛", "home_team_id": 9, "away_team_id": 8}
        ],
        "results": [],
    }
    configs = {"48": {"league": "英联赛杯"}, "998": {"league": "无国家登记联赛"}}
    countries = {"48": "England"}
    verdict = _unit(
        facts=facts,
        tables=tables,
        configs=configs,
        countries=countries,
        index={"508": [{"provider_team_id": "102", "evidence_status": "strict_verified"}]},
    )
    assert verdict["status"] == "pending_review"
    assert any("无国家登记联赛" in item for item in verdict["missing"])


def test_pending_review_with_multiple_provider_ids():
    def entry(provider):
        return {"provider_team_id": provider, "evidence_status": "strict_verified"}

    verdict = _unit(index={"508": [entry(101), entry(102)]})
    assert verdict["status"] == "pending_review"


def test_existing_via_approved_alias_entry():
    facts = {
        "teams": {
            "project-team:1": {
                "name": "Project home",
                "project_team_id": "1",
                "identity_key": "project-team:1",
                "provenance": [],
            }
        },
        "official": [],
        "formal": {
            "英联赛杯": {
                "102": {"status": "approved", "canonical": "Project home"},
            }
        },
    }
    verdict = _unit(
        facts=facts,
        index={"508": [{"provider_team_id": "102", "evidence_status": "strict_verified"}]},
    )
    assert verdict["status"] == "existing"
    assert verdict["basis"] == "approved_alias_entry"
    assert verdict["project_team_id"] == "1"
    assert verdict["provider_team_id"] == "102"


def test_existing_via_same_chinese_name_without_official_evidence(creation):  # noqa: F811
    archive, _db = creation
    table(archive, "teams", [dict(id=3, name="切尔西", aliases=[])])
    seal(archive)
    verdict = _verdicts(archive)["508"]
    assert verdict["status"] == "existing"
    assert verdict["basis"] == "same_chinese_name_without_official_evidence"
    assert verdict["project_team_id"] == "3"


def test_pending_review_when_same_country_fd_only_teams_exist(creation):  # noqa: F811
    """Real shape: stored jc fixtures have no counterpart, and the library holds
    fd/fdorg-only teams from the same country."""
    archive, _db = creation
    table(
        archive,
        "teams",
        [
            dict(id=8, name="Away old", aliases=[]),
            dict(id=9, name="Home old", aliases=[]),
        ],
    )
    table(
        archive,
        "matches",
        [
            dict(
                match_id="fd:1",
                source="fd",
                league="英超",
                home_team_id=9,
                away_team_id=8,
                kickoff_utc=KICKOFF,
                jc_num=None,
                status="finished",
                result_checked_utc=None,
                result_provider="fd",
                counterpart_id=None,
            )
        ],
    )
    seal(archive)
    verdict = _verdicts(archive)["508"]
    assert verdict["status"] == "pending_review"
    assert any("英超" in item for item in verdict["missing"])
    assert any("历史赛季的接口原件" in item for item in verdict["missing"])


def test_pending_review_when_official_only_in_catalogue_and_fd_teams_exist(creation):  # noqa: F811
    """Real shape: the official fixture exists only in the catalogue (not stored),
    and same-country fd/fdorg-only teams exist in the library."""
    archive, _db = creation
    table(
        archive,
        "teams",
        [
            dict(id=8, name="Away old", aliases=[]),
            dict(id=9, name="Home old", aliases=[]),
        ],
    )
    table(
        archive,
        "matches",
        [
            dict(
                match_id="fd:1",
                source="fd",
                league="英超",
                home_team_id=9,
                away_team_id=8,
                kickoff_utc=KICKOFF,
                jc_num=None,
                status="finished",
                result_checked_utc=None,
                result_provider="fd",
                counterpart_id=None,
            )
        ],
    )
    seal(archive)
    verdict = _verdicts(archive)["508"]
    assert verdict["status"] == "pending_review"


def test_apply_rejects_every_verdict_but_new(creation):  # noqa: F811
    from zucai.identity_canonical import apply_approvals

    archive, db = creation
    table(archive, "teams", [dict(id=3, name="切尔西", aliases=[])])
    seal(archive)
    path, _ = approved(archive)
    before = db.read_bytes()
    with pytest.raises(ValueError, match="创建被拒绝"):
        apply_approvals(db, archive, path)
    assert db.read_bytes() == before


def _fd_only_facts(formal=None):
    return {
        "teams": {
            "project-team:9": {
                "name": "Old home",
                "project_team_id": "9",
                "identity_key": "project-team:9",
                "provenance": [],
            }
        },
        "official": [],
        "formal": formal or {},
        "apif": [{"competition_id": "48"}],
    }


def test_fd_team_with_registered_provider_id_no_longer_blocks_creation():
    """T68 八.13: an approved alias entry on the fd team resolves the same-country doubt."""
    tables = {
        "matches": [{"source": "fd", "league": "候选联赛", "home_team_id": 9, "away_team_id": 8}],
        "results": [],
    }
    configs = {"48": {"league": "英联赛杯"}, "999": {"league": "候选联赛"}}
    countries = {"48": "England", "999": "England"}
    index = {"508": [{"provider_team_id": "102", "evidence_status": "strict_verified"}]}
    blocked = _unit(
        facts=_fd_only_facts(), tables=tables, configs=configs, countries=countries, index=index
    )
    assert blocked["status"] == "pending_review"
    for status in ("approved", "auto_verified"):
        formal = {"候选联赛": {"777": {"name": "Old", "canonical": "Old home", "status": status}}}
        verdict = _unit(
            facts=_fd_only_facts(formal),
            tables=tables,
            configs=configs,
            countries=countries,
            index=index,
        )
        assert verdict["status"] == "new"
    proposed = {"候选联赛": {"777": {"name": "Old", "canonical": "Old home", "status": "proposed"}}}
    still = _unit(
        facts=_fd_only_facts(proposed),
        tables=tables,
        configs=configs,
        countries=countries,
        index=index,
    )
    assert still["status"] == "pending_review"


def test_registered_fd_team_with_the_candidates_own_provider_id_is_existing():
    """Same provider id as the registered fd team means the same club, never 新建."""
    tables = {
        "matches": [{"source": "fd", "league": "候选联赛", "home_team_id": 9, "away_team_id": 8}],
        "results": [],
    }
    formal = {"候选联赛": {"102": {"name": "Old", "canonical": "Old home", "status": "approved"}}}
    verdict = _unit(
        facts=_fd_only_facts(formal),
        tables=tables,
        configs={"48": {"league": "英联赛杯"}, "999": {"league": "候选联赛"}},
        countries={"48": "England", "999": "England"},
        index={"508": [{"provider_team_id": "102", "evidence_status": "strict_verified"}]},
    )
    assert verdict["status"] == "existing" and verdict["project_team_id"] == "9"


def test_index_only_route_supplies_its_registered_country():
    """欧冠 is index-only: its stored teams must not count as country-unknown."""
    from zucai.identity_canonical import _country_by_code, _import_configs
    from zucai.league_registry import load_registry

    registry = load_registry()
    configs = _import_configs(registry)
    assert configs["2"]["league"] == "欧冠"
    assert _country_by_code(registry)["2"] == _country_by_code(registry)["3"] == "World"
    tables = {
        "matches": [
            {"source": "fdorg", "league": "欧冠", "home_team_id": 9, "away_team_id": 8},
        ],
        "results": [],
    }
    verdict = _unit(
        facts=_fd_only_facts(),
        tables=tables,
        configs={"48": {"league": "英联赛杯"}, "2": {"league": "欧冠"}},
        countries={"48": "England", "2": "World"},
        index={"508": [{"provider_team_id": "102", "evidence_status": "strict_verified"}]},
    )
    assert verdict["status"] == "new"
