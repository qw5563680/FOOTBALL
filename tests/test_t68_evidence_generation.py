"""T68 第八节第 10 项：生成的开启证据必须能被真实入口（build_register）核验。"""

import json
from pathlib import Path

from test_t68_daily_gate_entry import CUTOFF, bundle  # noqa: F401
from test_t68_identity_archive_entry import archive as frozen_archive  # noqa: F401
from test_t68_identity_archive_entry import seal
from zucai import identity_daily_gate as gate
from zucai.identity_gate_evidence_gen import _sanitize_archive, generate


def _seed_recent_originals(archive, root):
    """Place the fixture's date captures and JC supplements into the archive itself,
    the shape real data-branch archives carry them in."""
    for day_json in sorted((root / "daily").glob("*.json")):
        if day_json.name.endswith(".meta.json"):
            continue
        day = day_json.stem
        target = archive / f"state/catalogue/daily/api-football/{day}/fixtures_one.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(day_json.read_bytes())
        meta = json.loads(Path(str(day_json) + ".meta.json").read_bytes())
        Path(str(target) + ".meta.json").write_text(json.dumps(meta, ensure_ascii=False))
    for day_json in sorted((root / "jc").glob("*.json")):
        if day_json.name.endswith(".meta.json"):
            continue
        day = day_json.stem
        target = archive / f"state/catalogue/supplement/jc/2026-11/{day}-p1.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(day_json.read_bytes())
        meta = json.loads(Path(str(day_json) + ".meta.json").read_bytes())
        Path(str(target) + ".meta.json").write_text(json.dumps(meta, ensure_ascii=False))
    manifest_path = archive / "feed_manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["created_utc"] = CUTOFF
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    seal(archive)


def test_generated_evidence_is_accepted_by_the_real_entry(bundle):  # noqa: F811
    archive, root, _handcrafted = bundle
    _seed_recent_originals(archive, root)
    out = root / "generated"
    generated = generate(archive, "98", cutoff=CUTOFF, run_id="gen-1", out_root=out)
    result = gate.build_register(
        archive, CUTOFF, codes=["98"], evidence={"98": generated}, evidence_root=out
    )["leagues"][0]
    assert result["teams"]["bound"] == 2
    assert result["matches"]["linked"] == 1
    assert result["status"] == "ready_for_review", result["evidence"]
    assert result["daily_changed"] is False


def test_generated_evidence_is_deterministic(bundle):  # noqa: F811
    archive, root, _handcrafted = bundle
    _seed_recent_originals(archive, root)
    first = generate(archive, "98", cutoff=CUTOFF, run_id="gen-1", out_root=root / "a")
    second = generate(archive, "98", cutoff=CUTOFF, run_id="gen-1", out_root=root / "b")
    for key in ("provider_code", "cutoff_utc", "archive_sha256"):
        assert first[key] == second[key]
    first_register = gate.build_register(
        archive, CUTOFF, codes=["98"], evidence={"98": first}, evidence_root=root / "a"
    )["leagues"][0]
    second_register = gate.build_register(
        archive, CUTOFF, codes=["98"], evidence={"98": second}, evidence_root=root / "b"
    )["leagues"][0]
    assert first_register["blockers"] == second_register["blockers"]
    assert first_register["teams"] == second_register["teams"]
    assert first_register["matches"] == second_register["matches"]


def test_archive_without_held_audit_generates_empty_list(bundle):  # noqa: F811
    archive, root, _handcrafted = bundle
    _seed_recent_originals(archive, root)
    (archive / "state/audit/apif_held_unbound.csv").unlink()
    seal(archive)
    out = root / "generated"
    generated = generate(archive, "98", cutoff=CUTOFF, run_id="gen-1", out_root=out)
    assert generated["current_season_replay"]["held_audit_source"] == (
        "generated_empty_archive_has_no_held_audit"
    )
    result = gate.build_register(
        archive, CUTOFF, codes=["98"], evidence={"98": generated}, evidence_root=out
    )["leagues"][0]
    assert result["teams"]["bound"] == 2
    assert result["matches"]["linked"] == 1
    assert result["status"] == "ready_for_review", result["evidence"]


def test_cloud_identity_evidence_entry_persists_only_small_artifacts(  # noqa: F811
    bundle,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    import shutil
    from datetime import UTC as REAL_UTC
    from datetime import datetime as real_datetime

    from zucai import cloud_feeds

    bundle_archive, root, _handcrafted = bundle
    clean = tmp_path / "clean"
    sanitize_work = tmp_path / "sanitize-work"
    sanitize_work.mkdir()
    restorable, _dropped = _sanitize_archive(bundle_archive, sanitize_work)
    shutil.copytree(restorable, clean)
    # Replace the odd fixture original with a manifest-conforming copy so the
    # clean archive still witnesses the provider identity.
    shutil.rmtree(clean / "state/catalogue/backfill", ignore_errors=True)
    from test_t68_canonical_approval import _seed_provider_original

    _seed_provider_original(bundle_archive, clean)
    # Seed daily originals and jc supplements on disk (unlisted; never in the manifest).
    for day_json in sorted((root / "daily").glob("*.json")):
        if day_json.name.endswith(".meta.json"):
            continue
        day = day_json.stem
        target = clean / f"state/catalogue/daily/api-football/{day}/fixtures_one.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(day_json.read_bytes())
        meta = json.loads(Path(str(day_json) + ".meta.json").read_bytes())
        Path(str(target) + ".meta.json").write_text(json.dumps(meta, ensure_ascii=False))
    for day_json in sorted((root / "jc").glob("*.json")):
        if day_json.name.endswith(".meta.json"):
            continue
        day = day_json.stem
        target = clean / f"state/catalogue/supplement/jc/2026-11/{day}-p1.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(day_json.read_bytes())
        meta = json.loads(Path(str(day_json) + ".meta.json").read_bytes())
        Path(str(target) + ".meta.json").write_text(json.dumps(meta, ensure_ascii=False))
    before = {
        p.relative_to(clean).as_posix(): p.read_bytes() for p in clean.rglob("*") if p.is_file()
    }
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("ZUCAI_OPERATION", "reconcile")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(work / "summary.md"))

    class FakeDatetime:
        UTC = REAL_UTC

        @staticmethod
        def now(tz=None):
            return real_datetime(2026, 11, 14, 16, 30, tzinfo=REAL_UTC)

    monkeypatch.setattr(cloud_feeds, "datetime", FakeDatetime)
    monkeypatch.setattr(
        "sys.argv",
        [
            "feeds",
            "identity-evidence",
            "--archive",
            str(clean),
            "--run-id",
            "ev-1",
            "--leagues",
            "98",
        ],
    )
    cloud_feeds.main()
    register = clean / "state/identity_gate_evidence/ev-1/register.json"
    index = clean / "state/identity_gate_evidence/ev-1/98/index.json"
    assert register.is_file() and index.is_file()
    evidence_meta = json.loads((clean / "feed_manifest.json").read_bytes())
    record = evidence_meta["collection"]["identity_evidence"]
    assert record["status"] == "complete" and record["codes"] == ["98"]
    # Nothing else in the archive changed except the evidence directory, the
    # manifest, and export-derived artifacts (matches/*.csv).
    for p in clean.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(clean).as_posix()
        if (
            rel.startswith("state/identity_gate_evidence/")
            or rel == "feed_manifest.json"
            or rel.startswith("matches/")
            or rel == "state/apif_fixture_index.jsonl"
            or rel.startswith("state/stats")
        ):
            continue
        if rel.endswith(".jsonl") or rel.endswith(".csv"):
            assert p.read_text(encoding="utf-8").replace("\r\n", "\n") == before[rel].decode(
                "utf-8"
            ).replace("\r\n", "\n"), rel
        else:
            assert p.read_bytes() == before[rel], rel
    # Export then restore stays consistent.
    from zucai.cloud_feeds import restore_feeds

    restore_feeds(clean, tmp_path / "restored/data/feeds.db")
