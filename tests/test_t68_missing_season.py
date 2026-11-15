"""Missing registry season must be a blocked real-entry result, not an exception."""

import json

import pytest

from test_t68_identity_archive_entry import archive as frozen_archive  # noqa: F401
from zucai import identity_binding_archive as reader
from zucai import identity_daily_gate as gate
from zucai import league_registry


@pytest.mark.parametrize("season", [None, "2026", True, 0])
def test_missing_or_invalid_season_reports_blocked(frozen_archive, monkeypatch, season):  # noqa: F811
    registry = league_registry.load_registry()
    config = registry["providers"]["apif"]["48"]["config"]
    config.pop("current_season", None)
    if season is not None:
        config["current_season"] = season
    path = frozen_archive.parent / "registry.json"
    path.write_text(json.dumps(registry))
    monkeypatch.setattr(reader, "REGISTRY", path)
    monkeypatch.setattr(league_registry, "load_registry", lambda: registry)
    result = gate.build_register(frozen_archive, "2026-11-14T16:30:00Z", codes=["48"])
    row = result["leagues"][0]
    assert row["status"] == "blocked"
    assert "current_season_not_configured" in row["blockers"]
    assert row["matches"]["total"] is None
    assert row["daily_changed"] is False


from test_t68_daily_gate_entry import bundle  # noqa: E402,F401


def test_mixed_register_preserves_valid_route_while_missing_route_blocks(bundle):  # noqa: F811
    archive, root, evidence = bundle
    result = gate.build_register(
        archive,
        "2026-11-14T16:30:00Z",
        codes=["48", "98"],
        evidence={"98": evidence},
        evidence_root=root,
    )
    rows = {r["provider_code"]: r for r in result["leagues"]}
    assert rows["48"]["status"] == "blocked"
    assert rows["48"]["matches"]["total"] is None
    assert rows["98"]["status"] == "ready_for_review"
    assert rows["98"]["matches"]["linked"] == 1
