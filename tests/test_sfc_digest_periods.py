"""Strictly one lottery period at a time: of the periods on sale only the one closing
first is shown; the next one takes over after it closes."""

from datetime import UTC, datetime

import zucai.sfc_digest as digest

NOW = datetime(2026, 11, 18, 17, 0, tzinfo=UTC)
PICK_NINE = "### 任九 · 模型单选"


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
            PICK_NINE,
            "",
        ],
    )
    return digest.section(None, {}, None, NOW, lambda path: path)


def test_only_the_period_closing_first_is_shown(monkeypatch):
    lines = _render(
        monkeypatch,
        [
            _issue("26171", "2026-11-26T14:00:00+00:00"),
            _issue("26166", "2026-11-20T14:00:00+00:00"),
        ],
    )
    text = "\n".join(lines)
    assert "本期：26166（2026-11-20 22:00:00 截止）" in text
    assert "后面还有已开售的 26171（2026-11-26 22:00:00 截止），等本期截止后再展示" in text
    # The later period's table and pick-nine block are not rendered at all.
    assert "期号：26166" in lines and "期号：26171" not in lines
    assert lines.count(PICK_NINE) == 1


def test_next_period_takes_over_after_the_first_closes(monkeypatch):
    lines = _render(
        monkeypatch,
        [
            _issue("26171", "2026-11-26T14:00:00+00:00"),
            _issue("26166", "2026-11-18T14:00:00+00:00"),  # closed before NOW
        ],
    )
    assert "期号：26171" in lines and "期号：26166" not in lines
    assert not any("后面还有" in line for line in lines)


def test_single_open_period_keeps_the_plain_layout(monkeypatch):
    lines = _render(
        monkeypatch,
        [
            _issue("26166", "2026-11-20T14:00:00+00:00"),
            _issue("26165", "2026-11-15T14:00:00+00:00"),
        ],
    )
    assert not any("后面还有" in line for line in lines)
    assert "期号：26166" in lines and "期号：26165" not in lines


def test_latest_closed_period_is_shown_when_none_is_on_sale(monkeypatch):
    lines = _render(
        monkeypatch,
        [
            _issue("26165", "2026-11-15T14:00:00+00:00"),
            _issue("26164", "2026-11-12T14:00:00+00:00"),
        ],
    )
    assert "期号：26165" in lines and "期号：26164" not in lines


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
