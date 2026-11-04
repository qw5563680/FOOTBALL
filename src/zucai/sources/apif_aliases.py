"""Validate the approval provenance added by T23 without rewriting legacy aliases."""

import re
from datetime import datetime, timedelta


def validate_approvals(aliases, *, require_metadata=False):
    fields = {"approved_by", "approved_utc", "approval_pr"}
    if not isinstance(aliases, dict):
        raise ValueError("APIF 别名配置须为按联赛组织的对象")
    for records in aliases.values():
        if not isinstance(records, dict):
            continue  # Existing importer reports malformed league entries.
        for entry in records.values():
            if not isinstance(entry, dict) or entry.get("status") != "approved":
                continue
            if not require_metadata and not fields & entry.keys():
                continue  # Pre-T23 approvals remain readable, with no invented provenance.
            if not fields <= entry.keys() or any(
                not isinstance(entry[k], str) or not entry[k].strip()
                for k in ("approved_by", "approved_utc")
            ):
                raise ValueError("审批记录须完整提供 approved_by、approved_utc、approval_pr")
            try:
                instant = datetime.fromisoformat(entry["approved_utc"].replace("Z", "+00:00"))
            except ValueError:
                raise ValueError("审批 approved_utc 无效") from None
            if instant.utcoffset() != timedelta(0):
                raise ValueError("审批 approved_utc 须为带偏移的 UTC 时间")
            links = entry["approval_pr"]
            links = [links] if isinstance(links, str) else links
            if (
                not isinstance(links, list)
                or not links
                or any(
                    not isinstance(link, str)
                    or not re.fullmatch(
                        r"https://github\.com/qw5563680/FOOTBALL/pull/[1-9][0-9]*"
                        r"(?:#issuecomment-[1-9][0-9]*)?",
                        link,
                    )
                    for link in links
                )
            ):
                raise ValueError("审批 approval_pr 须为本仓库 PR 完整链接")
