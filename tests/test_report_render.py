import csv
import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest


@pytest.mark.parametrize("same_database", [False, True])
def test_model_table_shows_both_database_hashes_and_marks_mismatch(
    frozen_batch, tmp_path, same_database
):
    from zucai.report.render import write_report

    training, evaluation = "b" * 64, "c" * 64
    if same_database:
        evaluation = training
    frozen_batch["models"][0].update(
        training_database_sha256=training, evaluation_database_sha256=evaluation
    )
    write_report(frozen_batch, tmp_path / "dual-hashes")
    report = (tmp_path / "dual-hashes/report.md").read_text()
    model_table = report.split("## 模型与训练范围")[1].split("## 纯模型与市场对照")[0]
    assert "训练库 SHA256" in model_table and "评估库 SHA256" in model_table
    assert training in model_table and evaluation in model_table
    assert ("评估库 ≠ 训练库" in model_table) is not same_database


@pytest.mark.parametrize("sample", ["live-batch", "live-batch-v2", "public-replay"])
def test_checked_in_frozen_samples_have_matching_hashes(sample):
    from zucai.report.render import load_batch

    root = Path(__file__).parents[1] / "reports/2026-11-01-t06-acceptance"
    batch = load_batch(root / sample / "forecast.json")
    assert batch["probability_type"] == "P2" and batch["bet_allowed"] is False


@pytest.fixture
def frozen_batch():
    """A hand-calculated score grid: h=.6, d=.25, a=.15."""
    version = "a" * 64
    return {
        "schema_version": 1,
        "batch_id": "batch-001",
        "generated_utc": "2026-11-01T02:00:00Z",
        "as_of_utc": "2026-11-01T01:00:00Z",
        "mode": "historical_replay",
        "probability_type": "P2",
        "validation_status": "experimental_unadopted",
        "bet_allowed": False,
        "input": {"match_ids": ["jc:1", "jc:missing"]},
        "models": [
            {
                "model_name": "dixon-coles",
                "model_version": version,
                "source": "jc",
                "league": "测试联赛",
                "model_as_of_utc": "2026-10-31T00:00:00Z",
                "training_games": 100,
                "last_available_utc": "2026-10-30T22:00:00Z",
                "availability_basis": "mixed",
                "assumed_training_share": 0.125,
            }
        ],
        "predictions": [
            {
                "match_id": "jc:1",
                "source": "jc",
                "league": "测试联赛",
                "jc_num": "周日001",
                "home": "=SUM(1,1)|*<队>",
                "away": "客队[乙]\n换行",
                "kickoff_utc": "2026-11-01T12:00:00Z",
                "kickoff_beijing": "2026-11-01T20:00:00+08:00",
                "status": "predicted",
                "reason": None,
                "model_name": "dixon-coles",
                "model_version": version,
                "model_probabilities": {"h": 0.6, "d": 0.25, "a": 0.15},
                "score_matrix": [[0.1, 0.1, 0.05], [0.3, 0.1, 0], [0.2, 0.1, 0.05]],
                "top_scores": [
                    {"score": "1:0", "probability": 0.3},
                    {"score": "2:0", "probability": 0.2},
                    {"score": "0:0", "probability": 0.1},
                    {"score": "0:1", "probability": 0.1},
                    {"score": "1:1", "probability": 0.1},
                    {"score": "9:9", "probability": 0.00001},
                ],
                "market": {
                    "prices": {"h": 2.0, "d": 3.0, "a": 6.0},
                    "probabilities": {"h": 0.5, "d": 1 / 3, "a": 1 / 6},
                    "snapshot_utc": "2026-11-01T00:30:00Z",
                },
                "ev": {"h": 0.19999999999999996, "d": -0.25, "a": -0.10000000000000009},
                "metadata": {"excluded_score_mass": 0.0001},
                "experimental_blend": {
                    "model_name": "dixon-coles+market",
                    "probabilities": {
                        "h": 0.575,
                        "d": 0.2708333333333333,
                        "a": 0.15416666666666667,
                    },
                    "market_weight": 0.25,
                    "market_dependent": True,
                },
            },
            {
                "match_id": "jc:missing",
                "status": "excluded",
                "reason": "未知比赛身份|请核对",
                "home": None,
                "away": None,
                "model_probabilities": None,
                "score_matrix": None,
                "top_scores": [],
                "market": None,
                "ev": None,
                "experimental_blend": None,
            },
        ],
    }


def test_report_roundtrip_preserves_payload_and_hashes_all_exports(frozen_batch, tmp_path):
    from zucai.report.render import load_batch, write_report

    output = tmp_path / "new"
    path = write_report(frozen_batch, output)
    assert path == output / "forecast.json"
    assert {p.name for p in output.iterdir()} == {
        "forecast.json",
        "report.md",
        "report.csv",
        "manifest.json",
    }
    assert json.loads(path.read_text(encoding="utf-8")) == frozen_batch
    assert load_batch(path) == frozen_batch
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["files"]) == {"forecast.json", "report.md", "report.csv"}
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == digest


def test_readable_report_discloses_replay_model_market_and_point_ev(frozen_batch, tmp_path):
    from zucai.report.render import write_report

    write_report(frozen_batch, tmp_path / "new")
    report = (tmp_path / "new/report.md").read_text(encoding="utf-8")
    for text in (
        "P2",
        "实验",
        "非购买建议",
        "历史回放",
        "非当时冻结",
        "BET 禁用",
        "点估计",
        "2026-11-01 10:00:00",
        "2026-11-01 09:00:00",
        "北京时间",
        "模型主胜",
        "市场主胜",
        "SP 主胜",
        "EV 主胜",
        "100",
        "12.50%",
        "mixed",
        "jc",
        "测试联赛",
        "a" * 64,
        "1:0",
        "30.00%",
        "jc:missing",
        "未知比赛身份",
    ):
        assert text in report
    assert "**20.00%**" in report
    assert "9:9" not in report
    assert r"\|\*&lt;队&gt;" in report
    assert r"客队\[乙\]" in report
    assert "实验混合" in report and "57.50%" in report
    assert "60.00%" in report  # The primary result stays the pure model.


def test_csv_is_precise_numeric_and_protects_formula_text(frozen_batch, tmp_path):
    from zucai.report.render import write_report

    write_report(frozen_batch, tmp_path / "new")
    with (tmp_path / "new/report.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 2
    assert rows[0]["home"] == "'=SUM(1,1)|*<队>"
    assert rows[0]["model_h"] == "0.6"
    assert rows[0]["market_d"] == "0.3333333333333333"
    assert rows[0]["ev_a"] == "-0.10000000000000009"
    assert rows[0]["experimental_blend_h"] == "0.575"
    assert rows[1]["model_h"] == ""
    assert rows[1]["reason"] == "未知比赛身份|请核对"


def test_missing_market_remains_blank_in_csv(frozen_batch, tmp_path):
    from zucai.report.render import write_report

    prediction = frozen_batch["predictions"][0]
    prediction.update(market=None, ev=None, experimental_blend=None)
    write_report(frozen_batch, tmp_path / "new")
    with (tmp_path / "new/report.csv").open(encoding="utf-8-sig", newline="") as stream:
        row = next(csv.DictReader(stream))
    assert row["model_h"] == "0.6"
    assert row["market_h"] == row["sp_h"] == row["ev_h"] == ""


def test_report_never_overwrites_existing_directory(frozen_batch, tmp_path):
    from zucai.report.render import write_report

    output = tmp_path / "existing"
    output.mkdir()
    sentinel = output / "forecast.json"
    sentinel.write_text("original", encoding="utf-8")
    with pytest.raises(FileExistsError):
        write_report(frozen_batch, output)
    assert sentinel.read_text(encoding="utf-8") == "original"


@pytest.mark.parametrize("filename", ["forecast.json", "report.md", "report.csv"])
def test_loading_rejects_tampered_files(frozen_batch, tmp_path, filename):
    from zucai.report.render import load_batch, write_report

    path = write_report(frozen_batch, tmp_path / "new")
    with (path.parent / filename).open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(ValueError, match="SHA256|校验|hash"):
        load_batch(path)


def test_loader_rejects_missing_forecast_hash_and_external_paths(frozen_batch, tmp_path):
    from zucai.report.render import load_batch, write_report

    path = write_report(frozen_batch, tmp_path / "new")
    manifest_path = path.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["files"]["forecast.json"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        load_batch(path)
    manifest["files"]["forecast.json"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest["files"]["../outside"] = "a" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        load_batch(path)


@pytest.mark.parametrize("mutation", ["schema", "predictions", "unsafe", "probabilities", "time"])
def test_writer_rejects_invalid_payload_without_creating_output(frozen_batch, tmp_path, mutation):
    from zucai.report.render import write_report

    bad = deepcopy(frozen_batch)
    if mutation == "schema":
        bad["schema_version"] = 2
    elif mutation == "predictions":
        bad["predictions"] = "not rows"
    elif mutation == "unsafe":
        bad["bet_allowed"] = True
    elif mutation == "time":
        bad["generated_utc"] = "2026-11-01T10:00:00"
    else:
        bad["predictions"][0]["model_probabilities"]["h"] = float("nan")
    with pytest.raises(ValueError):
        write_report(bad, tmp_path / "new")
    assert not (tmp_path / "new").exists()


def test_loader_validates_payload_after_hash_verification(frozen_batch, tmp_path):
    from zucai.report.render import load_batch, write_report

    path = write_report(frozen_batch, tmp_path / "new")
    frozen_batch["schema_version"] = 2
    path.write_text(json.dumps(frozen_batch), encoding="utf-8")
    manifest_path = path.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["forecast.json"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version"):
        load_batch(path)


def test_failed_write_removes_only_files_created_by_this_call(frozen_batch, tmp_path, monkeypatch):
    from zucai.report.render import write_report

    output = tmp_path / "new"
    original_open = Path.open

    def fail_on_markdown(path, *args, **kwargs):
        if path == output / "report.md":
            (output / "unrelated.txt").write_text("keep", encoding="utf-8")
            raise OSError("disk failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_on_markdown)
    with pytest.raises(OSError, match="disk failure"):
        write_report(frozen_batch, output)
    assert {p.name for p in output.iterdir()} == {"unrelated.txt"}


def test_failed_write_removes_empty_new_directory(frozen_batch, tmp_path, monkeypatch):
    from zucai.report.render import write_report

    output = tmp_path / "new"
    original_open = Path.open

    def fail_on_manifest(path, *args, **kwargs):
        if path == output / "manifest.json":
            raise OSError("disk failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_on_manifest)
    with pytest.raises(OSError):
        write_report(frozen_batch, output)
    assert not output.exists()


def test_excluded_match_cannot_export_a_fabricated_matrix(frozen_batch, tmp_path):
    from zucai.report.render import write_report

    frozen_batch["predictions"][1]["score_matrix"] = [[1.0]]
    with pytest.raises(ValueError, match="排除"):
        write_report(frozen_batch, tmp_path / "new")
    assert not (tmp_path / "new").exists()


def test_failed_exclusive_open_does_not_remove_someone_elses_fixed_file(
    frozen_batch, tmp_path, monkeypatch
):
    from zucai.report.render import write_report

    output = tmp_path / "new"
    original_open = Path.open

    def concurrent_file(path, *args, **kwargs):
        if path == output / "report.md" and args == ("xb",):
            with original_open(path, "w", encoding="utf-8") as stream:
                stream.write("keep other writer's file")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", concurrent_file)
    with pytest.raises(FileExistsError):
        write_report(frozen_batch, output)
    assert {p.name for p in output.iterdir()} == {"report.md"}
    assert (output / "report.md").read_text(encoding="utf-8") == "keep other writer's file"


def test_ev_threshold_is_strictly_greater_than_five_percent(frozen_batch, tmp_path):
    from zucai.report.render import write_report

    frozen_batch["predictions"][0]["ev"] = {"h": 0.05, "d": 0.050000001, "a": -0.5}
    write_report(frozen_batch, tmp_path / "new")
    report = (tmp_path / "new/report.md").read_text(encoding="utf-8")
    assert "| 5.00% | **5.00%** | -50.00% |" in report
