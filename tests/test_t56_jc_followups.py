"""T56 keeps reviewed national identities and exposes the archived review queue."""

import hashlib
import json
from copy import deepcopy
from dataclasses import replace

import pytest

from test_daily_digest import NOW, snapshot
from test_daily_digest import digest_archive as digest_archive
from test_t54_jc_aliases import CAPTURED, schedule
from test_t54_jc_aliases import database as database
from zucai.daily_digest import generate
from zucai.db import session_scope
from zucai.models import Match, Team
from zucai.sources.results import apply_result
from zucai.sources.sporttery import save_schedule
from zucai.sources.sporttery_aliases import load_aliases


@pytest.mark.parametrize("league", ["友谊赛", "世预赛", "欧洲杯", "世界杯", "欧国联"])
def test_approved_national_short_name_does_not_reuse_old_duplicate(database, league):
    with session_scope(database) as session:
        session.add(Team(id=1156, name="阿尔巴尼", aliases=[]))
    with session_scope(database) as session:
        save_schedule(session, [schedule(league=league)], CAPTURED)
        assert session.get(Match, "jc:2042694").home_team_id == 1149
        assert session.get(Team, 1156).name == "阿尔巴尼"
        assert session.get(Team, 1149).aliases == ["apif:欧国联:team:778"]


def test_national_short_name_also_resolves_for_official_results(database):
    from test_t54_jc_aliases import SEEN, result

    with session_scope(database) as session:
        session.add(Team(id=1156, name="阿尔巴尼", aliases=[]))
    with session_scope(database) as session:
        apply_result(session, replace(result(), league="世预赛"), SEEN)
        assert session.get(Match, "jc:2042694").home_team_id == 1149


def test_conflicting_approved_national_aliases_fail_closed(database):
    aliases = deepcopy(load_aliases())
    aliases["友谊赛"] = {"阿尔巴尼": {"canonical": "另一国家", "status": "approved"}}
    with pytest.raises(ValueError, match="冲突"):
        with session_scope(database) as session:
            save_schedule(session, [schedule(league="友谊赛")], CAPTURED, aliases=aliases)


def test_unapproved_national_scope_cannot_authorize_a_name(database):
    aliases = {
        "欧国联": {
            "阿尔巴尼": {
                "canonical": "阿尔巴尼亚",
                "status": "pending",
                "scope": "national_team",
            }
        }
    }
    with session_scope(database) as session:
        save_schedule(session, [schedule(league="友谊赛")], CAPTURED, aliases=aliases)
        assert session.get(Match, "jc:2042694").home_team_id != 1149


def install_candidates(archive, candidates):
    name = "state/alias_candidates/jc.json"
    path = archive / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(candidates, ensure_ascii=False))
    manifest_path = archive / "feed_manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["files"][name] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False))


def candidate(match_id, captured="2026-11-03T09:30:00Z", status="pending"):
    return {
        "source": "jc",
        "canonical": None,
        "status": status,
        "team_id": 1234,
        "evidence": [{"match_id": match_id, "captured_utc": captured}],
    }


def test_daily_shows_pending_name_league_and_first_observed_match(digest_archive, tmp_path):
    entry = candidate("jc:later")
    entry["evidence"].append({"match_id": "jc:first", "captured_utc": "2026-11-02T10:00:00Z"})
    install_candidates(digest_archive, {"友谊赛": {"待核甲队": entry}})
    before = snapshot(digest_archive)
    text = generate(digest_archive, tmp_path / "daily", now=NOW).read_text()
    line = next(line for line in text.splitlines() if "竞彩待核新队名" in line)
    assert "竞彩待核新队名 1 个" in line
    assert "待核甲队" in line and "友谊赛" in line and "jc:first" in line
    assert "jc:later" not in line
    assert text.index("采集状态：") < text.index(line) < text.index("采集归档 UTC：")
    assert snapshot(digest_archive) == before


@pytest.mark.parametrize("entries", [{}, {"甲队": candidate("jc:approved", status="approved")}])
def test_daily_omits_empty_pending_queue(digest_archive, tmp_path, entries):
    install_candidates(digest_archive, {"友谊赛": entries})
    text = generate(digest_archive, tmp_path / "daily", now=NOW).read_text()
    assert "竞彩待核新队名" not in text


def test_daily_bounds_pending_names_and_excludes_future_observations(digest_archive, tmp_path):
    entries = {f"待核{i}": candidate(f"jc:pending-{i}") for i in range(7)}
    entries["未来"] = candidate("jc:future", captured="2026-11-04T10:00:00Z")
    install_candidates(digest_archive, {"世预赛": entries})
    text = generate(digest_archive, tmp_path / "daily", now=NOW).read_text()
    line = next(line for line in text.splitlines() if "竞彩待核新队名" in line)
    assert "竞彩待核新队名 7 个" in line
    assert line.count("jc:pending-") == 5
    assert "其余 2 个" in line
    assert "未来" not in line and "jc:future" not in line
