"""Lottery periods are followed in order, one at a time (owner's rule, 2026-11-19).

The source's own "current" page is not a reliable guide: it has shown a period several
numbers ahead while the next one in line was not published yet. A period on sale is
*in line* when its number does not jump past an unpublished one: it is at most one above
the highest closed number, or reached from there through periods that are all archived.
A period further ahead waits for its turn.

Among the periods in line the one closing first is the current one. Numbers and
deadlines normally agree; when a lower number closes later (seen in an international
week) the earlier deadline wins, otherwise its forecast could not appear before it closes.
"""

from datetime import UTC, datetime


def _instant(value):
    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.utcoffset() is None:
        raise ValueError("期次截止时间必须带时区")
    return moment.astimezone(UTC)


def verification_at(check, now):
    """A historical view can only use an official check completed by its cutoff."""
    if isinstance(check, dict) and check.get("checked_at"):
        try:
            checked = _instant(check["checked_at"])
        except (TypeError, ValueError):
            return {}
        if checked <= _instant(now):
            return check
    return {}


def known_official(check):
    """Read a fresh comparison or the explicitly retained previous notice."""
    return check.get("official") or check.get("last_known_official") or {}


def period_windows(issues, schedule, now):
    """Combine provider observations and official announcements, including uncollected periods."""
    provider_rows = {row["issue_no"]: row for row in issues}
    by_number = {number: dict(row) for number, row in provider_rows.items()}
    for row in by_number.values():
        official = known_official(verification_at(row.get("official_verification"), now))
        if official.get("sale_start_utc") and official.get("sale_end_utc"):
            row.update(
                sale_start_utc=official["sale_start_utc"], sale_end_utc=official["sale_end_utc"]
            )
    cutoff = _instant(now)
    for number, notice in schedule.items():
        seen, published = notice.get("observed_utc"), notice.get("published_at")
        if not seen or _instant(seen) > cutoff or (published and _instant(published) > cutoff):
            continue
        if notice.get("sale_start_utc") and notice.get("sale_end_utc"):
            row = by_number.setdefault(number, {"issue_no": number})
            row.update(sale_start_utc=notice["sale_start_utc"], sale_end_utc=notice["sale_end_utc"])
        elif number in by_number:
            by_number[number] = dict(provider_rows[number])
    return by_number


def sequence(issues, now):
    """Where the sequence stands at ``now``.

    ``issues`` are mappings with ``issue_no`` and ``sale_end_utc``. Returns a dict:
    ``last_closed`` (highest closed number or None), ``current`` (the period in line that
    closes first, or None), ``ahead`` (on-sale numbers that are not in line yet) and
    ``expected`` (numbers the next period may carry when none is in line: the following
    number, or the first number of the next year).
    """
    now = _instant(now)
    closed, on_sale, announced = [], {}, set()
    for issue in issues:
        end = issue.get("sale_end_utc")
        if not end:
            continue
        number = str(issue["issue_no"])
        if _instant(end) <= now:
            closed.append(number)
        else:
            if issue.get("sale_start_utc") and _instant(issue["sale_start_utc"]) > now:
                announced.add(number)
            else:
                on_sale[number] = _instant(end)
    if not closed:
        in_line, last, expected = set(on_sale), None, ()
    else:
        last = max(closed, key=int)
        following = f"{int(last) + 1:05d}"
        new_year = f"{int(last[:2]) + 1:02d}001"
        expected = (following, new_year)
        in_line = {n for n in on_sale if int(n) <= int(following)}
        for start in expected:
            step = int(start)
            while f"{step:05d}" in on_sale or f"{step:05d}" in announced:
                if f"{step:05d}" in on_sale:
                    in_line.add(f"{step:05d}")
                step += 1
    current = min(in_line, key=lambda n: (on_sale[n], int(n))) if in_line else None
    return dict(
        last_closed=last,
        current=current,
        in_line=sorted(in_line, key=int),
        ahead=sorted(set(on_sale) - in_line, key=int),
        announced=sorted(announced, key=int),
        expected=() if current else expected,
    )
