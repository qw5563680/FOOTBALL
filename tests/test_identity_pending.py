"""Fixtures of a club whose history still sits under another stored name are marked
「身份待核」 and kept out of the pick-nine candidates; the mark clears once merged."""

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from test_sfc_report import NOW
from test_sfc_report import complete_issue as complete_issue
from zucai.db import session_scope
from zucai.identity_pending import PATH, load, pending_label, pending_sides
from zucai.models import Match, Team
from zucai.report.sfc import build_sfc_report, pick_nine_lines
from zucai.sources.teams import normalize_team

ROOT = Path(__file__).parents[1]


def test_repository_list_is_valid_and_points_at_existing_evidence():
    pairs = load()
    assert pairs["克莱蒙"] == "Clermont"
    document = json.loads(PATH.read_bytes())
    for entry in document["teams"]:
        assert normalize_team(entry["canonical"]) == entry["canonical"]
        assert (ROOT / entry["evidence"]).is_file()


@pytest.mark.parametrize(
    "teams",
    [
        [dict(canonical="甲", other="甲")],
        [dict(canonical="甲", other="A"), dict(canonical="甲", other="B")],
        [dict(canonical="甲")],
        [dict(canonical="甲队", other="A")],
        "x",
    ],
)
def test_invalid_list_is_refused(tmp_path, teams):
    path = tmp_path / "pending.json"
    path.write_text(json.dumps(dict(version=1, teams=teams)), encoding="utf-8")
    with pytest.raises(ValueError):
        load(path)


def _split(session, issue, seq, *, league=None):
    """Give the home team of one issue fixture a second stored identity holding one match."""
    match = session.get(Match, next(r["match_id"] for r in issue.snapshot if r["seq"] == seq))
    home = session.get(Team, match.home_team_id).name
    other = Team(name="Other Name FC", aliases=[])
    session.add(other)
    session.flush()
    session.add(
        Match(
            match_id="fd:split-identity",
            source="fd",
            league=league or match.league,
            home_team_id=other.id,
            away_team_id=match.away_team_id,
            kickoff_utc="2025-01-01T12:00:00.000000Z",
            status="finished",
        )
    )
    session.flush()
    return match, home


def test_split_identity_is_marked_and_left_out_of_the_candidates(complete_issue, monkeypatch):
    from zucai.report import sfc

    engine, issue, batch = complete_issue
    with session_scope(engine) as session:
        match, home = _split(session, issue, 3)
        monkeypatch.setattr(sfc, "load_pending_identities", lambda: {home: "Other Name FC"})
        report = build_sfc_report(session, issue, batch, forecast_sha256="a" * 64, generated=NOW)
    rows = {row["seq"]: row for row in report["rows"]}
    assert rows[3]["identity_pending"] == {"home": dict(name=home, other="Other Name FC")}
    assert all("identity_pending" not in row for seq, row in rows.items() if seq != 3)
    # The frozen probabilities stay; only the candidate ranking leaves the fixture out.
    assert rows[3]["probabilities"] is not None
    assert 3 not in report["confidence_order"] and len(report["confidence_order"]) == 13
    assert pending_label(rows[3]) == f"身份待核：主队 {home}（历史仍在「Other Name FC」名下）"
    text = sfc._markdown(report).decode("utf-8")
    assert pending_label(rows[3]) in text.replace("\\", "")
    assert "### 身份待核" in text


def test_history_in_another_league_or_a_merged_identity_does_not_mark(complete_issue, monkeypatch):
    from zucai.report import sfc

    engine, issue, batch = complete_issue
    with session_scope(engine) as session:
        match, home = _split(session, issue, 3, league="别的联赛")
        pairs = {home: "Other Name FC"}
        assert pending_sides(session, pairs, match.league, home, "x") is None
        assert pending_sides(session, pairs, "别的联赛", home, "x") == {
            "home": dict(name=home, other="Other Name FC")
        }
        # Once merged the other row is gone: nothing is marked any more.
        other = session.scalar(select(Team).where(Team.name == "Other Name FC"))
        session.delete(session.get(Match, "fd:split-identity"))
        session.delete(other)
        session.flush()
        assert pending_sides(session, pairs, "别的联赛", home, "x") is None
        monkeypatch.setattr(sfc, "load_pending_identities", lambda: pairs)
        report = build_sfc_report(session, issue, batch, forecast_sha256="a" * 64, generated=NOW)
    assert all("identity_pending" not in row for row in report["rows"])
    assert len(report["confidence_order"]) == 14


def test_pick_nine_skips_a_pending_fixture_and_says_why():
    rows = [
        dict(
            seq=n,
            home="H",
            away="A",
            kickoff_utc="2026-11-08T12:00:00Z",
            probabilities={"h": 0.15, "d": 0.85 - n / 50, "a": n / 50},
        )
        for n in range(1, 15)
    ]
    rows[0]["identity_pending"] = {"away": dict(name="A", other="A English")}
    report = dict(
        rows=rows,
        sale_state="published_before_deadline",
        mode="prematch",
        published_utc="2026-11-08T10:00:00Z",
    )
    lines = pick_nine_lines(report)
    picks = [line for line in lines if "1 平" in line]
    # Row 1 has the highest probability but is pending, so rows 2-10 are chosen.
    assert [int(line.split("|")[1]) for line in picks] == list(range(2, 11))
    text = "\n".join(lines)
    assert "第 1 场：身份待核：客队 A（历史仍在「A English」名下）" in text
    assert "不进入任九模型单选" in text
