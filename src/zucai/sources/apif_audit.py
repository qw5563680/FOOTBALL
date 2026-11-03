"""APIF-only audit compaction; retain different original hashes and rejection reasons."""

import csv
import io
import json

from zucai.sources.base import _atomic_write

FIELDS = (
    "raw_sha256",
    "row_number",
    "competition_code",
    "league",
    "provider_match_id",
    "home_provider_id",
    "away_provider_id",
    "home",
    "away",
    "kickoff_utc",
    "status",
    "reason",
    "candidates",
)


def _key(row):
    # An unparsable row may have no provider ID; its original row number is evidence.
    identity = row["provider_match_id"] or "row:" + row["row_number"]
    return identity, row["raw_sha256"], row["reason"]


def audit_apif(path, rows, sha):
    previous = []
    if path.exists():
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream, strict=True)
            if reader.fieldnames != list(FIELDS):
                raise ValueError("APIF 审计字段无效，拒绝覆盖已有证据")
            previous = list(reader)
        if any(None in row or any(v is None for v in row.values()) for row in previous):
            raise ValueError("APIF 审计行无效，拒绝覆盖已有证据")
    records = {_key(row): row for row in previous}
    for row in rows:
        item = {key: "" if row.get(key) is None else str(row[key]) for key in FIELDS}
        item.update(
            raw_sha256=sha, candidates=json.dumps(row.get("candidates", []), ensure_ascii=False)
        )
        records.setdefault(_key(item), item)
    if list(records.values()) == previous:
        return
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, FIELDS)
    writer.writeheader()
    writer.writerows(records.values())
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, buffer.getvalue().encode("utf-8"))
