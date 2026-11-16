"""T68 第八节第 10 项：生成的开启证据必须能被真实入口（build_register）核验。"""

import hashlib
import json
from pathlib import Path

import pytest

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


def _seed_equivalent_archive(archive, root):
    """JC supplements plus a freshly-captured season original, but NO date
    captures: the generator must take the equivalent mode."""
    for day_json in sorted((root / "jc").glob("*.json")):
        if day_json.name.endswith(".meta.json"):
            continue
        day = day_json.stem
        target = archive / f"state/catalogue/supplement/jc/2026-11/{day}-p1.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(day_json.read_bytes())
        meta = json.loads(Path(str(day_json) + ".meta.json").read_bytes())
        Path(str(target) + ".meta.json").write_text(json.dumps(meta, ensure_ascii=False))
    # The season original must be captured after the window end and at most at
    # the gate cutoff; CUTOFF sits on the window's end boundary, so it qualifies.
    for meta_path in (archive / "state/catalogue").glob("**/fixtures_one.json.meta.json"):
        meta = json.loads(meta_path.read_bytes())
        meta["fetched_at_utc"] = CUTOFF
        meta_path.write_text(json.dumps(meta, ensure_ascii=False))
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


def test_forecast_archive_carries_into_exports_and_passes_the_gate(bundle):  # noqa: F811
    """Real archives carry a root forecast manifest plus models; the exports must
    preserve both (the second review's crash: forecasts/ without the manifest)."""
    archive, root, _handcrafted = bundle
    _seed_recent_originals(archive, root)
    (archive / "models").mkdir()
    latest = {"models": {}}
    (archive / "models/latest.json").write_text(json.dumps(latest) + "\n", encoding="utf-8")
    manifest = {
        "version": 1,
        "algorithm": "sha256",
        "runs": {},
        "files": {
            "models/latest.json": hashlib.sha256(
                (archive / "models/latest.json").read_bytes()
            ).hexdigest()
        },
    }
    (archive / "forecast_manifest.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    seal(archive)
    out = root / "generated"
    generated = generate(archive, "98", cutoff=CUTOFF, run_id="gen-1", out_root=out)
    for name in (
        "current-before",
        "current-after",
        "repeat-before",
        "repeat-after",
        "restored-export",
        "recent-before",
        "recent-after",
    ):
        assert (out / "98" / name / "forecast_manifest.json").is_file(), name
        assert (out / "98" / name / "models/latest.json").is_file(), name
    result = gate.build_register(
        archive, CUTOFF, codes=["98"], evidence={"98": generated}, evidence_root=out
    )["leagues"][0]
    assert result["teams"]["bound"] == 2
    assert result["matches"]["linked"] == 1
    assert result["status"] == "ready_for_review", result["evidence"]


def test_failed_generation_never_modifies_the_archive(bundle, monkeypatch):  # noqa: F811
    import zucai.identity_gate_evidence_gen as evidence_gen

    archive, root, _handcrafted = bundle
    _seed_recent_originals(archive, root)
    before = {
        p.relative_to(archive).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in archive.rglob("*")
        if p.is_file()
    }
    original = evidence_gen._export_state
    calls = {"count": 0}

    def flaky(database, source_archive, folder, day, cutoff):
        calls["count"] += 1
        if calls["count"] == 4:
            raise RuntimeError("injected export failure")
        return original(database, source_archive, folder, day, cutoff)

    monkeypatch.setattr(evidence_gen, "_export_state", flaky)
    with pytest.raises(RuntimeError, match="injected export failure"):
        generate(archive, "98", cutoff=CUTOFF, run_id="gen-fail", out_root=root / "fail-out")
    after = {
        p.relative_to(archive).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in archive.rglob("*")
        if p.is_file()
    }
    assert after == before, set(after) ^ set(before)


def _entry_archive(bundle, tmp_path, monkeypatch):  # noqa: F811
    """A manifest-clean archive plus the environment the online entry runs in."""
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
    return clean, before


def test_cloud_identity_evidence_entry_persists_only_small_artifacts(  # noqa: F811
    bundle,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    from zucai import cloud_feeds

    clean, before = _entry_archive(bundle, tmp_path, monkeypatch)
    # 体积限制：超过阈值的状态快照只登记哈希与字节数，不落盘。
    monkeypatch.setattr(cloud_feeds, "EVIDENCE_DOCUMENT_LIMIT", 600)
    cloud_feeds.main()
    register = clean / "state/identity_gate_evidence/ev-1/register.json"
    index = clean / "state/identity_gate_evidence/ev-1/98/index.json"
    assert register.is_file() and index.is_file()
    documents = json.loads(index.read_bytes())["documents"]
    assert {item["persisted"] for item in documents.values()} == {True, False}
    for name, item in documents.items():
        assert (index.parent / name).is_file() == item["persisted"] == (item["bytes"] <= 600)
        assert len(item["sha256"]) == 64
    assert {p.name for p in index.parent.iterdir()} == {"index.json"} | {
        name for name, item in documents.items() if item["persisted"]
    }
    persisted = json.loads((clean / "feed_manifest.json").read_bytes())["collection"][
        "identity_evidence"
    ]["leagues"]["98"]["persisted_bytes"]
    assert 0 < persisted <= cloud_feeds.EVIDENCE_LEAGUE_LIMIT
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


def test_equivalent_mode_never_fabricates_boundary_samples(bundle):  # noqa: F811
    """没有日期原件时走 equivalent 模式：真实窗口照常重放；程序不构造边界样例，
    也不借用别的原件的抓取元数据。六类边界处置由离线测试保证，开启检查如实写明这一点。"""
    archive, root, _handcrafted = bundle
    _seed_equivalent_archive(archive, root)
    out = root / "generated"
    generated = generate(archive, "98", cutoff=CUTOFF, run_id="gen-1", out_root=out)
    assert generated["generation_errors"] == {}
    assert generated["recent_replay"]["mode"] == "equivalent"
    assert json.loads((out / "98" / "recent-boundary-cases.json").read_bytes()) == []
    assert {
        item["category"]: item["covered_by_test"]
        for item in generated["recent_replay"]["boundary_missing"]
    } == gate.BOUNDARY_TESTS
    # 证据目录里除了归档原件的原样副本，没有任何新写出的“原件”。
    assert not list((out / "98").glob("case-*"))
    originals = {
        p.relative_to(out / "98").as_posix()
        for p in (out / "98" / "state" / "catalogue").rglob("*")
        if p.is_file()
    }
    assert originals
    for relative in originals:
        assert (out / "98" / relative).read_bytes() == (archive / relative).read_bytes(), relative
    result = gate.build_register(
        archive, CUTOFF, codes=["98"], evidence={"98": generated}, evidence_root=out
    )["leagues"][0]
    assert result["status"] == "ready_for_review", result["evidence"]
    assert result["evidence"]["recent_replay"]["boundary"] == dict(
        real_samples=[], covered_by_tests=gate.BOUNDARY_TESTS
    )


def test_boundary_categories_must_be_known_and_unique():
    """附了样例就得是已知类别且不重复；样例本身仍按原规则逐条核对。"""
    for cases in (
        [dict(category="made_up")],
        [dict(category="unbound"), dict(category="unbound")],
    ):
        with pytest.raises(ValueError, match="boundary categories invalid"):
            gate._boundary_cases(
                dict(apif=[], configs={"98": dict(league="日职")}), "98", cases, None
            )


def test_entry_keeps_every_league_in_the_gate_when_generation_fails(  # noqa: F811
    bundle,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    """证据生成失败的赛事仍进入开启检查：入口正常结束，登记如实列出缺项与失败原因。"""
    import zucai.identity_gate_evidence_gen as evidence_gen
    from zucai import cloud_feeds

    clean, _before = _entry_archive(bundle, tmp_path, monkeypatch)

    def broken(*args, **kwargs):
        raise ValueError("注入的生成失败")

    monkeypatch.setattr(evidence_gen, "generate", broken)
    cloud_feeds.main()
    run_dir = clean / "state/identity_gate_evidence/ev-1"
    league = json.loads((run_dir / "register.json").read_bytes())["leagues"][0]
    assert league["provider_code"] == "98"
    assert league["status"] == "insufficient_evidence"
    assert set(league["missing_evidence"]) >= {
        "current_season_replay",
        "recent_replay",
        "risk_review",
        "persistence",
    }
    index = json.loads((run_dir / "98/index.json").read_bytes())
    assert index["generation_errors"] == {"all": "ValueError: 注入的生成失败"}
    record = json.loads((clean / "feed_manifest.json").read_bytes())["collection"][
        "identity_evidence"
    ]
    assert record["leagues"]["98"]["generation_errors"] == {"all": "ValueError: 注入的生成失败"}
    assert "未能生成（all）：ValueError: 注入的生成失败" in (Path.cwd() / "summary.md").read_text(
        encoding="utf-8"
    )


def test_archive_content_binding_accepts_only_the_same_archive(tmp_path):
    """持久化基线与冻结归档逐文件核对：字节相同通过；JSON 仅排版不同通过；内容不同或缺失不通过。"""
    archive, export = tmp_path / "archive", tmp_path / "export"
    for folder in (archive, export):
        (folder / "state/sfc").mkdir(parents=True)
        (folder / "state/catalogue").mkdir(parents=True)
    (archive / "state/teams.jsonl").write_text('[1, "a"]\n', encoding="utf-8")
    (export / "state/teams.jsonl").write_text('[1, "a"]\n', encoding="utf-8")
    (archive / "state/sfc/26158.json").write_text('{\n  "issue": 1,\n  "rows": [1, 2]\n}\n')
    (export / "state/sfc/26158.json").write_text('{"issue": 1, "rows": [1, 2]}')

    def files(folder):
        return {
            p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in folder.rglob("*")
            if p.is_file()
        }

    def bind():
        manifest = archive / "feed_manifest.json"
        manifest.unlink(missing_ok=True)
        manifest.write_text(json.dumps(dict(files=files(archive))), encoding="utf-8")
        facts = dict(
            archive_root=str(archive),
            manifest=dict(
                path="feed_manifest.json",
                sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
            ),
        )
        gate._archive_content_binding(
            facts, dict(folder=export, manifest=dict(files=files(export)))
        )

    # 原始抓取文件不属于导出状态，只在归档一侧存在也不影响核对。
    (archive / "state/catalogue/raw.json").write_text("{}", encoding="utf-8")
    bind()
    (export / "state/sfc/26158.json").write_text('{"issue": 2, "rows": [1, 2]}')
    with pytest.raises(ValueError, match="different frozen archive"):
        bind()
    (export / "state/sfc/26158.json").write_text('{"issue": 1, "rows": [1, 2]}')
    (export / "state/teams.jsonl").write_text('[1,  "a"]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="different frozen archive"):
        bind()
    (export / "state/teams.jsonl").unlink()
    with pytest.raises(ValueError, match="different frozen archive"):
        bind()


def test_freshness_is_judged_per_fixture_not_per_capture():
    """真实归档里同一场比赛每次抓取各有一行：只要有一次完整且在窗口结束之后的抓取就算新鲜；
    只有旧抓取、或新抓取不完整，都不算。"""
    from datetime import UTC, datetime

    end = datetime(2026, 11, 15, 16, tzinfo=UTC)
    old = [dict(fetched_at_utc="2026-11-14T06:00:00Z")]
    new = [dict(fetched_at_utc="2026-11-15T21:57:00Z")]
    rows = [
        dict(match_id="apif:1", complete=True, provenance=old),
        dict(match_id="apif:1", complete=True, provenance=new),
        dict(match_id="apif:2", complete=True, provenance=old),
        dict(match_id="apif:3", complete=False, provenance=new),
        dict(match_id="apif:3", complete=True, provenance=old),
    ]
    assert gate._fresh_fixtures(rows, end) == {"apif:1": True, "apif:2": False, "apif:3": False}
