"""T68 第八节第 10 项：生成的开启证据必须能被真实入口（build_register）核验。"""

import json
from pathlib import Path

from test_t68_daily_gate_entry import CUTOFF, bundle  # noqa: F401
from test_t68_identity_archive_entry import archive as frozen_archive  # noqa: F401
from test_t68_identity_archive_entry import seal
from zucai import identity_daily_gate as gate
from zucai.identity_gate_evidence_gen import generate


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
