"""Portable SFC state; never restore source match identities from snapshots."""

import json
import logging
import re
from pathlib import Path

LOG = logging.getLogger(__name__)

ISSUE_COLUMNS = (
    "issue_no",
    "sale_end_utc",
    "observed_utc",
    "source_url",
    "raw_sha256",
    "date_basis",
    "snapshot",
)


def validate_state(payload, number, db):
    if not re.fullmatch(r"[0-9]{5}", number) or payload.get("schema_version") != 1:
        raise ValueError("Invalid SFC state version or filename")
    issue, links = payload["issue"], payload["matches"]
    if set(issue) != set(ISSUE_COLUMNS) or issue["issue_no"] != number:
        raise ValueError("Invalid SFC issue identity")
    rows = issue["snapshot"]
    if (
        not all(isinstance(r.get("fixture_id"), str) and r["fixture_id"].isdigit() for r in rows)
        or len(rows) != 14
        or {r["seq"] for r in rows} != set(range(1, 15))
        or len({r["fixture_id"] for r in rows}) != 14
    ):
        raise ValueError("SFC requires fourteen unique fixtures")
    expected = []
    ids = set()
    for row in rows:
        mid = row.get("match_id")
        if mid is None:
            if row.get("matched_identity") is not None or row.get("mapping") not in (
                "unmatched",
                "ambiguous",
            ):
                raise ValueError("Invalid unresolved SFC fixture")
        else:
            if (
                mid in ids
                or not db.execute("SELECT 1 FROM matches WHERE match_id=?", (mid,)).fetchone()
                or not row.get("matched_identity")
            ):
                raise ValueError("Invalid SFC match link")
            ids.add(mid)
            expected.append(dict(issue_no=number, seq=row["seq"], match_id=mid))
    if sorted(links, key=lambda r: r["seq"]) != sorted(expected, key=lambda r: r["seq"]):
        raise ValueError("SFC snapshot links disagree")


def export_sfc_state(db, archive: Path):
    files = []
    for values in db.execute(
        "SELECT " + ",".join(ISSUE_COLUMNS) + " FROM sfc_issues ORDER BY issue_no"
    ):
        issue = dict(zip(ISSUE_COLUMNS, values, strict=True))
        issue["snapshot"] = json.loads(issue["snapshot"]) if issue["snapshot"] is not None else None
        number = issue["issue_no"]
        if issue["snapshot"] is None:
            LOG.warning("Skipping legacy SFC issue %s: missing frozen snapshot", number)
            continue
        links = [
            dict(issue_no=number, seq=s, match_id=m)
            for s, m in db.execute(
                "SELECT seq,match_id FROM sfc_issue_matches WHERE issue_no=? ORDER BY seq",
                (number,),
            )
        ]
        payload = dict(schema_version=1, issue=issue, matches=links)
        validate_state(payload, number, db)
        key = f"state/sfc/{number}.json"
        path = archive / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        files.append(key)
    return files


def restore_sfc_state(db, archive: Path, files):
    payloads = []
    for key in sorted(files):
        if re.fullmatch(r"state/sfc/[0-9]{5}\.json", key):
            payload = json.loads((archive / key).read_text(encoding="utf-8"))
            validate_state(payload, Path(key).stem, db)
            payloads.append(payload)
    for payload in payloads:
        issue = payload["issue"]
        values = [
            json.dumps(issue[k], ensure_ascii=False) if k == "snapshot" else issue[k]
            for k in ISSUE_COLUMNS
        ]
        db.execute(
            "INSERT INTO sfc_issues (" + ",".join(ISSUE_COLUMNS) + ") VALUES (?,?,?,?,?,?,?)",
            values,
        )
        db.executemany(
            "INSERT INTO sfc_issue_matches (issue_no,seq,match_id) VALUES (?,?,?)",
            [(r["issue_no"], r["seq"], r["match_id"]) for r in payload["matches"]],
        )
