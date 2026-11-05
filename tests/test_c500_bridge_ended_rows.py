"""Rows whose sale has closed stay on the 500 JC page with blank provider IDs (seen 2026-11-05)."""

import logging
from pathlib import Path

import pytest

from zucai.sources.c500 import C500FixtureBridge

RAW = (Path(__file__).parents[1] / "data/fixtures/t26/full-bridge.html").read_bytes()
ENDED = (
    '<tr class="bet-tb-tr bet-tb-end" data-fixtureid="" data-homesxname="水晶宫" '
    'data-awaysxname="霍芬海姆" data-matchdate="2026-11-06" data-matchtime="01:45" '
    'data-simpleleague="欧罗巴" data-id="2042519" data-homeid="" data-awayid="" '
    'data-matchnum="周四002" data-isend="1"><td></td></tr>'
)


def page(*rows: str) -> bytes:
    text = RAW.decode("gb18030")
    head, tail = text.split("<tr", 1)
    return (head + "".join(rows) + "<tr" + tail).encode("gb18030")


def test_rows_without_any_provider_id_are_skipped_not_fatal(caplog):
    baseline = C500FixtureBridge().parse(RAW)
    with caplog.at_level(logging.WARNING, logger="zucai.sources.c500"):
        bridges = C500FixtureBridge().parse(page(ENDED))
    assert bridges == baseline
    assert all(b["jc_id"] != "jc:2042519" for b in bridges)
    assert "跳过1场" in caplog.text and "周四002" in caplog.text


def test_partial_provider_ids_still_reject_the_page():
    partial = ENDED.replace('data-fixtureid=""', 'data-fixtureid="1420000"')
    with pytest.raises(ValueError, match="缺少球队ID"):
        C500FixtureBridge().parse(page(partial))


def test_page_with_only_ended_rows_is_rejected():
    text = RAW.decode("gb18030")
    head, _ = text.split("<tr", 1)
    body = (head + ENDED + "</tbody></table></body></html>").encode("gb18030")
    with pytest.raises(ValueError, match="没有任何带球队ID"):
        C500FixtureBridge().parse(body)
