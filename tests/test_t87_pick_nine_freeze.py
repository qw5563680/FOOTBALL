from datetime import timedelta

import pytest

from test_sfc_daily import complete_issue as complete_issue
from test_sfc_daily import files
from test_sfc_daily import sfc_archive as sfc_archive
from test_sfc_report import NOW
from zucai.sfc_reports import publish_reports, read_reports


def test_pick_nine_cap_is_frozen_with_its_report_and_retries_are_idempotent(sfc_archive):
    archive, _, _, issue = sfc_archive
    assert publish_reports(archive, now=NOW, pick_nine_max_lines=8) == [issue.issue_no]
    ((original_path, first),) = read_reports(archive, include_unconfirmed=True)
    assert first["pick_nine_max_lines"] == 8
    frozen = files(archive / original_path)
    assert publish_reports(archive, now=NOW, pick_nine_max_lines=8) == []

    later = NOW + timedelta(minutes=1)
    assert publish_reports(archive, now=later, pick_nine_max_lines=1) == [issue.issue_no]
    versions = dict(read_reports(archive, include_unconfirmed=True))
    assert len(versions) == 2
    (second,) = (report for path, report in versions.items() if path != original_path)
    assert second["pick_nine_max_lines"] == 1
    assert second["combo"] == first["combo"]
    assert second["rows"] == first["rows"]
    assert files(archive / original_path) == frozen
    assert publish_reports(archive, now=later, pick_nine_max_lines=1) == []


@pytest.mark.parametrize("cap", [0, True, 3**9 + 1])
def test_invalid_pick_nine_budget_fails_before_archiving(sfc_archive, cap):
    archive, _, _, _ = sfc_archive
    old = files(archive)
    with pytest.raises(ValueError, match="注数上限"):
        publish_reports(archive, now=NOW, pick_nine_max_lines=cap)
    assert files(archive) == old
