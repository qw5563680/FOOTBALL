"""Validate the approval provenance added by T23 without rewriting legacy aliases."""

import re
from datetime import datetime, timedelta


def validate_approvals(aliases, *, require_metadata=False):
    fields = {"approved_by", "approved_utc", "approval_pr"}
    if not isinstance(aliases, dict):
        raise ValueError("APIF 别名配置须为按联赛组织的对象")
    for league, records in aliases.items():
        if not isinstance(records, dict):
            continue  # Existing importer reports malformed league entries.
        for entry in records.values():
            if (
                isinstance(entry, dict)
                and entry.get("status") in {"approved", "auto_verified"}
                and "binding_scope" in entry
            ):
                binding_scope_allows(entry, league)
                if league not in entry["binding_scope"]["leagues"]:
                    raise ValueError("binding_scope does not include its own formal league")
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


def valid_identity_category(category):
    """A positive explicit category; absent age/sex/squad never defaults to senior."""
    if not isinstance(category, str):
        return False
    parts = category.split("/")
    if len(parts) != 4:
        return False
    entity, sex, age, squad = parts
    return (
        entity in {"club", "national"}
        and sex in {"men", "women"}
        and (age == "senior" or re.fullmatch(r"U(?:1[5-9]|2[0-3])", age) is not None)
        and squad
        in ({"first", "reserve"} if entity == "club" else {"standard", "olympic", "asian_games"})
    )


def binding_scope_allows(entry, league, *, kickoff_utc=None):
    """Legacy entries retain their semantics; T68 scope fails closed."""
    if "binding_scope" not in entry:
        return True
    scope = entry["binding_scope"]
    if (
        not isinstance(scope, dict)
        or not isinstance(scope.get("leagues"), list)
        or not scope["leagues"]
        or any(not isinstance(value, str) or not value.strip() for value in scope["leagues"])
        or not valid_identity_category(scope.get("category"))
        or set(scope) - {"leagues", "category", "start_utc", "end_utc"}
    ):
        raise ValueError("binding_scope requires explicit leagues and identity category")
    if league not in scope["leagues"]:
        return False
    if "start_utc" in scope or "end_utc" in scope:
        try:
            low = datetime.fromisoformat(scope["start_utc"].replace("Z", "+00:00"))
            high = datetime.fromisoformat(scope["end_utc"].replace("Z", "+00:00"))
            if low.utcoffset() is None or high.utcoffset() is None or low > high:
                raise ValueError
            if kickoff_utc is None:
                return False
            when = datetime.fromisoformat(kickoff_utc.replace("Z", "+00:00"))
            return when.utcoffset() is not None and low <= when <= high
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise ValueError("binding_scope requires valid inclusive UTC time bounds") from exc
    return True
