"""Offline checks for the legacy cup routes in the controlled official-clock probe."""

import json
import sys
from datetime import date

import pytest
import responses

from test_cloud_feeds import seed_cloud_archive
from test_t68_jc_kickoffs import FIXTURES, HEADS, catalogue, save_json
from test_t68_jc_kickoffs import module as module
from zucai.cloud_feeds import archive_info, export_feeds, restore_feeds
from zucai.cloud_jc_catalogue import parse_official_leagues
from zucai.sources.sporttery import API

# The exact pre-T95 official request set, including the three generic labels.
T68_OFFICIAL_IDS = {
    1,
    3,
    4,
    7,
    11,
    13,
    14,
    15,
    19,
    21,
    23,
    24,
    26,
    27,
    28,
    29,
    30,
    34,
    36,
    38,
    39,
    41,
    43,
    45,
    46,
    49,
    52,
    54,
    61,
    66,
    67,
    71,
    72,
    73,
    75,
    76,
    78,
    80,
    82,
    83,
    86,
    90,
    93,
    94,
    95,
    100,
    106,
    118,
    120,
    2068445,
    2068446,
    2070739,
}


def test_default_plan_adds_exact_cups_and_preserves_every_old_route_row(
    tmp_path, module, monkeypatch
):
    dictionary = parse_official_leagues((FIXTURES / "t65/jc-one-result-real.json").read_bytes())
    rows = [
        dict(
            league_id=item["league_id"],
            league=item["league_abbr"],
            match_date="2026-10-31",
            match_id=i,
        )
        for i, item in enumerate(dictionary, 1)
    ]
    monkeypatch.setattr(module, "_catalogue", lambda _: (rows, dictionary, {"pages": []}))
    plan = module.plan_kickoffs(tmp_path)
    assert plan["new_route_count"] == 62
    assert set(plan["official_league_ids"]) == T68_OFFICIAL_IDS | {70, 1033103}
    assert [r for r in plan["selected"] if r["league_id"] in T68_OFFICIAL_IDS] == [
        r for r in rows if r["league_id"] in T68_OFFICIAL_IDS
    ]
    assert plan["new_route_match_count"] == len(T68_OFFICIAL_IDS) - 3
    assert plan["legacy_cup_match_count"] == 2

    cups = module.plan_kickoffs(tmp_path, cup_codes=("848", "3"))
    assert cups["new_route_count"] == cups["generic_match_count"] == 0
    assert cups["official_league_ids"] == [70, 1033103]
    assert cups["legacy_cup_match_count"] == 2
    assert {r["league"] for r in cups["selected"]} == {"欧罗巴", "欧协联"}
    assert cups["plan_sha256"] != plan["plan_sha256"]
    assert (
        cups["plan_sha256"] == module.plan_kickoffs(tmp_path, cup_codes=("3", "848"))["plan_sha256"]
    )


@pytest.mark.parametrize(("code", "league_id"), [("3", 70), ("848", 1033103)])
def test_legacy_approval_does_not_borrow_pr135_and_requires_exact_evidence(
    tmp_path, module, monkeypatch, code, league_id
):
    catalogue(tmp_path, (2042404,), [league_id])
    registry = json.loads(module.REGISTRY.read_bytes())
    route = registry["providers"]["apif"][code]
    assert all(item.get("kind") != "pr_identity_review" for item in route["evidence"])
    assert module.plan_kickoffs(tmp_path, cup_codes=(code,))["selected_count"] == 1

    route["evidence"][0]["pointer"] = "/2"
    path = tmp_path / "registry.json"
    save_json(path, registry)
    monkeypatch.setattr(module, "REGISTRY", path)
    with pytest.raises(ValueError, match=rf"{code}.*legacy"):
        module.plan_kickoffs(tmp_path, cup_codes=(code,))


@pytest.mark.parametrize("codes", [(), ("2",), ("3", "79"), ("848", "848")])
def test_targeted_plan_refuses_other_or_duplicate_codes(tmp_path, module, codes):
    catalogue(tmp_path)
    with pytest.raises(ValueError, match="3.*848"):
        module.plan_kickoffs(tmp_path, cup_codes=codes)


@responses.activate
def test_targeted_cups_share_the_existing_probe_archive_and_resume(tmp_path, module, monkeypatch):
    archive = seed_cloud_archive(tmp_path)
    database = tmp_path / "work/data/zucai.db"
    restore_feeds(archive, database)
    catalogue(database.parent, (2042404, 2042405, 2042406), [70, 1033103, 24])
    codes = ("3", "848")
    with pytest.raises(ValueError, match="pilot"):
        module.collect_kickoffs(database, "over-budget", max_requests=6, cup_codes=codes)
    original = json.loads((HEADS / "success.json").read_bytes())
    for sid in (2042404, 2042405):
        original["value"]["sportteryMatchId"] = sid
        responses.add(
            responses.GET,
            API + "getMatchHeadV1.qry",
            match=[
                responses.matchers.query_param_matcher(
                    {"source": "web", "sportteryMatchId": str(sid)}
                )
            ],
            json=original,
        )
    sleeps = []
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    report = module.collect_kickoffs(database, "cup-pilot", cup_codes=codes)
    assert len(responses.calls) == report["requests"] == 2
    assert sleeps == [1, 1]
    assert report["pilot_verified"] and report["success_count"] == 2
    export_feeds(database, archive, date(2026, 11, 1), "export")
    files = archive_info(archive)["files"]
    for sid in (2042404, 2042405):
        assert f"state/catalogue/cup-pilot/jc-heads/{sid}.json" in files
        assert f"state/catalogue/cup-pilot/jc-heads/{sid}.json.meta.json" in files

    restored = tmp_path / "restored/data/zucai.db"
    restore_feeds(archive, restored)
    resume = module.collect_kickoffs(restored, "cup-resume", max_requests=6, cup_codes=codes)
    assert resume["requests"] == 0 and resume["success_count"] == 2
    assert module.plan_kickoffs(archive)["pending"][0]["league_id"] == 24

    output = tmp_path / "plan.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["kickoffs", "plan", "--archive", str(archive), "--out", str(output), "--leagues", "3"],
    )
    module.main()
    single = json.loads(output.read_bytes())
    assert single["official_league_ids"] == [70]
    assert single["verified"][0]["match_id"] == 2042404
