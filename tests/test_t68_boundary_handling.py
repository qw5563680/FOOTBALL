"""T68 第八节第 10 项修订（方案 A）：六类边界情况由导入程序的离线测试统一保证。

每一类对应开启检查 RISKS 里的一个类别，测的是线上导入用的同一段程序；
`identity_daily_gate.BOUNDARY_TESTS` 按名字引用这里的测试，名字不可随意改。
"""

from datetime import timedelta

import pytest

from test_api_football import ROOT, source, transport
from test_apif_ingest import KEY, WHEN, db, game, ingest, seed  # noqa: F401
from zucai.db import session_scope
from zucai.models import Match, Result

SECOND = "apif:91234"


def _stored(session):
    return {m.match_id for m in session.query(Match).all()}


def test_boundary_unbound(db):  # noqa: F811
    """球队没有生效绑定、又与竞彩比赛时间相近：暂缓，不入库，既有比赛不动。"""
    with session_scope(db) as session:
        seed(session, "jc:1")
        session.get(Result, "jc:1").home_goals = 2
        session.get(Result, "jc:1").away_goals = 1
        result = ingest(session, game(), mapping={})
        assert result["held_unbound"] == 1
        assert result["inserted"] == result["suspected_duplicates"] == result["rejected_rows"] == 0
        assert result["held"][0]["reason_code"] == "held_unbound"
        assert _stored(session) == {"jc:1"}
        assert session.get(Match, "jc:1").counterpart_id is None
        assert session.get(Result, "jc:1").home_goals == 2


def test_boundary_reversed(db):  # noqa: F811
    """同一时刻、主客队与库内竞彩比赛相反：列为疑似重复，不入库，不关联。"""
    with session_scope(db) as session:
        seed(session, "jc:1", home="东岸", away="北港")
        result = ingest(session, game())
        assert result["suspected_duplicates"] == 1
        assert result["inserted"] == result["linked_existing"] == 0
        assert "主客冲突" in result["suspected"][0]["reason"]
        assert _stored(session) == {"jc:1"}
        assert session.get(Match, "jc:1").counterpart_id is None


@pytest.mark.parametrize("same_opponent", [True, False])
def test_boundary_same_team_same_time(db, same_opponent):  # noqa: F811
    """同一批里同一支球队三小时内出现两个比赛编号：两场都隔离，一场也不入库。"""
    other = {} if same_opponent else dict(away_provider_id=777, away_raw="Other FC")
    with session_scope(db) as session:
        result = ingest(
            session,
            game(),
            game(provider_match_id=91234, kickoff_utc=WHEN + timedelta(hours=1), **other),
        )
        assert result["suspected_duplicates"] == 2 and result["inserted"] == 0
        assert all("三小时内同队" in row["reason"] for row in result["suspected"])
        assert _stored(session) == set()


def test_boundary_cross_day(db):  # noqa: F811
    """同一对阵相隔一天的两个比赛编号：是两场不同的比赛，都正常入库，互不关联。"""
    with session_scope(db) as session:
        result = ingest(
            session,
            game(),
            game(provider_match_id=91234, kickoff_utc=WHEN + timedelta(hours=24)),
        )
        assert result["inserted"] == 2
        assert result["suspected_duplicates"] == result["held_unbound"] == 0
        assert _stored(session) == {KEY, SECOND}
        assert session.get(Match, KEY).counterpart_id is None
        assert session.get(Match, SECOND).counterpart_id is None


@pytest.mark.parametrize("status", ["AWD", "WO"])
def test_boundary_rejected(db, status):  # noqa: F811
    """判罚或弃权的比赛没有常规时间比分：拒绝，不入库，不写赛果。"""
    with session_scope(db) as session:
        result = ingest(session, game(status=status))
        assert result["rejected_rows"] == 1 and result["inserted"] == 0
        assert session.get(Match, KEY) is None and session.get(Result, KEY) is None


def test_boundary_partial_pages(tmp_path, monkeypatch):
    """接口说有两页、第二页没拿到：整次抓取报错，不产出可导入的合并原件。"""
    from zucai.sources.api_common import LocalValidationError

    session = transport(
        monkeypatch,
        [
            (ROOT / "synthetic_page1.json").read_bytes(),
            (ROOT / "synthetic_page1.json").read_bytes(),
        ],
    )
    client = source(tmp_path, session=session, min_interval=0)
    with pytest.raises(LocalValidationError, match="分页发生变化或缺页"):
        client.fetch_raw(league_id=98, season=2024)
    assert client.request_count == 2
    # 留下的只有逐页原件；没有任何一份带 archive_pages 的合并文件可供导入。
    import json

    saved = [p for p in (tmp_path / "raw").rglob("*.json") if not p.name.endswith(".meta.json")]
    assert saved and all("archive_pages" not in json.loads(p.read_bytes()) for p in saved)


def test_gate_boundary_registry_points_at_these_tests():
    """开启检查登记的六个测试名必须真实存在，类别与 RISKS 一一对应。"""
    from zucai.identity_daily_gate import BOUNDARY_TESTS, RISKS

    assert set(BOUNDARY_TESTS) == RISKS
    for category, test_id in BOUNDARY_TESTS.items():
        path, name = test_id.split("::")
        assert path == "tests/test_t68_boundary_handling.py"
        assert name == f"test_boundary_{category}" and callable(globals()[name])
