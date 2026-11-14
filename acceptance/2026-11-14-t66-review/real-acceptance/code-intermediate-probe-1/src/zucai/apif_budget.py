"""Portable validation for archived API-Football budgets (no Unix-only imports)."""

from datetime import UTC, date, datetime

FALLBACK_LIMIT = 100


def utc_stamp(value):
    if not isinstance(value, str):
        raise ValueError
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError
    return parsed


def normalize_quota(value):
    """Accept legacy files without replenishing any recorded daily consumption."""
    try:
        q = dict(value)
        if date.fromisoformat(q["date_utc"]).isoformat() != q["date_utc"]:
            raise ValueError
        q.setdefault("limit_day", FALLBACK_LIMIT)
        q.setdefault("plan", None)
        q.setdefault("observed_utc", None)
        q.setdefault("limit_source", "stored" if "limit_day" in value else "fallback")
        q.setdefault("current", None)
        q.setdefault("status_free", None)
        q.setdefault("status_checked_utc", None)
        q.setdefault("status_evidence", [])
        limit = q["limit_day"]
        if type(limit) is not int or limit < 1 or type(q["used"]) is not int or q["used"] < 0:
            raise ValueError
        for field in ("remaining", "current"):
            v = q[field]
            if v is not None and (type(v) is not int or not 0 <= v <= limit):
                raise ValueError
        if q["plan"] is not None and (
            not isinstance(q["plan"], str)
            or not q["plan"].strip()
            or len(q["plan"]) > 200
            or any(ord(c) < 32 for c in q["plan"])
        ):
            raise ValueError
        if q["limit_source"] not in ("fallback", "header", "status", "stored"):
            raise ValueError
        if q["limit_source"] == "fallback" and limit != FALLBACK_LIMIT:
            raise ValueError
        for field in ("observed_utc", "status_checked_utc"):
            if q[field] is not None:
                utc_stamp(q[field])
        if q["status_free"] is not None and type(q["status_free"]) is not bool:
            raise ValueError
        evidence = q["status_evidence"]
        if not isinstance(evidence, list) or any(
            not isinstance(sha, str)
            or len(sha) != 64
            or any(c not in "0123456789abcdef" for c in sha)
            for sha in evidence
        ):
            raise ValueError
        if q["status_free"] is not None and (len(evidence) != 2 or q["status_checked_utc"] is None):
            raise ValueError
        return q
    except (ValueError, TypeError, KeyError, OverflowError):
        raise ValueError("配额文件损坏，拒绝重新假定完整预算") from None
