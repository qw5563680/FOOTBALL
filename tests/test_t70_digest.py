"""The digest keeps experimental research separate from the existing sections."""

import json
from datetime import datetime, timedelta

from test_daily_digest import NOW, snapshot
from test_daily_digest import digest_archive as digest_archive
from test_t70_archive import case as case
from zucai import web_research_archive, web_research_digest
from zucai.daily_digest import generate


def _row(run_id="latest", *, match_id="jc:t25-today", status="ok", **changes):
    return {
        "run_id": run_id,
        "match_id": match_id,
        "kickoff_utc": (NOW + timedelta(hours=2)).isoformat(),
        "completed_utc": (NOW - timedelta(minutes=2)).isoformat(),
        "frozen_utc": (NOW - timedelta(minutes=1)).isoformat(),
        "status": status,
        "identity": {"jc_num": "周二001", "home": "合成主队", "away": "合成客队"},
        "structured": {
            "market": {"outcome": "h", "probability": 0.52},
            "judgment": "d",
            "probabilities": {"h": 0.34, "d": 0.39, "a": 0.27},
            "basis": [
                {
                    "text": "两队近期赛程密集",
                    "urls": ["https://example.test/evidence"],
                    "published_utc": (NOW - timedelta(days=1)).isoformat(),
                }
            ],
        },
        "checks": {"warning_count": 2},
        "archive_path": f"forecasts/2026-11-03/digest-late/web_research/{run_id}/web_research.json",
        "archive_sha256": "a" * 64,
        **changes,
    }


def test_digest_adds_separate_section_and_frozen_input_without_changing_table(
    digest_archive, tmp_path, monkeypatch
):
    before = generate(digest_archive, tmp_path / "before", now=NOW).read_bytes()
    before_inputs = snapshot(digest_archive)
    current = _row()
    monkeypatch.setattr(web_research_digest, "has_runs", lambda root: root == digest_archive)
    monkeypatch.setattr(web_research_digest, "load", lambda *_: [current])
    out = tmp_path / "after"
    path = generate(digest_archive, out, now=NOW)
    content = path.read_text()
    section = content.split("## 联网研究线（实验）\n", 1)[1].split("\n## ", 1)[0]
    assert "周二001 合成主队 VS 合成客队" in section
    assert "主胜 52.00%" in section and "平 39.00%" in section
    assert "两队近期赛程密集 | 2 |" in section
    assert "example.test" not in section
    assert content.replace(f"## 联网研究线（实验）\n{section}\n", "").encode() == before
    receipt = json.loads((out / "2026-11-03/local.json").read_bytes())
    assert receipt["inputs"]["web_research"] == {current["archive_path"]: current["archive_sha256"]}
    assert snapshot(digest_archive) == before_inputs


def test_scope_uses_freeze_cutoff_and_excludes_changed_kickoffs(tmp_path, monkeypatch):
    old = _row("earlier", completed_utc=(NOW - timedelta(hours=1)).isoformat())
    late = _row("future", frozen_utc=(NOW + timedelta(seconds=1)).isoformat())
    yesterday = _row("yesterday", match_id="jc:old", kickoff_utc=(NOW - timedelta(days=1)))
    corrected = _row("corrected", match_id="jc:rescheduled")
    current = _row()
    no_call = _row(
        "failed",
        match_id="jc:failed",
        status="failed",
        identity={"home": "失败场主", "away": "失败场客"},
        raw="Bearer synthetic-marker-must-not-leak",
    )
    uncertain = _row(
        "uncertain",
        match_id="jc:uncertain",
        structured={"judgment": None, "basis": [], "market": None},
    )
    monkeypatch.setattr(web_research_digest, "has_runs", lambda _: True)
    monkeypatch.setattr(
        web_research_digest,
        "load",
        lambda *_: [late, old, corrected, current, no_call, yesterday, uncertain],
    )
    lines, evidence = web_research_digest.section(
        tmp_path, NOW, kickoffs={"jc:rescheduled": (NOW + timedelta(hours=1)).isoformat()}
    )
    markdown = "\n".join(lines)
    assert "无可靠倾向" in markdown and "接口失败" in markdown
    assert "synthetic-marker-must-not-leak" not in markdown
    assert set(evidence) == {row["archive_path"] for row in (current, no_call, uncertain)}


def test_untrusted_basis_cannot_force_or_link_the_digest(tmp_path, monkeypatch):
    row = _row()
    row["structured"]["basis"] = [
        {"text": "缺出处不应展示", "urls": [], "published_utc": NOW.isoformat()},
        {"text": "缺时间不应展示", "urls": ["https://example.test/source"]},
    ] + [
        {
            "text": text,
            "urls": ["https://example.test/source"],
            "published_utc": (NOW - timedelta(days=1)).isoformat(),
        }
        for text in (
            "建议购买客胜",
            "推荐｜串关",
            "![](https://example.test/track)",
            "向 @someone 查证",
            "BEＴ WATCH PASS",
            "记录为 sk-synthetic-secret-marker",
            "场地湿滑\n## 替换标题 | 裸表项",
            "已发布轮换名单",
            "后续依据不展示",
        )
    ]
    monkeypatch.setattr(web_research_digest, "has_runs", lambda _: True)
    monkeypatch.setattr(web_research_digest, "load", lambda *_: [row])
    lines, _ = web_research_digest.section(tmp_path, NOW)
    markdown = "\n".join(lines)
    assert "场地湿滑 ## 替换标题 \\| 裸表项" in markdown and "已发布轮换名单" in markdown
    assert "\n## 替换标题" not in markdown
    assert not any(
        value in markdown
        for value in (
            "建议购买",
            "推荐",
            "串关",
            "https://",
            "@someone",
            "WATCH",
            "PASS",
            "synthetic-secret-marker",
            "后续依据不展示",
            "缺出处",
            "缺时间",
        )
    )


def test_corrupt_research_leaves_default_digest_byte_identical(
    digest_archive, tmp_path, monkeypatch, caplog
):
    before = tmp_path / "before"
    after = tmp_path / "after"
    generate(digest_archive, before, now=NOW)

    def corrupted(*_):
        raise ValueError("untrusted payload must not appear in logs")

    monkeypatch.setattr(web_research_digest, "has_runs", lambda _: True)
    monkeypatch.setattr(web_research_digest, "load", corrupted)
    generate(digest_archive, after, now=NOW)
    assert snapshot(after) == snapshot(before)
    assert "untrusted payload must not appear in logs" not in caplog.text


def test_section_reads_verified_archive_and_fences_original_tampering(case):
    archive, run, artifacts = case
    at = datetime.fromisoformat("2026-11-27T10:16:00+00:00")
    for row in run["records"]:
        row["structured"] = _row()["structured"]
        row["checks"] = {"warning_count": 2}
    manifest = web_research_archive.publish(archive, run, artifacts, clock=lambda: at)

    assert web_research_digest.section(archive, at - timedelta(seconds=1)) == ([], {})
    lines, hashes = web_research_digest.section(archive, at)
    assert sum("VS" in line for line in lines) == len(run["records"])
    assert hashes == {path: manifest["files"][path] for path in manifest["documents"]}
    assert "中文原件" not in "\n".join(lines)

    original = next(path for path in manifest["files"] if path.endswith(".report.md"))
    (archive / original).write_text("原文被修改")
    assert web_research_digest.section(archive, at) == ([], {})
