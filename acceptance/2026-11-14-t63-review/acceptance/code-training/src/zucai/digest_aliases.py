"""Read-only presentation of the archived JC names awaiting human review."""

import json

from zucai.digest_display import _text
from zucai.forecast_archive import _path
from zucai.scoreboard.score import instant


def pending_name_lines(archive, feed, now):
    """Show five pending entries and their first observation known at the cutoff."""
    path = "state/alias_candidates/jc.json"
    if path not in feed["files"]:
        return []
    candidates = json.loads(_path(archive, path).read_bytes())
    pending = []
    for league, names in candidates.items():
        for name, entry in names.items():
            reviewed = entry.get("review", {}).get("recorded_utc")
            if entry.get("status") != "pending" and not (
                reviewed and instant(reviewed) > instant(now)
            ):
                continue
            observations = [
                row
                for row in entry.get("evidence", [])
                if row.get("captured_utc") and instant(row["captured_utc"]) <= instant(now)
            ]
            if not observations:
                continue
            first = min(
                observations,
                key=lambda row: (instant(row["captured_utc"]), row.get("match_id", "")),
            )
            pending.append((league, name, first.get("match_id")))
    if not pending:
        return []
    details = [
        f"{_text(name)}（{_text(league)}，首次比赛 {_text(match_id)}）"
        for league, name, match_id in sorted(pending)[:5]
    ]
    if len(pending) > 5:
        details.append(f"其余 {len(pending) - 5} 个")
    return [f"竞彩待核新队名 {len(pending)} 个：{'；'.join(details)}。"]
