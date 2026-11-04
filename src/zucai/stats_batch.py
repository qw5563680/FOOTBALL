"""Stage technical collections and publish immutable evidence before adopting data."""

import hashlib
import json
import os
import shutil
import sqlite3
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from zucai.cloud_apis import failure_reason
from zucai.cloud_feeds import MANIFEST, archive_info, export_feeds
from zucai.sources.apif_stats import instant
from zucai.sources.base import RawStore, _atomic_write
from zucai.sources.football_data import CONFIG_ROOT, FootballDataCoUk
from zucai.sources.sporttery import BEIJING
from zucai.stats_client import LineupWindowMissed, StatsClient, StatsStopped
from zucai.stats_context import refresh_context
from zucai.stats_identity import reconcile_den_haag
from zucai.stats_probe import parse_response
from zucai.stats_release import publish_raw
from zucai.stats_rows import publish_rows, restore_rows
from zucai.stats_schedule import (
    coverage,
    due,
    fd_units,
    index_units,
    injury_units,
    load_cursor,
    match_units,
    record,
    resume_units,
    unsupported,
)
from zucai.stats_storage import ingest_apif, ingest_fd, link_archive


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(
        path, (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    )


def _copy_evidence(source, target):
    for path in sorted(source.rglob("*")) if source.exists() else []:
        if path.is_symlink():
            raise ValueError("原件归档不允许符号链接")
        if path.is_file():
            dest = target / path.relative_to(source)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists() and dest.read_bytes() != path.read_bytes():
                raise ValueError("原件归档文件名冲突")
            if not dest.exists():
                shutil.copyfile(path, dest)


def _adopt(database, staged, archive, feed, meta, old, backups):
    updates = {database: staged}
    for source in (staged.parent / "state").rglob("*"):
        if source.is_file() and not source.name.endswith(".lock"):
            updates[database.parent / "state" / source.relative_to(staged.parent / "state")] = (
                source
            )
    for key in [*meta["files"], MANIFEST]:
        updates[archive / key] = feed / key
    updates.update({archive / key: None for key in set(old["files"]) - set(meta["files"])})
    completed = []
    backups.mkdir()
    try:
        for i, (target, source) in enumerate(updates.items()):
            target.parent.mkdir(parents=True, exist_ok=True)
            backup = backups / str(i) if target.exists() else None
            if backup:
                os.replace(target, backup)
            completed.append((target, backup))
            if source is not None:
                os.replace(source, target)
    except BaseException:
        for target, backup in reversed(completed):
            target.unlink(missing_ok=True)
            if backup:
                os.replace(backup, target)
        raise


def _quota_only(database, archive, original, report):
    """Charged attempts survive archive failure; no staged observations/cursors do."""
    quota = database.parent / "state/api_football_quota.json"
    if quota.exists():
        key = "state/api_football_quota.json"
        target = archive / key
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, quota.read_bytes())
        original["files"][key] = hashlib.sha256(target.read_bytes()).hexdigest()
    original["collection"] = {**original.get("collection", {}), "stats": report}
    original["collection_status"] = "partial"
    save_json(archive / MANIFEST, original)


def collect_batch(
    database,
    archive,
    selection,
    *,
    release,
    run_id,
    now,
    daily=False,
    resume=True,
    max_requests=7000,
    client_factory=StatsClient,
    fd_factory=FootballDataCoUk,
):
    database, archive = Path(database), Path(archive)
    original = archive_info(archive)
    tag = "stats-raw-" + now.strftime("%Y%m")
    quota_path = database.parent / "state/api_football_quota.json"
    report = dict(
        status="complete",
        requests=0,
        downloaded_files=0,
        inserted=0,
        linked_existing=0,
        rejected_rows=0,
        errors=[],
        run_id=run_id,
        observed_utc=now.isoformat(),
        mode="daily" if daily else "backfill",
        resume=resume,
    )
    with TemporaryDirectory(prefix="stats-stage-", dir=database.parent.parent) as temp:
        root = Path(temp)
        shard_cache = root / "history-shards"
        staged = root / "data" / database.name
        shutil.copytree(database.parent, staged.parent)
        raw_root = staged.parent / "raw"
        assets = root / "assets"
        assets.mkdir()
        state = staged.parent / "state"
        state.mkdir(exist_ok=True)
        cursor_path = state / "stats_backfill_cursor.json"
        cursor = load_cursor(cursor_path)
        source = client_factory(
            store=RawStore(raw_root), quota_path=quota_path, max_requests=max_requests
        )
        stopped = False

        def location(path):
            relative = Path(path).relative_to(raw_root).as_posix()
            return f"release:{tag}/stats-raw-{run_id}.zip#{relative}"

        def handle(item, context=None):
            nonlocal stopped
            if item["key"] not in cursor["records"]:
                record(cursor, item, "pending", now)
            if not due(item, cursor, now):
                return
            reason = unsupported(item, cursor, context)
            if reason:
                record(cursor, item, "unsupported", now, reason=reason)
                return
            if item.get("missed"):
                record(cursor, item, "missed", now, reason="本班未处于开球前 60–30 分钟窗口")
                return
            if stopped:
                return
            endpoint = item["endpoint"]
            if endpoint in ("statistics", "events", "lineups", "players") and not context:
                record(
                    cursor, item, "error", now, reason="索引缺少已归档 fixture 上下文；先运行 index"
                )
                return
            if (
                endpoint in ("statistics", "events", "lineups", "players")
                and item["phase"] != "pregame"
                and context["fixture"]["status"]["short"] not in {"FT", "AET", "PEN"}
            ):
                record(cursor, item, "error", now, reason="完赛 fixture 状态尚未取得，不推断 FT")
                return
            count, page_count = source.request_count, len(source.pages)
            path, metadata = None, {}
            try:
                if endpoint == "fd":
                    fd = fd_factory(store=RawStore(raw_root))
                    try:
                        path = fd.fetch_raw(div=item["div"], season=item["fd_season"])
                    finally:
                        fd.session.close()
                    report["downloaded_files"] += 1
                    metadata = source.store.load_metadata(path)
                    result = ingest_fd(
                        staged,
                        path,
                        div=item["div"],
                        season=item["fd_season"],
                        raw_location=location(path),
                    )
                    record(
                        cursor,
                        item,
                        "error" if result.get("rejected") else "ok",
                        instant(metadata["fetched_at_utc"]),
                        requested=True,
                        received=True,
                        raw_sha256=metadata["sha256"],
                        raw_location=location(path),
                    )
                else:
                    name = "fixtures" if endpoint == "index" else endpoint
                    path = source.fetch(
                        name,
                        league_id=item["league_id"],
                        season=item["season"],
                        fixture_id=item["fixture_id"] or None,
                        kickoff_utc=item.get("kickoff_utc") if item["phase"] == "pregame" else None,
                    )
                    metadata = source.store.load_metadata(path)
                    parameters = (
                        {"league": str(item["league_id"]), "season": str(item["season"])}
                        if endpoint in {"index", "injuries"}
                        else {"fixture": item["fixture_id"]}
                    )
                    resource = name if endpoint in {"index", "injuries"} else "fixtures/" + name
                    parsed = parse_response(path.read_bytes(), resource, parameters)
                    doc = json.loads(path.read_bytes())
                    if parsed["outcome"] == "error":
                        if isinstance(doc.get("errors"), dict) and set(doc["errors"]) & {
                            "plan",
                            "access",
                            "token",
                            "authentication",
                        }:
                            cursor["unsupported"][f"{item['league_id']}:{item['season']}:*"] = (
                                "权限错误，整联赛季暂停待人工复核"
                            )
                            parsed["outcome"] = "unsupported"
                        report["status"] = "partial"
                        result = {}
                    elif endpoint == "index":
                        result = link_archive(
                            staged,
                            path,
                            league_id=item["league_id"],
                            season=item["season"],
                            aliases_path=CONFIG_ROOT / "team_aliases/api_football.json",
                            raw_location=location(path),
                        )
                        if result.get("rejected"):
                            parsed.update(outcome="error", reason="索引存在拒绝行，详见审计")
                    elif endpoint == "injuries":
                        # T31 has no injury table: retain raw evidence and coverage only.
                        result = {}
                    else:
                        result = ingest_apif(
                            staged, path, endpoint=endpoint, raw_location=location(path)
                        )
                        if result.get("rejected"):
                            parsed.update(
                                outcome="error", reason="技术行解析或身份校验拒绝，详见审计"
                            )
                    record(
                        cursor,
                        item,
                        parsed["outcome"],
                        instant(metadata["fetched_at_utc"]),
                        requested=True,
                        received=True,
                        reason=parsed["reason"],
                        null_fields=parsed["null_fields"],
                        raw_sha256=metadata["sha256"],
                        raw_location=location(path),
                    )
                report["inserted"] += result.get("inserted", 0)
                report["linked_existing"] += result.get("linked", 0)
                report["rejected_rows"] += result.get("rejected", 0)
            except LineupWindowMissed as exc:
                record(cursor, item, "missed", source.now(), reason=str(exc))
            except StatsStopped as exc:
                stopped = True
                report["status"] = "partial"
                report["errors"].append(str(exc))
                if source.request_count > count:
                    record(
                        cursor,
                        item,
                        "error",
                        now,
                        requested=True,
                        received=len(source.pages) > page_count,
                        reason=str(exc),
                    )
            except OSError:
                raise  # Failure to archive originals cannot commit the staged batch.
            except Exception as exc:
                if len(source.pages) > page_count:
                    metadata = source.store.load_metadata(source.pages[-1])
                outcome = "error"
                http = metadata.get("status_code") or (
                    source.last_http if endpoint != "fd" else None
                )
                if http in {401, 403}:
                    outcome = "unsupported"
                    cursor["unsupported"][f"{item['league_id']}:{item['season']}:*"] = (
                        "HTTP 权限错误，整联赛季暂停待人工复核"
                    )
                captured = (
                    instant(metadata["fetched_at_utc"]) if metadata.get("fetched_at_utc") else now
                )
                record(
                    cursor,
                    item,
                    outcome,
                    captured,
                    requested=source.request_count > count or endpoint == "fd",
                    received=bool(metadata),
                    reason=failure_reason(exc),
                    http=http,
                    raw_sha256=metadata.get("sha256"),
                    raw_location=location(source.pages[-1])
                    if len(source.pages) > page_count
                    else None,
                )
                report["status"] = "partial"
                report["errors"].append(f"{item['key']}: {failure_reason(exc)}")

        try:
            if original.get("technical_data", {}).get("version") == 2:
                restore_rows(staged, archive, release=release, shard_cache=shard_cache)
            with sqlite3.connect(staged) as db:
                den_haag = dict(
                    db.execute("SELECT name,id FROM teams WHERE name IN ('Den Haag','海牙')")
                )
            report["identity"] = reconcile_den_haag(staged, run_id)
            if not daily:
                for item, _ in resume_units(
                    ((row, None) for row in index_units(selection)), cursor, resume
                ):
                    handle(item)
                for item, _ in resume_units(
                    ((row, None) for row in fd_units(selection)), cursor, resume
                ):
                    handle(item)
            report["context_updates"] = refresh_context(staged, raw_root, location)
            for item, context in resume_units(
                match_units(staged, selection, now, daily=daily), cursor, resume
            ):
                handle(item, context)
            injuries = list(injury_units(selection, now=now if daily else None))
            if daily:
                # Daily league/season requests share one unit per Beijing day.
                injuries = [
                    dict(
                        item,
                        key=item["key"] + ":" + now.astimezone(BEIJING).date().isoformat(),
                        phase="daily",
                    )
                    for item in injuries
                ]
            for item, _ in resume_units(((item, None) for item in injuries), cursor, resume):
                handle(item)
            report["requests"] = source.request_count
            report["remaining_quota"] = source.quota.describe()["remaining_quota"]
            source.session.close()
            if any(
                row["outcome"] in {"error", "pending", "empty"}
                for row in cursor["records"].values()
            ):
                report["status"] = "partial"
            summary = coverage(cursor, now)
            save_json(cursor_path, cursor)
            save_json(state / "stats_coverage.json", summary)
            prior_path = state / "stats_rows_manifest.json"
            previous = json.loads(prior_path.read_bytes()) if prior_path.exists() else None
            if previous and report["identity"]["merged_teams"]:
                previous["identity_redirects"] = [
                    *previous.get("identity_redirects", []),
                    dict(
                        old_team_id=den_haag["Den Haag"],
                        new_team_id=den_haag["海牙"],
                        approval_url="https://github.com/qw5563680/FOOTBALL/pull/49#issuecomment-6627072639",
                    ),
                ]
            # Archive failure here leaves all database/audit/cursor edits in this temp dir.
            if (
                source.request_count
                or report["downloaded_files"]
                or report["context_updates"]
                or report["identity"]["merged_teams"]
                or previous is None
            ):
                raw_archives = []
                if raw_root.exists() and any(path.is_file() for path in raw_root.rglob("*")):
                    raw = publish_raw(raw_root, assets, release=release, tag=tag, run_id=run_id)
                    if raw is not None:
                        raw_archives.append(raw)
                publish_rows(
                    staged,
                    assets,
                    release=release,
                    tag=tag,
                    run_id=run_id,
                    previous=previous,
                    raw_archives=raw_archives,
                    shard_cache=shard_cache,
                )
            if quota_path.exists():
                shutil.copyfile(quota_path, state / quota_path.name)
            feed = root / "feed"
            feed.mkdir()
            collection = {**original.get("collection", {}), "stats": report}
            meta = export_feeds(
                staged,
                feed,
                date.fromisoformat(original["through_beijing"]),
                run_id,
                collection=collection,
            )
            _copy_evidence(raw_root, database.parent / "raw")
            _copy_evidence(assets, database.parent / "stats_assets")
            _adopt(database, staged, archive, feed, meta, original, root / "backups")
            report["coverage"] = summary
            return report
        except Exception as exc:
            source.session.close()
            _copy_evidence(raw_root, database.parent / "raw")
            _copy_evidence(assets, database.parent / "stats_assets")
            report.update(
                status="partial",
                requests=source.request_count,
                inserted=0,
                linked_existing=0,
                archive_failed=True,
                errors=["技术归档失败，该批数据库/游标未提交：" + failure_reason(exc)],
            )
            _quota_only(database, archive, original, report)
            return report
