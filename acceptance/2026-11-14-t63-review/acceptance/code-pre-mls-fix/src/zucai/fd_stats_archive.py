"""Optional FD identity evidence, separate from legacy technical row formats."""

import json
import sqlite3

from zucai.fd_stats_links import FD_LINK_COLUMNS, FD_LINK_FILE, validate_fd_links


def validate_fd_links_manifest(meta):
    """A descriptor and its hash-listed file must either both exist or both be absent."""
    block = meta.get("fd_stats_links")
    listed = FD_LINK_FILE in meta["files"]
    if "fd_stats_links" not in meta and not listed:
        return False
    if (
        not listed
        or not isinstance(block, dict)
        or set(block) != {"version", "columns", "count"}
        or type(block.get("version")) is not int
        or block["version"] != 1
        or block.get("columns") != list(FD_LINK_COLUMNS)
        or type(block.get("count")) is not int
        or block["count"] < 1
    ):
        raise ValueError("fd 统计关联 manifest 字段、文件或行数无效")
    return True


def export_fd_links(db, archive):
    """Export only nonempty, validated evidence; old feeds remain descriptor-free."""
    links = validate_fd_links(db)
    if not links:
        return None
    target = archive / FD_LINK_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as stream:
        for row in db.execute(
            f"SELECT {','.join(FD_LINK_COLUMNS)} FROM fd_stats_links ORDER BY provider_fixture_id"
        ):
            stream.write(json.dumps(list(row), ensure_ascii=False, separators=(",", ":")) + "\n")
    return {"version": 1, "columns": list(FD_LINK_COLUMNS), "count": len(links)}


def restore_fd_links(db, archive, meta):
    """Stage evidence before technical rows; the caller owns the atomic DB replacement."""
    if not validate_fd_links_manifest(meta):
        return
    names = ",".join(FD_LINK_COLUMNS)
    placeholders = ",".join("?" for _ in FD_LINK_COLUMNS)
    count = 0
    with (archive / FD_LINK_FILE).open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if not isinstance(row, list) or len(row) != len(FD_LINK_COLUMNS):
                raise ValueError("fd 统计关联行字段无效")
            try:
                db.execute(f"INSERT INTO fd_stats_links ({names}) VALUES ({placeholders})", row)
            except (sqlite3.DatabaseError, TypeError) as exc:
                raise ValueError("fd 统计关联行类型、身份或唯一性无效") from exc
            count += 1
    if count != meta["fd_stats_links"]["count"]:
        raise ValueError("fd 统计关联行数校验失败")
    validate_fd_links(db)
