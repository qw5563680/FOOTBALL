"""Generate the four opening-evidence documents for identity_daily_gate offline.

Every document is produced by actually replaying the archived originals through
the unchanged offline importer on temporary copies. No network requests, no
registry or alias-table writes; the output is exactly the shape the existing
gate verification code consumes (see tests/test_t68_daily_gate_entry.py).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlparse

from zucai.cloud_feeds import digest, export_feeds, restore_feeds
from zucai.identity_binding_archive import FORMAL, _tables, load_archive
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


def _copy_immutables(archive, folder):
    for relative in ("ledger.jsonl",):
        source = Path(archive) / relative
        if source.is_file():
            target = folder / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
    for relative in ("data/runs", "scoreboard", "sfc", "forecasts"):
        source = Path(archive) / relative
        if source.is_dir():
            shutil.copytree(source, folder / relative)


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
    _patch_manifest_created(folder, cutoff)
    return folder


def _carry(root, source_folder, name):
    target = root / name
    shutil.copytree(
        source_folder,
        target,
        ignore=shutil.ignore_patterns("identity_gate_evidence"),
    )
    return target


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

    expected_immutable = dict(verify_forecasts(folder)["files"]) if verify_forecasts(folder) else {}
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


def _sanitize_archive(archive, work):
    """Materialize a manifest-clean copy so restore_feeds accepts every state file."""
    import re

    from zucai.cloud_feeds import (
        FD_LINK_FILE,
        MONTH,
        RESULT_IDENTITY_FILE,
        RESULT_LINK_FILE,
        STATE_FILE,
        STATS_FILES,
    )

    target = work / "restorable"
    shutil.copytree(archive, target)
    manifest_path = target / "feed_manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    allowed = STATS_FILES | {FD_LINK_FILE, RESULT_LINK_FILE, RESULT_IDENTITY_FILE}

    def acceptable(key):
        return key in allowed or re.fullmatch(
            rf"state/(?:teams|matches|results|closing_odds)\.jsonl|"
            rf"state/(?:unmatched_teams|rejected_results)\.csv|"
            rf"state/odds/{MONTH}\.jsonl|matches/{MONTH}\.csv|"
            rf"state/sfc/[0-9]{{5}}\.json|{STATE_FILE}",
            key,
        )

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


def generate(archive, code, *, cutoff, run_id, out_root):
    """Produce the four evidence pieces for one competition; returns the evidence dict.

    All large intermediates (archive copy, state exports) live under ``out_root``,
    which callers must place in a temporary working directory — never inside the
    data archive. References inside the evidence dict are relative to ``out_root``
    and carry the competition prefix.
    """
    from zucai.identity_daily_gate import _recent_official
    from zucai.identity_gate_evidence import export as gate_export
    from zucai.identity_gate_evidence import outcome as state_outcome
    from zucai.identity_gate_evidence import snapshot

    archive = Path(archive)
    out_root = Path(out_root)
    file_root = out_root / str(code)
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

    with TemporaryDirectory(prefix="zucai-evidence-", ignore_cleanup_errors=True) as temporary:
        work = Path(temporary)
        # The gate's held reconciliation reads the audit from the frozen archive:
        # when the archive carries none, materialize an empty list there and say so.
        held_audit_source = "archive"
        if not (Path(archive) / HELD_PATH).is_file():
            held_path = Path(archive) / HELD_PATH
            held_path.parent.mkdir(parents=True, exist_ok=True)
            held_path.write_text(HELD_HEADER, encoding="utf-8")
            manifest_path = Path(archive) / "feed_manifest.json"
            archive_manifest = json.loads(manifest_path.read_bytes())
            archive_manifest["files"][HELD_PATH] = digest(held_path)
            manifest_path.write_text(
                json.dumps(archive_manifest, ensure_ascii=False, indent=2) + "\n"
            )
            held_audit_source = "generated_empty_archive_has_no_held_audit"
        restorable, dropped = _sanitize_archive(archive, work)
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
        before_folder = _export_state(restored, archive, work / "current-before", day, cutoff)
        current_result = _run_importer(restored, games, seen, imported, retain_results=False)
        archive_copy = _carry(file_root, archive, "archive-copy")
        before_folder = _carry(file_root, before_folder, "current-before")
        before_ref = ref(before_folder / "feed_manifest.json")
        after_ref = ref(archive_copy / "feed_manifest.json")
        held_ref = ref(archive_copy / HELD_PATH)
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
        # three states are byte-identical copies of the archive (zero changes).
        repeat_result = _run_importer(restored, games, seen, imported, retain_results=False)
        repeat_after = _carry(file_root, archive, "repeat-after")
        restored_export = _carry(file_root, archive, "restored-export")
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
            before_manifest=ref(archive_copy / "feed_manifest.json"),
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
            ("before_repeat", before_folder),
            ("after_repeat", repeat_after),
            ("restored", restored_export),
        ):
            state_ref = ref(folder / "feed_manifest.json")
            state = gate_export(file_root, _ref(file_root, folder / "feed_manifest.json"))
            snap = snapshot(state, cutoff, league)
            snap["immutable_sha256"] = _immutable_inventory(folder)
            persistence[key] = doc(f"{key}.json", snap)
            persistence[key + "_manifest"] = state_ref

        # ---- Recent 30-day replay.
        start, end = (
            _clock(cutoff).astimezone(BEIJING).date() - timedelta(days=30),
            (_clock(cutoff).astimezone(BEIJING).date() - timedelta(days=1)),
        )
        recent_section = dict(mode="dates", originals=[], official_originals=[])
        originals = _date_originals(archive, code, start, end)
        if not originals:
            raise ValueError(
                f"{code} 30 天窗口内没有日期原件；equivalent 模式（含边界样例）留待下一提交"
            )
        recent_section["originals"] = [
            _prefix(_copy_original(file_root, archive, r), code) for r in originals
        ]
        official_originals = []
        for scan_root in (
            Path(facts["archive_root"]) / "state/catalogue/catalogue/jc",
            Path(facts["archive_root"]) / "state/catalogue/supplement/jc",
        ):
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
                        official_originals.append(dict(path=relative, sha256=digest(page)))
        official_originals = [
            _prefix(_copy_original(file_root, archive, r), code) for r in official_originals
        ]
        recent_section["official_originals"] = official_originals
        official = _recent_official(facts, code, recent_section, out_root, start, end)

        recent_restored = work / "recent/data/feeds.db"
        restore_feeds(restorable, recent_restored)
        _ensure_held(recent_restored.parent)
        recent_before = _export_state(recent_restored, archive, work / "recent-before", day, cutoff)
        outcomes, fixture_ids, raw_hashes = [], set(), set()
        parser = ApiFootball(leagues={code: facts["configs"][code]})
        try:
            for original in originals:
                raw = proof(Path(facts["archive_root"]), original)
                recent_games = parser.parse(raw)
                _run_importer(recent_restored, recent_games, seen, imported, retain_results=False)
                for game in recent_games:
                    fixture_ids.add(f"apif:{game.provider_match_id}")
                raw_hashes.add(original["sha256"])
        finally:
            parser.session.close()
        recent_after = _export_state(recent_restored, archive, work / "recent-after", day, cutoff)
        recent_before = _carry(file_root, recent_before, "recent-before")
        recent_after = _carry(file_root, recent_after, "recent-after")
        recent_section["before_manifest"] = ref(recent_before / "feed_manifest.json")
        recent_section["after_manifest"] = ref(recent_after / "feed_manifest.json")
        before_state = gate_export(file_root, _ref(file_root, recent_before / "feed_manifest.json"))
        after_state = gate_export(file_root, _ref(file_root, recent_after / "feed_manifest.json"))
        inserted_count = 0
        for mid in sorted(fixture_ids):
            old, new = state_outcome(before_state, mid), state_outcome(after_state, mid)
            if new is None:
                continue
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
            inserted_count += derived == "inserted"
            outcomes.append(dict(fixture_id=mid, outcome=derived))
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

    return dict(
        provider_code=code,
        cutoff_utc=cutoff,
        archive_sha256=digest(archive / "feed_manifest.json"),
        current_season_replay=current_section,
        recent_replay=recent_section,
        risk_review=risk_document,
        persistence=persistence,
        sanitized_manifest_exclusions=dropped,
    )
