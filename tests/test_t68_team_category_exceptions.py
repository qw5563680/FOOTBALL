"""Per-team, reviewed category exceptions never grant a numeric binding."""

import json
from hashlib import sha256

import pytest

from test_t68_identity_archive_entry import archive as archive_fixture
from test_t68_identity_archive_entry import raw, seal
from test_t68_jc_kickoffs import save_json
from zucai import identity_categories
from zucai.identity_binding import replay

archive = archive_fixture


@pytest.mark.parametrize("scope", ["exact", "other_league", "other_official", "other_provider"])
@pytest.mark.parametrize("status,expected", [("proposed", False), ("approved", True)])
@pytest.mark.parametrize(
    "label,category",
    [("Willem II", "club/men/senior/first"), ("Jong Ajax", "club/men/senior/reserve")],
)
def test_actual_reader_applies_only_approved_exact_team_exception(
    archive, monkeypatch, status, expected, label, category, scope
):
    path = archive / "state/catalogue/backfill/backfill/api-football/day/fixtures_one.json"
    doc = json.loads(path.read_bytes())
    pid = str(doc["response"][0]["teams"]["home"]["id"])
    doc["response"][0]["teams"]["home"]["name"] = label
    raw(
        archive,
        path.relative_to(archive),
        doc,
        "https://v3.football.api-sports.io/fixtures?league=48&season=2026",
    )
    seal(archive)
    source = archive.parent / "category-proof.txt"
    source.write_text(
        "Synthetic offline reviewer evidence: this exact club identity/category was checked."
    )
    entry = dict(
        competition_id="48",
        provider_team_id=pid,
        official_team_id="849",
        category=category,
        allowed_labels=[label],
        status=status,
        evidence=[
            dict(
                path=source.name,
                sha256=sha256(source.read_bytes()).hexdigest(),
                observed_utc="2026-11-14T01:00:00Z",
            )
        ],
    )
    if expected:
        entry.update(
            approved_by="qw5563680",
            approved_utc="2026-11-14T02:00:00Z",
            approval_pr="https://github.com/qw5563680/FOOTBALL/pull/143",
            review_url="https://github.com/qw5563680/FOOTBALL/pull/143#synthetic-test",
        )
    if scope == "other_league":
        entry["competition_id"] = "39"
    elif scope == "other_official":
        entry["official_team_id"] = "123456"
    elif scope == "other_provider":
        entry["provider_team_id"] = "123456"
    expected = expected and scope == "exact"
    registry = archive.parent / "exceptions.json"
    save_json(registry, dict(version=1, exceptions=[entry]))
    monkeypatch.setattr(identity_categories, "EXCEPTIONS", registry, raising=False)
    result = replay(archive, archive.parent / "out", leagues=["48"])
    row = next(r for r in result["official_register"]["rows"] if r["official_team_id"] == "849")
    assert bool(row["candidate_ids"]) == expected
    if scope == "exact":
        assert row["team_category_exceptions"][0]["status"] == status
    if expected:
        candidate = next(
            e
            for rows in result["apif"].values()
            for e in rows.values()
            if e.get("official_team_id") == "849"
        )
        assert candidate["category"] == category
        assert candidate["status"] == "proposed"
