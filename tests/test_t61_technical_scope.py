"""Approving a fixture route must not authorize technical-statistics collection."""

import subprocess
import sys
from pathlib import Path


def test_new_fixture_route_does_not_expand_technical_scopes():
    code = """
from datetime import UTC, datetime
from zucai.sources import api_football
api_football.APIF_LEAGUE_IDS = api_football.APIF_LEAGUE_IDS | {1001}
from zucai.stats_client import ALLOWED_LEAGUES
from zucai.cloud_stats_probe import ALLOWED
from zucai.stats_schedule import plan, injury_units
assert 1001 not in ALLOWED_LEAGUES
assert 1001 not in ALLOWED
selection = plan(None, '2025', 'injuries', datetime(2026, 11, 14, tzinfo=UTC))
list(injury_units(selection))
assert len(ALLOWED_LEAGUES) == 19
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
