"""Exercise publication and verified reads of independent T70 archives."""

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import responses

from test_t70_targets import NOW as TARGET_NOW
from test_t70_targets import web_target_archive as web_target_archive
from zucai import web_research, web_research_digest
from zucai import web_research_archive as archive_module

FIXTURES = Path(__file__).resolve().parents[1] / "data/fixtures/t70"


def write(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def case(tmp_path):
    forecasts = {}
    files = {}
    records = []
    artifacts = {}
    probability = {"h": 0.6, "d": 0.2, "a": 0.2}
    for i, day in enumerate(("2026-11-27", "2026-11-28"), 1):
        match_id, jc_num = f"jc:{i}", f"周五00{i}"
        kickoff = f"{day}T16:00:00+00:00"
        identity = dict(
            match_id=match_id,
            jc_num=jc_num,
            league="测试联赛",
            home=f"主队{i}",
            away=f"客队{i}",
            kickoff_utc=kickoff,
        )
        source = f"forecasts/{day}/forecast-run/forecast.json"
        generated = "2026-11-27T10:00:00+00:00"
        published = "2026-11-27T10:05:00+00:00"
        prices = {"h": 2.0, "d": 4.0, "a": 5.0}
        price_total = sum(1 / price for price in prices.values())
        market_probabilities = {key: 1 / price / price_total for key, price in prices.items()}
        sha = write(
            tmp_path / source,
            dict(
                batch_id=f"batch-{i}",
                mode="prematch",
                generated_utc=generated,
                as_of_utc=generated,
                predictions=[
                    {
                        **identity,
                        "source": "jc",
                        "status": "predicted",
                        "model_probabilities": probability,
                        "market": {"prices": prices, "snapshot_utc": "2026-11-27T09:45:00+00:00"},
                    }
                ],
            ),
        )
        forecasts[day] = dict(
            path=source.removesuffix("/forecast.json"),
            batch_id=f"batch-{i}",
            generated_utc=generated,
            publication_required=True,
            published_utc=published,
            publication_evidence=dict(
                method="post_push_confirmation",
                commit="0" * 40,
                ref="refs/remotes/origin/data/football-feeds",
                candidate_path=source,
                candidate_sha256=sha,
                published_utc=published,
            ),
        )
        files[source] = sha
        records.append(
            dict(
                match_id=match_id,
                forecast_run_id="forecast-run",
                forecast_path=source,
                forecast_sha256=sha,
                forecast_published_utc=published,
                kickoff_utc=kickoff,
                completed_utc="2026-11-27T10:13:00+00:00",
                cutoff_utc="2026-11-27T10:10:00+00:00",
                identity=identity,
                status="ok",
                structured={"judgment": "h", "probabilities": probability},
                checks={},
                model_probabilities=probability,
                market_probabilities=market_probabilities,
            )
        )
        artifacts[match_id] = dict(
            report_text="中文原件\r\n保留空格  \n",
            research_response={"choices": [{"message": {"content": "中文原件\r\n保留空格  \n"}}]},
            structure_response={},
            material={"identity": identity},
        )

    write(
        tmp_path / "forecast_manifest.json",
        dict(
            version=1,
            algorithm="sha256",
            runs={"forecast-run": {"forecasts": forecasts}},
            files=files,
        ),
    )
    write(tmp_path / "feed_manifest.json", {"unrelated": "preserve"})
    run = dict(
        run_id="research-run",
        status="complete",
        started_utc="2026-11-27T10:10:00+00:00",
        generated_utc="2026-11-27T10:15:00+00:00",
        prompt_version="v1",
        prompt_sha256="1" * 64,
        provider="deepseek",
        model="deepseek-flash",
        records=records,
    )
    return tmp_path, run, artifacts


def clock(stamp="2026-11-27T10:16:00+00:00"):
    return lambda: datetime.fromisoformat(stamp)


def files_under(path):
    return {file.relative_to(path): file.read_bytes() for file in path.rglob("*") if file.is_file()}


def test_publishes_independent_bytes_and_filters_by_actual_freeze(case):
    root, run, artifacts = case
    prior = files_under(root)
    assert not archive_module.has_runs(root)
    assert archive_module.read_run(root, "research-run") is None
    manifest = archive_module.publish(root, run, artifacts, clock=clock())

    assert archive_module.has_runs(root)
    assert archive_module.read_run(root, "research-run") == manifest
    assert archive_module.load(root, "2026-11-27T10:15:59+00:00") == []
    rows = archive_module.load(root, "2026-11-27T10:16:00+00:00")
    assert [row["match_id"] for row in rows] == ["jc:1", "jc:2"]
    for row in rows:
        expected = artifacts[row["match_id"]]
        archive_path = root / row["archive_path"]
        assert archive_path.parent.parent.name == "web_research"
        assert archive_path.name == "web_research.json"
        assert archive_path.parent.parent.parent == (root / row["forecast_path"]).parent
        assert row["archive_sha256"] == hashlib.sha256(archive_path.read_bytes()).hexdigest()
        assert (root / row["report_path"]).read_bytes() == expected["report_text"].encode("utf-8")
        assert json.loads((root / row["response_path"]).read_bytes()) == expected
    after = files_under(root)
    assert {name: after[name] for name in prior} == prior


def test_different_run_preserves_earlier_version_and_same_run_refuses_overwrite(case):
    root, run, artifacts = case
    archive_module.publish(root, run, artifacts, clock=clock())
    before = files_under(root)
    revised = deepcopy(artifacts)
    revised["jc:1"]["report_text"] += "新增"

    assert archive_module.publish(root, run, artifacts, clock=clock()) == archive_module.read_run(
        root, "research-run"
    )
    with pytest.raises(ValueError):
        archive_module.publish(root, run, revised, clock=clock())
    assert files_under(root) == before

    archive_module.publish(
        root, {**run, "run_id": "second-run"}, revised, clock=clock("2026-11-27T10:17:00+00:00")
    )
    rows = archive_module.load(root, "2026-11-27T10:17:00+00:00")
    assert [(row["run_id"], row["match_id"]) for row in rows] == [
        ("research-run", "jc:1"),
        ("research-run", "jc:2"),
        ("second-run", "jc:1"),
        ("second-run", "jc:2"),
    ]
    after = files_under(root)
    assert {name: after[name] for name in before} == before


@pytest.mark.parametrize("missed", ["completed_utc", "frozen_clock"])
def test_cannot_freeze_any_record_at_or_after_kickoff(case, missed):
    root, run, artifacts = case
    before = files_under(root)
    freeze_clock = clock()
    when = run["records"][0]["kickoff_utc"]
    if missed == "completed_utc":
        run["records"][0]["completed_utc"] = when
        run["generated_utc"] = when
    freeze_clock = clock(when)
    with pytest.raises(ValueError):
        archive_module.publish(root, run, artifacts, clock=freeze_clock)
    assert files_under(root) == before
    assert not archive_module.has_runs(root)


@pytest.mark.parametrize(
    "field",
    ["forecast_sha256", "forecast_published_utc", "model_probabilities", "market_probabilities"],
)
def test_rejects_broken_forecast_lineage(case, field):
    root, run, artifacts = case
    before = files_under(root)
    run["records"][0][field] = {
        "forecast_sha256": "1" * 64,
        "forecast_published_utc": "2026-11-27T10:04:00+00:00",
        "model_probabilities": {"h": 0.4, "d": 0.2, "a": 0.4},
        "market_probabilities": None,
    }[field]
    with pytest.raises(ValueError):
        archive_module.publish(root, run, artifacts, clock=clock())
    assert files_under(root) == before


def test_failed_marker_publication_leaves_run_retryable(case, monkeypatch):
    root, run, artifacts = case
    before = files_under(root)

    def fail(*args, **kwargs):
        raise OSError("synthetic local publication error")

    with monkeypatch.context() as context:
        context.setattr(type(root), "replace", fail)
        with pytest.raises(OSError, match="synthetic local publication error"):
            archive_module.publish(root, run, artifacts, clock=clock())

    assert not archive_module.has_runs(root)
    assert files_under(root) == before
    assert archive_module.read_run(root, run["run_id"]) is None
    archive_module.publish(root, run, artifacts, clock=clock())
    assert archive_module.has_runs(root)


def test_rejects_changed_artifact_but_keeps_older_cutoff_readable(case):
    root, run, artifacts = case
    archive_module.publish(root, run, artifacts, clock=clock())
    rows = archive_module.load(root, "2026-11-27T10:16:00+00:00")
    (root / rows[0]["response_path"]).write_text("{}", encoding="utf-8")

    assert archive_module.load(root, "2026-11-27T10:15:00+00:00") == []
    with pytest.raises(ValueError, match="SHA256"):
        archive_module.load(root, "2026-11-27T10:16:00+00:00")


def test_empty_run_preserves_partial_reason(case):
    root, run, _ = case
    run.update(status="partial", records=[], stop_reason="cost_limit")
    manifest = archive_module.publish(root, run, {}, clock=clock())
    assert manifest["status"] == "partial"
    assert manifest["stop_reason"] == "cost_limit"
    assert manifest["files"] == {}
    assert archive_module.load(root, "2026-11-27T10:16:00+00:00") == []


@pytest.mark.parametrize("raw_judgment, expected", [("主胜", "ok"), ("无可靠倾向", "unparsed")])
@responses.activate
def test_verified_sale_through_two_mock_requests_archive_and_daily_section(
    web_target_archive, monkeypatch, raw_judgment, expected
):
    archive = web_target_archive
    before = files_under(archive)
    research = json.loads((FIXTURES / "provider-research.synthetic.json").read_bytes())
    structure = json.loads((FIXTURES / "provider-structure.synthetic.json").read_bytes())
    quoted_at = (TARGET_NOW - timedelta(minutes=20)).isoformat()
    evidence_url = "https://example.test/preview"
    research["choices"][0]["message"]["content"] = (
        f"数据截止时间：{quoted_at}\n"
        "比赛身份核对：一致。测试联赛，2026-11-03，合成主队 VS 合成客队。\n"
        f"主要依据：合成主队在发布会上提到轮换。{evidence_url}\n"
        f"研究判断：{raw_judgment}\n"
    ) + (
        "研究判断的概率：主胜 60%、平 25%、客胜 15%\n"
        if expected == "ok"
        else "研究判断的概率：不给\n"
    )
    structure["choices"][0]["message"]["content"] = json.dumps(
        {
            "identity": {
                "match_id": "jc:2",
                "jc_num": "周二002",
                "league": "测试联赛",
                "home": "合成主队",
                "away": "合成客队",
                "date_beijing": "2026-11-03",
            },
            "cutoff_utc": quoted_at,
            "judgment": "h",
            "probabilities": {"h": 0.6, "d": 0.25, "a": 0.15},
            "market": {"outcome": None, "probability": None, "odds": None},
            "basis_type": "伤停首发",
            "injuries_lineups": {"home": [], "away": []},
            "basis": [
                {
                    "text": "合成主队在发布会上提到轮换",
                    "urls": [evidence_url],
                    "published_utc": quoted_at,
                }
            ],
            "counterevidence": [],
            "recent": [],
            "citations": [evidence_url],
        },
        ensure_ascii=False,
    )
    for payload in (research, structure):
        responses.post("https://api.deepseek.com/chat/completions", json=payload)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-synthetic-key")
    actual = web_research.run(
        archive, "offline-run", max_cost="1", max_matches=1, clock=lambda: TARGET_NOW
    )
    assert actual["status"] == "partial" and actual["stop_reason"] == "match_limit"
    assert actual["provider_requests"] == len(responses.calls) == 2

    (row,) = archive_module.load(archive, TARGET_NOW)
    assert row["match_id"] == "jc:2" and row["status"] == expected
    raw = json.loads((archive / row["response_path"]).read_bytes())
    assert (raw["research_response"], raw["structure_response"]) == (research, structure)
    lines, hashes = web_research_digest.section(archive, TARGET_NOW)
    if expected == "ok":
        assert row["structured"]["basis"][0]["urls"] == [evidence_url]
        assert row["checks"]["warning_count"] == 0
        assert any(
            "周二002 合成主队 VS 合成客队" in line and "主胜 60.00%" in line for line in lines
        )
    else:
        assert row["structured"] is None and row["checks"]["reason"] == "raw_report_conflict"
        assert any("未生成（结构未通过校验）" in line for line in lines)
        assert not any("主胜 60.00%" in line for line in lines)
    assert hashes == {row["archive_path"]: row["archive_sha256"]}
    assert {name: (archive / name).read_bytes() for name in before} == before
