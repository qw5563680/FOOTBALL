import ast
import sqlite3
from datetime import date
from pathlib import Path

import pytest
import responses

from zucai.cli import main
from zucai.sources.base import RawStore
from zucai.sources.c500 import URL, C500JcHistory

ROOT = Path(__file__).parents[1]
RAW = (ROOT / "data/fixtures/c500_root_20261102.html").read_bytes()


def test_source_url_literals_exclude_robots_forbidden_prefixes():
    forbidden = ("trade.500.com/jczq/?", "trade.500.com/zc6/index.php?expect=")
    violations = []
    for path in (ROOT / "src/zucai/sources").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if any(prefix in node.value for prefix in forbidden):
                    violations.append(f"{path.name}:{node.lineno}: {node.value}")
    assert not violations, "\n".join(violations)


def test_history_fetch_archives_only_queryless_root(tmp_path):
    feed = C500JcHistory(store=RawStore(tmp_path))
    with responses.RequestsMock() as api:
        api.get(URL, body=RAW, match=[responses.matchers.query_string_matcher("")])
        path = feed.fetch_raw()
        assert feed.store.load(path) == RAW
        assert feed.store.load_metadata(path)["url"] == URL
        assert len(api.calls) == 1
    feed.session.close()


def test_root_redirect_is_not_followed_into_a_forbidden_query(tmp_path):
    feed = C500JcHistory(store=RawStore(tmp_path))
    with responses.RequestsMock() as api:
        api.get(URL, status=302, headers={"Location": URL + "?date=2026-11-01"})
        with pytest.raises(ValueError, match="重定向"):
            feed.fetch_raw()
        assert len(api.calls) == 1
    feed.session.close()


@pytest.mark.parametrize(
    ("start", "end", "success"),
    [
        ("2026-11-02", "2026-11-03", True),
        ("2026-11-02", "2026-11-02", True),
        ("2026-11-01", "2026-11-02", False),
        ("2026-11-03", "2026-11-04", False),
        ("2025-01-01", "2025-01-01", False),
    ],
)
def test_cli_validates_whole_kickoff_range_from_one_root_snapshot(
    tmp_path, monkeypatch, caplog, start, end, success
):
    monkeypatch.chdir(tmp_path)
    with responses.RequestsMock() as api:
        api.get(URL, body=RAW, match=[responses.matchers.query_string_matcher("")])
        code = main(["fetch", "results", "--source", "c500", "--from", start, "--to", end])
        assert (code == 0) == success
        assert len(api.calls) == 1
    with sqlite3.connect("data/zucai.db") as db:
        assert db.execute("select count(*) from results").fetchone() == (0,)
    if not success:
        assert "近期备用" in caplog.text
        assert "2026-11-02" in caplog.text and "2026-11-03" in caplog.text


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b'<html><table id="betTbThead"></table></html>',
        RAW.replace(b' data-matchdate="2026-11-02"', b""),
        RAW.replace(b"2026-11-03", b"2026-11-04"),
    ],
)
def test_unknown_or_missing_day_coverage_fails_instead_of_silent_empty(tmp_path, raw):
    feed = C500JcHistory(store=RawStore(tmp_path))
    with responses.RequestsMock() as api:
        api.get(URL, body=raw)
        with pytest.raises(ValueError, match="覆盖|日期"):
            feed.range_results(date(2026, 11, 2), date(2026, 11, 4))
    feed.session.close()


@pytest.mark.parametrize("end, expected", [("2026-10-31", [(0, 4)]), ("2026-11-01", [])])
def test_settled_cross_number_day_replay_and_rejection_before_any_result_write(
    tmp_path, monkeypatch, end, expected
):
    # Regression-only composition of the archived settlement sample: this is
    # neither a new network response nor evidence of scores on today's root.
    raw = (ROOT / "data/fixtures/c500_20261031.html").read_bytes()
    raw = raw.replace(b"<body>", b'<body><table id="betTbThead"></table>')
    raw = raw.replace(b'data-processdate="2026-10-31"', b'data-processdate="2026-10-30"')
    monkeypatch.chdir(tmp_path)
    with responses.RequestsMock() as api:
        api.get(URL, body=raw, match=[responses.matchers.query_string_matcher("")])
        code = main(["fetch", "results", "--source", "c500", "--from", "2026-10-31", "--to", end])
        assert (code == 0) == bool(expected)
        assert len(api.calls) == 1
    with sqlite3.connect("data/zucai.db") as db:
        assert db.execute("select home_goals,away_goals from results").fetchall() == expected
