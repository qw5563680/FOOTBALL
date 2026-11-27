"""Exercise country policy with normalized unit inputs, never provider response fixtures."""

import pytest

from zucai.identity_canonical import _existing_team_verdict


def _country(country, *, path="illustrative-evidence-a", sha256="1" * 64):
    return {"country": country, "path": path, "sha256": sha256}


def _verdict(teams, country_proofs, *, competition="3"):
    facts = {
        "teams": {
            f"project-team:{tid}": dict(
                project_team_id=str(tid), name=name, identity_key=f"project-team:{tid}"
            )
            for tid, name, _league in teams
        },
        "formal": {},
        "apif": [{"competition_id": competition}],
    }
    tables = {
        "matches": [
            {"source": "fdorg", "home_team_id": tid, "league": league}
            for tid, _name, league in teams
        ]
    }
    return _existing_team_verdict(
        facts,
        tables,
        {"official_team_id": "508", "name": "候选球队"},
        "jc-team:508",
        configs={
            code: {"league": league}
            for code, league in {
                "3": "欧罗巴",
                "2": "欧冠",
                "39": "英超",
                "140": "西甲",
                "999": "国家未知联赛",
            }.items()
        },
        countries={"3": "World", "2": "Europe", "39": "England", "140": "Spain"},
        index={"508": [{"provider_team_id": "102", "evidence_status": "strict_verified"}]},
        code=competition,
        country_proofs=country_proofs,
    )


def test_known_country_unblocks_candidate_and_reports_exact_country_proof():
    proofs = [_country("England")]
    verdict = _verdict([(8, "异国队", "西甲")], {"102": proofs})
    assert verdict["status"] == "new"
    assert verdict["provider_team_country"] == {"country": "England", "evidence": proofs}
    assert verdict["blocking_teams"] == []


def test_known_country_lists_each_same_country_or_unknown_league_team():
    teams = [(8, "异国队", "西甲"), (9, "同国队", "英超"), (10, "待查国家队", "国家未知联赛")]
    verdict = _verdict(teams, {"102": [_country("England")]})
    assert verdict["status"] == "pending_review"
    assert verdict["basis"] == "unverified_same_country_fd_teams"
    assert {
        (row["project_team_id"], row["name"], row["league"]) for row in verdict["blocking_teams"]
    } == {
        ("9", "同国队", "英超"),
        ("10", "待查国家队", "国家未知联赛"),
    }


@pytest.mark.parametrize(
    "proofs",
    [
        {"508": [_country("England")]},  # The official number cannot select the provider proof.
        {},
        {"102": [_country("")]},
        {"102": [_country("England"), _country("Spain", path="illustrative-evidence-b")]},
    ],
    ids=("wrong-provider-id", "missing", "empty", "conflict"),
)
def test_uncertain_country_stays_pending_even_without_stored_team_blockers(proofs):
    verdict = _verdict([], proofs)
    assert verdict["status"] == "pending_review"
    assert verdict["provider_team_country"]["country"] is None


def test_country_unknown_keeps_the_broad_blocker_inventory():
    verdict = _verdict([(8, "异国队", "西甲"), (9, "同国队", "英超")], {})
    assert verdict["status"] == "pending_review"
    assert {row["name"] for row in verdict["blocking_teams"]} == {"异国队", "同国队"}


@pytest.mark.parametrize("country", ["England", "Spain", "Denmark"])
def test_international_fdorg_placeholder_blocks_each_country_until_registered(
    country, tmp_path, monkeypatch
):
    from zucai.sources import fdorg_verify

    aliases = tmp_path / "fdorg-aliases.json"
    monkeypatch.setattr(fdorg_verify, "DEFAULT_ALIASES", aliases)
    teams = [(8, "fdorg:team:999999", "欧冠"), (9, "具名球队", "欧冠")]
    proofs = {"102": [_country(country)]}
    aliases.write_text("{}")
    verdict = _verdict(teams, proofs)
    assert verdict["status"] == "pending_review"
    assert [row["name"] for row in verdict["blocking_teams"]] == ["fdorg:team:999999"]
    aliases.write_text('{"欧冠":{"999999":{"canonical":"已核实球队","status":"approved"}}}')
    assert _verdict(teams, proofs)["status"] == "new"


@pytest.mark.parametrize(
    ("teams", "status"), [([(8, "同国队", "英超")], "pending_review"), ([], "new")]
)
def test_domestic_verdict_ignores_team_country_and_keeps_existing_output(teams, status):
    existing = _verdict(teams, None, competition="39")
    assert existing["status"] == status
    assert _verdict(teams, {"102": [_country("Spain")]}, competition="39") == existing


def test_production_default_retains_existing_world_scope_before_data_activation():
    verdict = _verdict([(8, "异国队", "西甲")], None)
    assert verdict["status"] == "pending_review"
    assert verdict["basis"] == "unverified_same_country_fd_teams"
    assert "provider_team_country" not in verdict
