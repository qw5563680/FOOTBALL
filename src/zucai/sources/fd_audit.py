"""Append-only deduplication for the four fd/fdorg row-level audit CSVs."""

import csv
from pathlib import Path


def append_unique(path, fields, rows, row_key):
    path = Path(path)

    def key(row):
        return tuple(
            str(value or "") for value in (*row_key(row), row.get("raw_sha256"), row.get("reason"))
        )

    seen = set()
    if path.exists() and path.stat().st_size:
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream, strict=True)
            if reader.fieldnames != list(fields):
                raise ValueError("fd/fdorg 审计字段不符，拒绝覆盖历史")
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError("fd/fdorg 审计行不完整，拒绝覆盖历史")
                seen.add(key(row))
    new = []
    for row in rows:
        identity = key(row)
        if identity not in seen:
            seen.add(identity)
            new.append(row)
    if not new:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        if stream.tell() == 0:
            writer.writeheader()
        writer.writerows(new)
