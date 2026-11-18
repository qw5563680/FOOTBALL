"""Generate the four opening-evidence documents for identity_daily_gate offline.

Every document is produced by actually replaying the archived originals through
the unchanged offline importer on temporary copies. No network requests, no
registry or alias-table writes; the output is exactly the shape the existing
gate verification code consumes (see tests/test_t68_daily_gate_entry.py).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlparse

from zucai.cloud_feeds import digest, export_feeds, restore_feeds
from zucai.identity_binding_archive import FORMAL, _tables, load_archive
from zucai.identity_gate_evidence import outcome as state_outcome
from zucai.identity_gate_evidence import proof
from zucai.jc_catalogue_kickoffs import _catalogue
from zucai.league_registry import load_registry
from zucai.sources.api_football import ApiFootball
from zucai.sources.apif_ingest import COUNT_KEYS, import_apif_games
from zucai.sources.sporttery import BEIJING

HELD_HEADER = "raw_sha256,league,provider_match_id\n"
HELD_PATH = "state/audit/apif_held_unbound.csv"


def _clock(value):
    instant = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if instant.utcoffset() is None:
        raise ValueError("evidence timestamps must carry a timezone")
    return instant


def _document(root, name, value):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return dict(path=path.relative_to(root).as_posix(), sha256=digest(path))


def _ref(root, path):
    path = Path(path)
    return dict(path=path.relative_to(root).as_posix(), sha256=digest(path))


def _facts(archive, codes, cutoff):
    from zucai.identity_daily_gate import existing_links
    from zucai.identity_gate_inventory import season_windows

    registry = load_registry()
    configs = {
        code: entry["config"]
        for code, entry in registry["providers"]["apif"].items()
        if entry["status"] == "verified" and entry.get("mode") == "import"
    }
    loaded = load_archive(Path(archive), leagues=codes)
    tables, manifest = _tables(Path(archive))
    _, official_leagues, catalogue = _catalogue(Path(archive))
    return dict(
        loaded,
        season_windows=season_windows(Path(archive), cutoff),
        configs=configs,
        archive_root=str(Path(archive).resolve()),
        matches=tables["matches"],
        results=tables["results"],
        result_links=existing_links(Path(archive), tables),
        fd_originals=[],
        catalogue_window=catalogue["window"],
        official_leagues={
            code: [r for r in official_leagues if r["league_abbr"] == config["league"]]
            for code, config in configs.items()
        },
        manifest=manifest,
        gate_cutoff=cutoff,
    )


def _season_originals(facts, code, season):
    rows = [r for r in facts["apif"] if r["competition_id"] == code and r["season"] == season]
    if not rows or not all(r["complete"] for r in rows):
        raise ValueError(f"{code} 当季原件不完整，无法生成当季重放证据")
    provenance = [p for r in rows for p in r["provenance"] if p.get("sha256")]
    return rows, provenance


def _patch_manifest_created(folder, stamp):
    manifest_path = Path(folder) / "feed_manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["created_utc"] = stamp
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return _ref(folder, manifest_path)


def _link_or_copy(source, target):
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def _copy_immutables(archive, folder):
    """Copy frozen artifacts into an export so the gate can re-verify them.

    The archive is never modified; regular files are byte-copied and trees are
    hard-linked (copy2 fallback) into the export folder only.
    """
    archive = Path(archive)
    if (archive / "forecast_manifest.json").is_file():
        shutil.copyfile(archive / "forecast_manifest.json", folder / "forecast_manifest.json")
    if (archive / "ledger.jsonl").is_file():
        (folder / "ledger.jsonl").write_bytes((archive / "ledger.jsonl").read_bytes())
    for relative in ("data/runs", "scoreboard", "sfc"):
        source = archive / relative
        if source.is_dir():
            shutil.copytree(source, folder / relative, copy_function=_link_or_copy)
    if (folder / "forecast_manifest.json").is_file():
        for relative in ("models", "forecasts"):
            source = archive / relative
            if source.is_dir():
                shutil.copytree(source, folder / relative, copy_function=_link_or_copy)


def _immutable_files(folder):
    """Relative paths of the frozen artifacts carried into an export."""
    found = []
    for relative in ("ledger.jsonl", "forecast_manifest.json"):
        if (folder / relative).is_file():
            found.append(relative)
    for base in ("data/runs", "scoreboard", "sfc", "models", "forecasts"):
        root = folder / base
        if root.is_dir():
            found.extend(
                path.relative_to(folder).as_posix()
                for path in sorted(root.rglob("*"))
                if path.is_file()
            )
    return found


def _ensure_held(state_dir):
    path = Path(state_dir) / HELD_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file():
        path.write_text(HELD_HEADER, encoding="utf-8")


def _run_importer(database, games, seen, imported, retain_results):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine = create_engine(f"sqlite:///{Path(database).resolve().as_posix()}")
    try:
        with Session(engine) as session:
            result = import_apif_games(
                session,
                games,
                seen=seen,
                assume=None,
                aliases=json.loads(FORMAL.read_bytes()),
                retain_results=retain_results,
                imported_utc=imported,
            )
            session.commit()
        return result
    finally:
        engine.dispose()


def _parse_season(facts, code, season):
    parser = ApiFootball(leagues={code: facts["configs"][code]})
    try:
        rows, provenance = _season_originals(facts, code, season)
        raw = proof(Path(facts["archive_root"]), provenance[0])
        games = parser.parse(raw, league_id=code, season=season)
        if parser.rejected:
            raise ValueError("当季原件解析存在拒绝行")
        return games, provenance, raw
    finally:
        parser.session.close()


def _export_state(database, archive, folder, day, cutoff):
    export_feeds(database, folder, day, "evidence")
    _copy_immutables(archive, folder)
    manifest_path = folder / "feed_manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    for relative in _immutable_files(folder):
        manifest["files"][relative] = digest(folder / relative)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    _patch_manifest_created(folder, cutoff)
    return folder


def _copy_original(root, archive, ref):
    source = Path(archive) / ref["path"]
    target = root / ref["path"]
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    sidecar = Path(str(source) + ".meta.json")
    if sidecar.is_file():
        shutil.copyfile(sidecar, Path(str(target) + ".meta.json"))
    return dict(path=ref["path"], sha256=ref["sha256"])


def _prefix(item, code):
    item["path"] = f"{code}/{item['path']}"
    return item


def _immutable_inventory(folder):
    from zucai.forecast_archive import verify_forecasts

    forecasts = verify_forecasts(folder)
    expected_immutable = dict(forecasts["files"]) if forecasts else {}
    for candidate in [
        folder / "ledger.jsonl",
        *(folder / "data/runs").glob("**/*"),
        *(folder / "scoreboard").glob("**/*"),
        *(folder / "sfc").glob("**/*"),
    ]:
        if candidate.is_file():
            expected_immutable[candidate.relative_to(folder).as_posix()] = hashlib.sha256(
                candidate.read_bytes()
            ).hexdigest()
    return expected_immutable


def _sanitize_archive(archive, work, target_name="restorable"):
    """Materialize a manifest-clean copy so restore_feeds accepts every state file."""
    from zucai.identity_gate_evidence import restorable_state_file as acceptable

    target = Path(work) / target_name

    def copy_entry(source, target):
        # The manifest is rewritten below (filter + held audit registration);
        # it must be a real copy, never a hard link back into the archive.
        if Path(source).name == "feed_manifest.json":
            shutil.copyfile(source, target)
        else:
            _link_or_copy(source, target)

    shutil.copytree(Path(archive), target, copy_function=copy_entry)
    manifest_path = target / "feed_manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    dropped = sorted(key for key in manifest["files"] if not acceptable(key))
    manifest["files"] = {key: sha for key, sha in manifest["files"].items() if acceptable(key)}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return target, dropped


def _date_originals(archive, code, start, end):
    """Archived UTC-date captures; season originals under backfill/ are excluded."""
    originals = []
    first_utc = datetime.combine(start, datetime.min.time(), BEIJING).astimezone(UTC)
    end_utc = datetime.combine(end + timedelta(days=1), datetime.min.time(), BEIJING).astimezone(
        UTC
    )
    for path in sorted(
        (Path(archive) / "state/catalogue").glob("*/api-football/*/fixtures_*.json")
    ):
        if path.name.endswith(".meta.json"):
            continue
        meta_path = Path(str(path) + ".meta.json")
        if not meta_path.is_file():
            continue
        meta = json.loads(meta_path.read_bytes())
        params = parse_qs(urlparse(meta.get("url", "")).query)
        if set(params) != {"date"} or any(len(values) != 1 for values in params.values()):
            continue
        when = params["date"][0]
        if first_utc.date().isoformat() <= when <= end_utc.date().isoformat():
            relative = path.relative_to(Path(archive)).as_posix()
            originals.append(dict(path=relative, sha256=digest(path)))
    return originals


def _window_bounds(start, end):
    first_utc = datetime.combine(start, datetime.min.time(), BEIJING).astimezone(UTC)
    end_utc = datetime.combine(end + timedelta(days=1), datetime.min.time(), BEIJING).astimezone(
        UTC
    )
    return first_utc, end_utc


def _row_when(row):
    try:
        return datetime.fromtimestamp(int(row["fixture"]["timestamp"]), tz=UTC)
    except (KeyError, TypeError, ValueError):
        return None


def _season_archive_originals(archive, code):
    """Every archived season-scope capture of one competition (load_apif's glob)."""
    originals = []
    for path in sorted(
        (Path(archive) / "state/catalogue").glob("*/backfill/api-football/*/fixtures_*.json")
    ):
        if path.name.endswith(".meta.json"):
            continue
        meta_path = Path(str(path) + ".meta.json")
        if not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_bytes())
            params = parse_qs(urlparse(meta.get("url", "")).query)
            doc = json.loads(path.read_bytes())
        except (OSError, ValueError):
            continue
        if set(params) != {"league", "season"}:
            continue
        if params.get("league") != [str(code)]:
            continue
        if not meta.get("fetched_at_utc"):
            continue
        relative = path.relative_to(Path(archive)).as_posix()
        originals.append((dict(path=relative, sha256=digest(path)), meta, doc))
    return originals


def _equivalent_original(facts, archive, code, start, end, cutoff):
    """Pick the season original that can stand in for the missing date captures."""
    from zucai.identity_daily_gate import _fresh_fixtures, listed_utc

    first_utc, end_utc = _window_bounds(start, end)
    expected = [
        r
        for r in facts["apif"]
        if r["competition_id"] == code
        # A postponed or unscheduled fixture has no clock; like the checker, place it by the
        # date the provider page lists it under.
        and listed_utc(r)
        and first_utc <= _clock(listed_utc(r)) < end_utc
    ]
    if not expected:
        raise ValueError(f"{code} 30 天窗口内没有接口场次，无法生成 equivalent 重放证据")

    stale = sorted(mid for mid, ok in _fresh_fixtures(expected, end_utc).items() if not ok)
    if stale:
        raise ValueError(
            f"{code} 30 天窗口内有 {len(stale)} 场缺少窗口结束之后的完整捕获（如 {stale[:3]}）；"
            "请先对该联赛跑目录回填并重新导入，再生成开启证据"
        )
    expected_ids = {str(r["match_id"]).split(":", 1)[1] for r in expected}
    candidates = []
    for ref, meta, doc in _season_archive_originals(archive, code):
        fetched = _clock(meta["fetched_at_utc"])
        if not end_utc <= fetched <= _clock(cutoff):
            continue
        window_ids = {
            str(r["fixture"]["id"])
            for r in doc.get("response", [])
            if str(r.get("league", {}).get("id")) == str(code)
            and (_when := _row_when(r)) is not None
            and first_utc <= _when < end_utc
        }
        if window_ids == expected_ids:
            candidates.append((fetched, ref))
    if not candidates:
        raise ValueError(
            f"{code} 归档里没有窗口结束之后捕获、且窗口场次与库内完全一致的赛季原件；"
            "请先对该联赛跑目录回填并重新导入，再生成开启证据"
        )
    candidates.sort(key=lambda item: item[0])
    return candidates[-1][1]


def _classify(before_state, after_state, fixture_ids, import_results, rejected_rows):
    """Risk labels first, then state transitions; unresolvable rows raise."""
    risks = {}
    for label, items in (
        ("held_unbound", import_results.get("held", [])),
        ("suspected", import_results.get("suspected", [])),
        ("rejected", import_results.get("rejected", [])),
    ):
        for item in items:
            audit = item.get("audit", item)
            risks[f"apif:{audit['provider_match_id']}"] = label
    for item in rejected_rows:
        risks[f"apif:{item['provider_match_id']}"] = "rejected"
    outcomes, inserted = [], 0
    for mid in sorted(fixture_ids):
        old, new = state_outcome(before_state, mid), state_outcome(after_state, mid)
        if mid in risks:
            outcomes.append(dict(fixture_id=mid, outcome=risks[mid]))
            continue
        if new is None:
            raise ValueError(f"{mid} 未落库且无风险标注，无法生成重放结论")
        if mid not in before_state["matches"] and mid in after_state["matches"]:
            derived = "inserted"
        elif old == new:
            derived = "unchanged"
        elif old:
            derived = "updated"
        elif new[0]["match_id"] == mid:
            derived = "inserted"
        else:
            derived = "linked"
        if new[0]["status"] == "cancelled":
            derived = "cancelled"
        inserted += derived == "inserted"
        outcomes.append(dict(fixture_id=mid, outcome=derived))
    return outcomes, inserted


def generate(archive, code, *, cutoff, run_id, out_root, restorable=None, held_audit_source=None):
    """Produce the four evidence pieces for one competition; returns the evidence dict.

    All large intermediates (archive copy, state exports) live under ``out_root``,
    which callers must place in a temporary working directory — never inside the
    data archive. The archive itself is only ever read; a shared sanitized copy
    (``restorable``) is reused across competitions when the caller builds one.
    References inside the evidence dict are relative to ``out_root`` and carry the
    competition prefix.
    """
    from zucai.identity_daily_gate import BOUNDARY_TESTS, RISKS, _recent_official
    from zucai.identity_gate_evidence import export as gate_export
    from zucai.identity_gate_evidence import snapshot

    archive = Path(archive)
    out_root = Path(out_root)
    file_root = out_root / str(code)
    if file_root.exists():
        # 上一次运行留下的陈旧证据目录会让 export_feeds 把已登记的
        # forecast_manifest.json 当普通归档清单解析并报“未知归档文件”，
        # 必须先清空再重建。
        shutil.rmtree(file_root, ignore_errors=True)
    file_root.mkdir(parents=True, exist_ok=True)

    def doc(name, value):
        item = _document(file_root, name, value)
        item["path"] = f"{code}/{name}"
        return item

    def ref(path):
        item = _ref(file_root, path)
        item["path"] = f"{code}/{item['path']}"
        return item

    facts = _facts(archive, [code], cutoff)
    season = facts["configs"][code]["current_season"]
    if not season:
        raise ValueError(f"{code} 登记无 current_season，无法生成当季重放证据")
    league = facts["configs"][code]["league"]
    day = _clock(cutoff).astimezone(BEIJING).date()
    imported = _clock(cutoff)

    generation_errors = {}
    recent_section = risk_document = None
    with TemporaryDirectory(prefix="zucai-evidence-", ignore_cleanup_errors=True) as temporary:
        work = Path(temporary)
        # The gate's held reconciliation reads the audit from a materialized state:
        # when the archive carries none, an empty header-only list is materialized
        # in the temporary copy only — the archive is never modified.
        if restorable is None:
            restorable, dropped = _sanitize_archive(archive, work)
        else:
            restorable = Path(restorable)
            dropped = []
        if held_audit_source is None:
            held_audit_source = (
                "archive"
                if (Path(archive) / HELD_PATH).is_file()
                else "generated_empty_archive_has_no_held_audit"
            )
        if held_audit_source != "archive" and not (restorable / HELD_PATH).is_file():
            held_path = restorable / HELD_PATH
            held_path.parent.mkdir(parents=True, exist_ok=True)
            held_path.write_text(HELD_HEADER, encoding="utf-8")
            restorable_manifest_path = restorable / "feed_manifest.json"
            restorable_manifest = json.loads(restorable_manifest_path.read_bytes())
            restorable_manifest["files"][HELD_PATH] = digest(held_path)
            restorable_manifest_path.write_text(
                json.dumps(restorable_manifest, ensure_ascii=False, indent=2) + "\n"
            )
        games, provenance, _raw = _parse_season(facts, code, season)
        source_ref = provenance[0]
        meta = json.loads(
            (Path(facts["archive_root"]) / (source_ref["path"] + ".meta.json")).read_bytes()
        )
        seen = _clock(meta["fetched_at_utc"])

        # ---- Current-season replay: restore, replay once, export before/after.
        restored = work / "current/data/feeds.db"
        restore_feeds(restorable, restored)
        _ensure_held(restored.parent)
        before_folder = _export_state(restored, archive, file_root / "current-before", day, cutoff)
        current_result = _run_importer(restored, games, seen, imported, retain_results=False)
        after_folder = _export_state(restored, archive, file_root / "current-after", day, cutoff)
        before_ref = ref(before_folder / "feed_manifest.json")
        after_ref = ref(after_folder / "feed_manifest.json")
        held_ref = ref(after_folder / HELD_PATH)
        scope = dict(
            code=code,
            season=season,
            status="complete",
            parsed_matches=len(games),
            raw={"sha256": source_ref["sha256"]},
            **{key: int(current_result.get(key, 0)) for key in COUNT_KEYS},
        )
        invocation = dict(
            run_id=run_id,
            provider_code=code,
            season=season,
            source_sha256=source_ref["sha256"],
            retain_results=False,
            assume_known_after_seconds=None,
            imported_utc=imported.isoformat(),
        )
        options = dict(
            **invocation,
            before_manifest_sha256=before_ref["sha256"],
            after_manifest_sha256=after_ref["sha256"],
            cutoff_utc=cutoff,
            formal_aliases_sha256=hashlib.sha256(FORMAL.read_bytes()).hexdigest(),
        )
        options["invocation_evidence"] = doc("current-invocation.json", invocation)
        current_section = dict(
            report=doc(
                "current-report.json",
                dict(run_id=run_id, processed_files=1, scopes=[scope]),
            ),
            replay_options=doc("current-options.json", options),
            before_manifest=before_ref,
            after_manifest=after_ref,
            held_before=held_ref,
            held_before_manifest=after_ref,
            held_audit_source=held_audit_source,
        )

        # ---- Persistence: a second, distinct replay must insert zero rows; the
        # three states are real exports (before = byte-copy of current-after,
        # after = post-second-replay export, restored = export->restore->export).
        repeat_result = _run_importer(restored, games, seen, imported, retain_results=False)
        repeat_after = _export_state(restored, archive, file_root / "repeat-after", day, cutoff)
        repeat_before = file_root / "repeat-before"
        shutil.copytree(after_folder, repeat_before, copy_function=_link_or_copy)
        restorable_after, _ = _sanitize_archive(repeat_after, work, target_name="restorable-after")
        restored_db = work / "restored/data/feeds.db"
        restore_feeds(restorable_after, restored_db)
        _ensure_held(restored_db.parent)
        restored_export = _export_state(
            restored_db, archive, file_root / "restored-export", day, cutoff
        )
        repeat_report = dict(
            run_id=f"{run_id}-repeat",
            processed_files=1,
            inserted=int(repeat_result.get("inserted", 0)),
            status="complete",
            scopes=[
                dict(
                    code=code,
                    season=season,
                    status="complete",
                    parsed_matches=len(games),
                    raw={"sha256": source_ref["sha256"]},
                    **{key: int(repeat_result.get(key, 0)) for key in COUNT_KEYS},
                )
            ],
        )
        persistence = dict(
            repeat_report=doc("repeat-report.json", repeat_report),
            before_manifest=ref(repeat_before / "feed_manifest.json"),
            after_manifest=ref(repeat_after / "feed_manifest.json"),
            restored_manifest=ref(restored_export / "feed_manifest.json"),
        )
        repeat_invocation = dict(invocation, run_id=f"{run_id}-repeat")
        repeat_options = dict(
            **repeat_invocation,
            before_manifest_sha256=persistence["before_manifest"]["sha256"],
            after_manifest_sha256=persistence["after_manifest"]["sha256"],
            cutoff_utc=cutoff,
            formal_aliases_sha256=hashlib.sha256(FORMAL.read_bytes()).hexdigest(),
        )
        repeat_options["invocation_evidence"] = doc("repeat-invocation.json", repeat_invocation)
        persistence["replay_options"] = doc("repeat-options.json", repeat_options)
        for key, folder in (
            ("before_repeat", repeat_before),
            ("after_repeat", repeat_after),
            ("restored", restored_export),
        ):
            state_ref = ref(folder / "feed_manifest.json")
            state = gate_export(file_root, _ref(file_root, folder / "feed_manifest.json"))
            snap = snapshot(state, cutoff, league)
            snap["immutable_sha256"] = _immutable_inventory(folder)
            persistence[key] = doc(f"{key}.json", snap)
            persistence[key + "_manifest"] = state_ref

        try:
            # ---- Recent 30-day replay (dates mode first; equivalent mode when the
            # archive carries no UTC-date captures).
            start, end = (
                _clock(cutoff).astimezone(BEIJING).date() - timedelta(days=30),
                (_clock(cutoff).astimezone(BEIJING).date() - timedelta(days=1)),
            )
            first_utc, end_utc = _window_bounds(start, end)
            originals = _date_originals(archive, code, start, end)
            if originals:
                recent_mode = "dates"
            else:
                recent_mode = "equivalent"
                chosen_original = _equivalent_original(facts, archive, code, start, end, cutoff)
                originals = [chosen_original]
            recent_section = dict(mode=recent_mode, originals=[], official_originals=[])
            recent_section["originals"] = [
                _prefix(_copy_original(file_root, archive, r), code) for r in originals
            ]
            official_originals = []
            scan_roots = [
                Path(facts["archive_root"]) / "state/catalogue/catalogue/jc",
                Path(facts["archive_root"]) / "state/catalogue/supplement/jc",
            ]
            scan_roots += sorted(Path(facts["archive_root"]).glob("state/catalogue/*/jc"))
            seen_pages = set()
            for scan_root in scan_roots:
                for month_dir in sorted(scan_root.glob("*-*")):
                    for page in sorted(month_dir.glob("*-p*.json")):
                        if page.name.endswith(".meta.json"):
                            continue
                        try:
                            when = date.fromisoformat(page.stem.split("-p")[0])
                        except ValueError:
                            continue
                        if start <= when <= end:
                            relative = page.relative_to(Path(facts["archive_root"])).as_posix()
                            if relative not in seen_pages:
                                seen_pages.add(relative)
                                official_originals.append(dict(path=relative, sha256=digest(page)))
            official_originals = [
                _prefix(_copy_original(file_root, archive, r), code) for r in official_originals
            ]
            recent_section["official_originals"] = official_originals
            official = _recent_official(facts, code, recent_section, out_root, start, end)

            recent_restored = work / "recent/data/feeds.db"
            restore_feeds(restorable, recent_restored)
            _ensure_held(recent_restored.parent)
            recent_before = _export_state(
                recent_restored, archive, file_root / "recent-before", day, cutoff
            )
            outcomes, fixture_ids, raw_hashes = [], set(), set()
            import_results = dict(held=[], suspected=[], rejected=[])
            rejected_rows = []
            parser = ApiFootball(leagues={code: facts["configs"][code]})
            if recent_mode == "equivalent":
                # Replay on a copy so the pre-replay export stays the baseline.
                import_db = work / "recent/import.db"
                shutil.copyfile(recent_restored, import_db)
                chosen_ref = originals[0]
                try:
                    raw = proof(Path(facts["archive_root"]), chosen_ref)
                    chosen_doc = json.loads(raw)
                    recent_games = parser.parse(raw)
                    rejected_rows = list(parser.rejected)
                finally:
                    parser.session.close()
                if rejected_rows:
                    raise ValueError(f"{code} equivalent 原件存在解析拒绝行，无法生成重放结论")
                chosen_meta = json.loads(
                    (Path(facts["archive_root"]) / (chosen_ref["path"] + ".meta.json")).read_bytes()
                )
                import_results = _run_importer(
                    import_db,
                    recent_games,
                    _clock(chosen_meta["fetched_at_utc"]),
                    imported,
                    retain_results=False,
                )
                for game in recent_games:
                    fixture_ids.add(f"apif:{game.provider_match_id}")
                raw_hashes.add(chosen_ref["sha256"])
                # The claimed denominator is the 30-day window rows only.
                fixture_ids = {
                    f"apif:{int(r['fixture']['id'])}"
                    for r in chosen_doc.get("response", [])
                    if str(r.get("league", {}).get("id")) == str(code)
                    and (_when := _row_when(r)) is not None
                    and first_utc <= _when < end_utc
                }
                recent_db = import_db
            else:
                recent_db = recent_restored
                try:
                    for original in originals:
                        raw = proof(Path(facts["archive_root"]), original)
                        recent_games = parser.parse(raw)
                        original_meta = json.loads(
                            (
                                Path(facts["archive_root"]) / (original["path"] + ".meta.json")
                            ).read_bytes()
                        )
                        _run_importer(
                            recent_db,
                            recent_games,
                            _clock(original_meta["fetched_at_utc"]),
                            imported,
                            retain_results=False,
                        )
                        for game in recent_games:
                            fixture_ids.add(f"apif:{game.provider_match_id}")
                        raw_hashes.add(original["sha256"])
                finally:
                    parser.session.close()
            recent_after = _export_state(
                recent_db, archive, file_root / "recent-after", day, cutoff
            )
            recent_section["before_manifest"] = ref(recent_before / "feed_manifest.json")
            recent_section["after_manifest"] = ref(recent_after / "feed_manifest.json")
            before_state = gate_export(
                file_root, _ref(file_root, recent_before / "feed_manifest.json")
            )
            after_state = gate_export(
                file_root, _ref(file_root, recent_after / "feed_manifest.json")
            )
            outcomes, inserted_count = _classify(
                before_state, after_state, fixture_ids, import_results, rejected_rows
            )
            risk_ids = sorted(
                {r["fixture_id"] for r in outcomes if r["outcome"] in {"suspected", "rejected"}}
            )
            recent_section["risk_ids"] = risk_ids
            recent_invocation = dict(invocation, run_id=f"{run_id}-recent")
            recent_options = dict(
                **recent_invocation,
                before_manifest_sha256=recent_section["before_manifest"]["sha256"],
                after_manifest_sha256=recent_section["after_manifest"]["sha256"],
                cutoff_utc=cutoff,
                formal_aliases_sha256=hashlib.sha256(FORMAL.read_bytes()).hexdigest(),
            )
            recent_options["invocation_evidence"] = doc("recent-invocation.json", recent_invocation)
            recent_section["replay_options"] = doc("recent-options.json", recent_options)
            recent_section["results"] = doc(
                "recent-report.json",
                dict(
                    fixture_ids=sorted(fixture_ids),
                    raw_sha256=sorted(raw_hashes),
                    inserted=inserted_count,
                    risk_ids=risk_ids,
                    outcomes=outcomes,
                    official_match_ids=sorted(official["official_match_ids"]),
                ),
            )
            if recent_mode == "equivalent":
                # 边界样例只接受归档里真实存在、可直接引用的原件。本程序不构造样例，
                # 也不把别的原件的抓取地址与时间挪给改写过的页面。六类边界处置由
                # identity_daily_gate.BOUNDARY_TESTS 列出的离线测试统一保证。
                recent_section["boundary_cases"] = doc("recent-boundary-cases.json", [])
                recent_section["boundary_missing"] = [
                    dict(
                        category=category,
                        reason="归档内没有可直接引用的真实样例，未构造；由离线测试覆盖",
                        covered_by_test=BOUNDARY_TESTS[category],
                    )
                    for category in sorted(RISKS)
                ]

            # ---- Risk review.
            risk_rows = []
            for risk in [r for r in outcomes if r["outcome"] in {"suspected", "rejected"}]:
                evidence = doc(
                    f"risk-{risk['fixture_id'].replace(':', '-')}.json",
                    dict(kind="replay_risk", fixture_id=risk["fixture_id"]),
                )
                risk_rows.append(
                    dict(
                        fixture_id=risk["fixture_id"],
                        reason="接口与库内既有比赛疑似重复，保持隔离待人工核验",
                        disposition="quarantined",
                        review_evidence=evidence,
                    )
                )
            risk_document = doc("risk-review.json", risk_rows)
        except Exception as exc:  # noqa: BLE001 - 如实记录未能生成的部分，不中断其余证据
            generation_errors["recent_replay"] = f"{type(exc).__name__}: {exc}"
            recent_section = risk_document = None

    evidence = dict(
        provider_code=code,
        cutoff_utc=cutoff,
        archive_sha256=digest(archive / "feed_manifest.json"),
        current_season_replay=current_section,
        persistence=persistence,
        sanitized_manifest_exclusions=dropped,
        generation_errors=generation_errors,
    )
    if recent_section is not None:
        evidence["recent_replay"] = recent_section
        evidence["risk_review"] = risk_document
    return evidence
