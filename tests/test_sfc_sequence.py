"""Lottery periods are followed in order; the scheduled collector asks for the period
that is due by number when the source's root page shows another one."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
import responses
from responses import matchers

from zucai.db import get_engine, init_db, session_scope
from zucai.models import SfcIssue
from zucai.sfc_sequence import sequence
from zucai.sources.c500 import URL, C500Sfc
from zucai.sources.sfc import collect_current_sfc

FIXTURES = Path(__file__).parents[1] / "data/fixtures"
RAW = (FIXTURES / "t07/26156.html").read_bytes()  # period 26156, closes 2026-11-02 22:00 Beijing
BRIDGE_RAW = (FIXTURES / "t26/full-bridge.html").read_bytes()
NOW = datetime(2026, 11, 18, 17, 0, tzinfo=UTC)
ROOT = matchers.query_param_matcher({})


def DUE(number):
    return matchers.query_param_matcher({"expect": number})


def _issue(number, end):
    return dict(issue_no=number, sale_end_utc=end)


def test_period_published_ahead_is_not_in_line():
    state = sequence(
        [
            _issue("26165", "2026-11-15T14:00:00+00:00"),
            _issue("26166", "2026-11-20T14:00:00+00:00"),
            _issue("26171", "2026-11-26T14:00:00+00:00"),
        ],
        NOW,
    )
    assert (state["last_closed"], state["current"]) == ("26165", "26166")
    assert state["ahead"] == ["26171"] and state["expected"] == ()


def test_gap_after_the_last_closed_period_names_the_next_number():
    state = sequence(
        [
            _issue("26166", "2026-11-18T14:00:00+00:00"),
            _issue("26171", "2026-11-26T14:00:00+00:00"),
        ],
        NOW,
    )
    assert state["current"] is None and state["ahead"] == ["26171"]
    assert state["expected"] == ("26167", "27001")


def test_consecutive_numbers_are_all_in_line_and_the_earliest_deadline_is_current():
    state = sequence(
        [
            _issue("26166", "2026-11-18T14:00:00+00:00"),
            _issue("26167", "2026-11-21T14:00:00+00:00"),
            _issue("26168", "2026-11-22T14:00:00+00:00"),
            _issue("26170", "2026-11-25T14:00:00+00:00"),
        ],
        NOW,
    )
    assert state["in_line"] == ["26167", "26168"] and state["current"] == "26167"
    assert state["ahead"] == ["26170"]


def test_lower_numbers_on_sale_stay_in_line_and_earliest_deadline_wins():
    state = sequence(
        [
            _issue("26162", "2026-11-10T14:00:00+00:00"),
            _issue("26163", "2026-11-20T14:00:00+00:00"),
            _issue("26164", "2026-11-21T14:00:00+00:00"),
            _issue("26165", "2026-11-19T14:00:00+00:00"),
        ],
        NOW,
    )
    assert state["in_line"] == ["26163", "26164", "26165"] and state["current"] == "26165"


def test_first_number_of_the_next_year_continues_the_sequence():
    state = sequence(
        [
            _issue("26200", "2026-11-10T14:00:00+00:00"),
            _issue("27001", "2026-11-20T14:00:00+00:00"),
        ],
        NOW,
    )
    assert state["current"] == "27001" and state["ahead"] == []


def test_without_a_closed_period_every_period_on_sale_is_in_line():
    state = sequence([_issue("26171", "2026-11-26T14:00:00+00:00"), _issue("26150", None)], NOW)
    assert (state["last_closed"], state["current"], state["expected"]) == (None, "26171", ())
    with pytest.raises(ValueError):
        sequence([_issue("26171", "2026-11-26T14:00:00")], NOW)


def _database(tmp_path, issues):
    path = tmp_path / "data/feed.db"
    path.parent.mkdir()
    engine = get_engine(path)
    init_db(engine)
    with session_scope(engine) as session:
        for number, end in issues:
            session.add(SfcIssue(issue_no=number, sale_end_utc=end, snapshot=[]))
    engine.dispose()
    return path


def _clock(monkeypatch, moment):
    import zucai.sources.sfc as sfc

    monkeypatch.setattr(sfc, "_sequence_time", lambda meta: moment)


def _page(number):
    return RAW.replace(b'value="26156"', f'value="{number}"'.encode(), 1)


@responses.activate
def test_root_showing_a_later_period_is_left_alone_and_the_due_one_is_fetched_by_number(
    tmp_path, monkeypatch
):
    # 26154 closed, 26155 is due; the root page shows 26156, two numbers ahead.
    path = _database(tmp_path, [("26154", "2026-10-28T14:00:00+00:00")])
    monkeypatch.chdir(tmp_path)
    _clock(monkeypatch, datetime(2026, 11, 1, 12, 0, tzinfo=UTC))
    due_url = C500Sfc.url + "?expect=26155"
    responses.add(responses.GET, C500Sfc.url, body=_page("26155"), match=[DUE("26155")])
    responses.add(responses.GET, C500Sfc.url, body=RAW, match=[ROOT])
    responses.add(responses.GET, URL, body=BRIDGE_RAW)
    result = collect_current_sfc(path)
    assert result["status"] == "complete" and result["requests"] == 3
    assert [c.request.url for c in responses.calls] == [C500Sfc.url, due_url, URL]
    assert (result["root_issue_no"], result["skipped_ahead_issue_no"]) == ("26156", "26156")
    assert (result["due_issue_no"], result["due_status"], result["issue_no"]) == (
        "26155",
        "collected",
        "26155",
    )
    with sqlite3.connect(path) as db:
        stored = {row[0] for row in db.execute("SELECT issue_no FROM sfc_issues")}
    assert stored == {"26154", "26155"}  # 26156 was not stored ahead of its turn


@responses.activate
def test_due_period_not_published_yet_is_reported_and_nothing_stands_in(tmp_path, monkeypatch):
    path = _database(tmp_path, [("26154", "2026-10-28T14:00:00+00:00")])
    monkeypatch.chdir(tmp_path)
    _clock(monkeypatch, datetime(2026, 11, 1, 12, 0, tzinfo=UTC))
    # The source answers the request for 26155 with its default page (26156).
    responses.add(responses.GET, C500Sfc.url, body=RAW, match=[DUE("26155")])
    responses.add(responses.GET, C500Sfc.url, body=RAW, match=[ROOT])
    responses.add(responses.GET, URL, body=BRIDGE_RAW)
    result = collect_current_sfc(path)
    assert result["status"] == "complete" and result["requests"] == 3
    assert result["due_status"] == "unavailable" and "26156" in result["due_detail"]
    assert result["issue_no"] is None and result["mapped"] == 0
    with sqlite3.connect(path) as db:
        assert {row[0] for row in db.execute("SELECT issue_no FROM sfc_issues")} == {"26154"}


@responses.activate
def test_root_showing_a_closed_period_also_asks_for_the_next_number(tmp_path, monkeypatch):
    # Real clock position after the deadline: the root page still shows closed 26156.
    path = _database(tmp_path, [])
    monkeypatch.chdir(tmp_path)
    _clock(monkeypatch, datetime(2026, 11, 3, 12, 0, tzinfo=UTC))
    responses.add(responses.GET, C500Sfc.url, status=503, match=[DUE("26157")])
    responses.add(responses.GET, C500Sfc.url, body=RAW, match=[ROOT])
    responses.add(responses.GET, URL, body=BRIDGE_RAW)
    result = collect_current_sfc(path)
    assert result["status"] == "complete" and result["requests"] == 3
    assert (result["due_issue_no"], result["due_status"]) == ("26157", "unavailable")
    assert result["issue_no"] == "26156"  # the closed period is still refreshed as before
    assert len(responses.calls) == 3  # one attempt only, no retry on the failed request


@responses.activate
def test_root_showing_the_due_period_needs_no_extra_request(tmp_path, monkeypatch):
    path = _database(tmp_path, [("26155", "2026-10-30T14:00:00+00:00")])
    monkeypatch.chdir(tmp_path)
    _clock(monkeypatch, datetime(2026, 11, 1, 12, 0, tzinfo=UTC))
    responses.add(responses.GET, C500Sfc.url, body=RAW)
    responses.add(responses.GET, URL, body=BRIDGE_RAW)
    result = collect_current_sfc(path)
    assert result["status"] == "complete" and result["requests"] == 2
    assert result["issue_no"] == "26156" and "due_issue_no" not in result
    assert [c.request.url for c in responses.calls] == [C500Sfc.url, URL]
