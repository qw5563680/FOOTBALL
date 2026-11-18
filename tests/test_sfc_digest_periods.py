"""Several periods on sale: each gets its own heading, the one closing first comes first,
and a pick-nine block always names its own period."""

from datetime import UTC, datetime

import zucai.sfc_digest as digest

NOW = datetime(2026, 11, 18, 17, 0, tzinfo=UTC)


def _issue(number, end):
    return (f"sfc/{number}/sfc.json", dict(issue_no=number, sale_end_utc=end, snapshot=[]))


def _render(monkeypatch, states):
    monkeypatch.setattr(digest, "read_reports", lambda archive, include_unconfirmed: [])
    monkeypatch.setattr(digest, "resolve_publications", lambda archive, reports, **kwargs: [])
    monkeypatch.setattr(digest, "read_settled", lambda archive, summary: ([], None))
    monkeypatch.setattr(digest, "archived_sfc_issues", lambda archive, feed, now: states)
    monkeypatch.setattr(
        digest,
        "_issue_lines",
        lambda name, issue, reports, settled, now, link: [
            f"期号：{issue['issue_no']}",
            digest.PICK_NINE_HEADING,
            "",
        ],
    )
    return digest.section(None, {}, None, NOW, lambda path: path)


def test_two_open_periods_are_separated_and_ordered_by_deadline(monkeypatch):
    lines = _render(
        monkeypatch,
        [
            _issue("26171", "2026-11-26T14:00:00+00:00"),
            _issue("26166", "2026-11-20T14:00:00+00:00"),
        ],
    )
    text = "\n".join(lines)
    assert (
        "当前有 2 期在售：26166（2026-11-20 22:00:00 截止）、26171（2026-11-26 22:00:00 截止）"
        in text
    )
    first, second = text.index("### 第 26166 期"), text.index("### 第 26171 期")
    assert first < second
    # Each pick-nine block names its period and sits under that period's own heading.
    assert digest.PICK_NINE_HEADING not in lines
    assert first < text.index("#### 任九 · 模型单选（第 26166 期）") < second
    assert second < text.index("#### 任九 · 模型单选（第 26171 期）")


def test_single_open_period_keeps_the_plain_layout(monkeypatch):
    lines = _render(
        monkeypatch,
        [
            _issue("26166", "2026-11-20T14:00:00+00:00"),
            _issue("26165", "2026-11-15T14:00:00+00:00"),
        ],
    )
    assert digest.PICK_NINE_HEADING in lines
    assert not any(line.startswith("### 第") or "期在售" in line for line in lines)
    assert "期号：26166" in lines and "期号：26165" not in lines


def _row(kickoff, probabilities=None):
    return dict(seq=1, kickoff_utc=kickoff, probabilities=probabilities)


def test_period_before_its_forecast_day_says_when_forecasts_arrive():
    end = "2026-11-20T14:00:00+00:00"
    rows = [_row("2026-11-20T19:30:00+00:00"), _row("2026-11-20T17:30:00+00:00")]
    note = digest.not_yet_note(rows, end, NOW)  # Beijing 11-19 01:00, fixtures on 11-21
    assert "最早一场在北京时间 11-21 01:30 开球" in note and "最早在 11-20 的日报里给出" in note


def test_no_note_once_due_closed_or_forecast_present():
    end = "2026-11-20T14:00:00+00:00"
    rows = [_row("2026-11-20T19:30:00+00:00")]
    due = datetime(2026, 11, 19, 16, 30, tzinfo=UTC)  # Beijing 11-20
    assert digest.not_yet_note(rows, end, due) is None
    assert digest.not_yet_note(rows, "2026-11-18T14:00:00+00:00", NOW) is None
    assert digest.not_yet_note(rows, None, NOW) is None
    assert digest.not_yet_note([], end, NOW) is None
    predicted = [_row("2026-11-20T19:30:00+00:00", dict(h=0.5, d=0.3, a=0.2))]
    assert digest.not_yet_note(predicted, end, NOW) is None
