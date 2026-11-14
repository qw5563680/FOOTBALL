"""Immutable source-expanded candidates, separate from primary and three experiment lines.

Only the seven leagues retaining the previous source policy can have this sidecar.
It never updates the primary model index or supplies paper-account predictions.
"""

import copy
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from zucai.forecast_archive import MANIFEST, _path, _read, verify_forecasts
from zucai.predict.model_definitions import validate_definition_metadata
from zucai.report.render import _timestamp, _validate_batch, load_batch
from zucai.sources.base import _atomic_write

LEGACY_LEAGUES = frozenset({"英超", "西甲", "意甲", "德甲", "法甲", "葡超", "巴甲"})
EXPANDED_SOURCES = frozenset({"fd", "jc", "fdorg", "apif"})
IDENTITY_FIELDS = ("match_id", "source", "league", "home", "away", "kickoff_utc")


def _raw(value):
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")


def _experimental(value, *, definition=False):
    if (
        not isinstance(value, dict)
        or value.get("source_variant") != "expanded"
        or value.get("p2_ready") is not False
        or value.get("paper_eligible") is not False
    ):
        raise ValueError("来源候选必须显式标记 expanded、实验且不可进入纸面账户")
    if definition:
        validate_definition_metadata(value)
    if "p2_label" in value and value["p2_label"] != "实验":
        raise ValueError("来源候选标签必须为实验")


def _sources(value):
    sources = value.get("sources")
    if (
        not isinstance(sources, list)
        or any(not isinstance(source, str) for source in sources)
        or len(sources) != len(set(sources))
        or set(sources) != EXPANDED_SOURCES
    ):
        raise ValueError("来源候选必须显式使用 fd/jc/fdorg/apif")
    return set(sources)


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _frozen_status(outcomes):
    failed = sorted(league for league, result in outcomes.items() if result["status"] == "failed")
    return {
        "status": "failed" if len(failed) == len(outcomes) else "partial" if failed else "complete",
        "stage": "training" if failed else "complete",
        "reason": "candidate_training_failed" if failed else None,
        "failed_leagues": failed,
        "artifacts_frozen": True,
        "error_type": None,
    }


def _validate_status(run):
    status = run.get("source_version_status")
    if status is None:  # Older archives predate the explicit attempt status.
        return
    if not isinstance(status, dict) or set(status) != {
        "status",
        "stage",
        "reason",
        "failed_leagues",
        "artifacts_frozen",
        "error_type",
        "recorded_utc",
    }:
        raise ValueError("来源候选状态结构无效")
    if _timestamp(status["recorded_utc"]) < _timestamp(run["generated_utc"]):
        raise ValueError("来源候选状态不得早于主运行冻结")
    if "source_versions" in run:
        expected = _frozen_status(run["source_versions"]["leagues"])
    else:
        stage = status["stage"]
        if stage not in {"training", "prediction", "freeze"} or status["error_type"] not in {
            "ValueError",
            "OSError",
            "RuntimeError",
            "Exception",
        }:
            raise ValueError("来源候选失败阶段或异常类别无效")
        expected = dict(
            status="failed",
            stage=stage,
            reason=f"candidate_{stage}_failed",
            failed_leagues=sorted(set(run["leagues"]) & LEGACY_LEAGUES),
            artifacts_frozen=False,
            error_type=status["error_type"],
        )
        if not expected["failed_leagues"]:
            raise ValueError("来源候选失败状态缺少适用联赛")
    if (
        any(status[key] != value for key, value in expected.items())
        or type(status["artifacts_frozen"]) is not bool
    ):
        raise ValueError("来源候选状态与实际冻结产物不符")


def record_source_version_failure(archive, run_id, stage, error, *, clock=None):
    """Record a terminal attempt without creating artifacts or altering primary bytes.

    Only fixed reason codes and exception categories are public; exception text
    may contain credentials or private paths and stays out of the manifest.
    """
    archive = Path(archive)
    manifest = verify_forecasts(archive)
    if manifest is None or run_id not in manifest["runs"]:
        raise ValueError("来源候选失败只能记录到已经冻结的主运行")
    before = (archive / MANIFEST).read_bytes()
    run = manifest["runs"][run_id]
    if "source_versions" in run or "source_version_status" in run:
        raise ValueError("来源候选状态已冻结，不允许覆盖")
    error_type = next(
        (kind.__name__ for kind in (ValueError, OSError, RuntimeError) if isinstance(error, kind)),
        "Exception",
    )
    instant = clock() if clock else datetime.now(UTC)
    run["source_version_status"] = {
        "status": "failed",
        "stage": stage,
        "reason": f"candidate_{stage}_failed",
        "failed_leagues": sorted(set(run["leagues"]) & LEGACY_LEAGUES),
        "artifacts_frozen": False,
        "error_type": error_type,
        "recorded_utc": instant.isoformat(),
    }
    _validate_status(run)
    if (archive / MANIFEST).read_bytes() != before:
        raise ValueError("主清单在来源候选状态记录期间发生变化")
    _atomic_write(archive / MANIFEST, _raw(manifest))
    try:
        verify_forecasts(archive)
    except BaseException:
        _atomic_write(archive / MANIFEST, before)
        raise
    return copy.deepcopy(run)


def source_version_files(run_id, run):
    """Whitelist the optional extension without changing legacy manifest schemas."""
    _validate_status(run)
    extension = run.get("source_versions")
    if extension is None:
        if any(
            any(key.startswith("source_versions_") for key in forecast)
            for forecast in run["forecasts"].values()
        ):
            raise ValueError("来源候选附件缺少运行元数据")
        return set()
    if not isinstance(extension, dict):
        raise ValueError("来源候选运行元数据无效")
    outcomes = extension.get("leagues")
    if (
        not isinstance(outcomes, dict)
        or not outcomes
        or not set(outcomes) <= LEGACY_LEAGUES
        or set(outcomes) != set(run["leagues"]) & LEGACY_LEAGUES
    ):
        raise ValueError("来源候选必须完整覆盖本次已配置的旧规七联赛")
    params_path = f"models/{run_id}/source-expanded.params.json"
    if extension.get("params_path") != params_path or not _sha(extension.get("params_sha256")):
        raise ValueError("来源候选参数快照路径或 SHA256 无效")
    files = {params_path}
    for league, result in outcomes.items():
        _experimental(result, definition=True)
        if result["model_definition"]["league"] != league or result["model_definition"][
            "training_sources"
        ] != sorted(_sources(result)):
            raise ValueError("来源候选训练定义与联赛或来源范围不符")
        if result.get("training_database_sha256") != run["database_sha256"]:
            raise ValueError("来源候选与本次训练库不符")
        if _timestamp(result.get("training_cutoff_utc")) != _timestamp(run["training_cutoff_utc"]):
            raise ValueError("来源候选训练截点与主运行不符")
        train_path = f"models/{run_id}/{league}.source-expanded.train.json"
        if result.get("training_json_path") != train_path:
            raise ValueError("来源候选训练 JSON 路径无效")
        files.add(train_path)
        if result.get("status") == "trained":
            model_path = f"models/{run_id}/{league}.source-expanded.pkl"
            if result.get("model_path") != model_path or not _sha(result.get("sha256")):
                raise ValueError("来源候选模型路径或 SHA256 无效")
            files.add(model_path)
        elif result.get("status") == "failed":
            if (
                not isinstance(result.get("reason"), str)
                or not result["reason"].strip()
                or result.get("model_path") is not None
                or result.get("sha256") is not None
            ):
                raise ValueError("来源候选训练失败必须保留原因且不得声称存在模型")
        else:
            raise ValueError("来源候选训练状态无效")
    for forecast in run["forecasts"].values():
        path = forecast["path"] + "/source_versions.json"
        if forecast.get("source_versions_path") != path:
            raise ValueError("来源候选预测路径与主批次不符")
        _timestamp(forecast.get("source_versions_attached_utc"))
        files.add(path)
    return files


def _validate_targets(document, primary, outcomes):
    expected = {
        row["match_id"]: row for row in primary["predictions"] if row.get("league") in outcomes
    }
    rows = document["predictions"]
    keys = [row["match_id"] for row in rows]
    if (
        len(keys) != len(set(keys))
        or set(keys) != set(expected)
        or document["input"].get("match_ids") != keys
    ):
        raise ValueError("来源候选须完整保留本次已配置旧规联赛的主批次目标，含排除行")
    for row in rows:
        original = expected[row["match_id"]]
        for field in IDENTITY_FIELDS:
            if field not in row or field not in original:
                raise ValueError("来源候选缺少完整目标身份")
            left, right = row[field], original[field]
            if field == "kickoff_utc":
                left, right = _timestamp(left), _timestamp(right)
            if left != right:
                raise ValueError("来源候选目标身份与主批次不符")
        for field in ("home_team_id", "away_team_id", "sfc_only"):
            if field in original and row.get(field) != original[field]:
                raise ValueError("来源候选目标身份或 SFC 范围与主批次不符")
        _experimental(row, definition=True)
        result = outcomes.get(row["league"])
        if result is None or row["model_definition_id"] != result["model_definition_id"]:
            raise ValueError("来源候选目标缺少同定义的训练结果")
        if result["status"] == "failed":
            if (
                row["status"] != "excluded"
                or row.get("model_version") is not None
                or row.get("expected_definition") is not True
            ):
                raise ValueError("来源候选缺模型或训练失败必须显式排除")
        elif row.get("expected_definition") is True or row.get("model_version") != result["sha256"]:
            raise ValueError("来源候选预测与本次模型 SHA256 不符")


def _validate_document(document, primary, outcomes, day, *, allow_unknown_odds_time=False):
    _validate_batch(document, allow_unknown_odds_time=allow_unknown_odds_time)
    _experimental(document)
    if (
        type(document.get("model_definition_schema")) is not int
        or document["model_definition_schema"] != 1
    ):
        raise ValueError("来源候选必须使用 model_definition_schema=1，不能回退旧格式")
    if document.get("validation_status") != "experimental_unadopted":
        raise ValueError("来源候选永久保留实验状态")
    if (
        document["batch_id"] != primary["batch_id"]
        or _timestamp(document["as_of_utc"]) != _timestamp(primary["as_of_utc"])
        or document["mode"] != primary["mode"]
        or document["input"].get("kickoff_date_beijing") != day
        or document["input"].get("market_weight") != 0
        or _timestamp(document["generated_utc"]) < _timestamp(document["as_of_utc"])
    ):
        raise ValueError("来源候选批次、截点、日期或生成时间与主批次不符")
    for model in primary["models"]:
        if model["league"] in LEGACY_LEAGUES and set(model.get("sources", [])) != {"fd", "jc"}:
            raise ValueError("来源候选不可替换旧规主版来源")
    models = document["models"]
    expected = {league for league, result in outcomes.items() if result["status"] == "trained"}
    if len(models) != len(expected) or {model["league"] for model in models} != expected:
        raise ValueError("来源候选模型注册表与训练结果不符")
    for model in models:
        _experimental(model, definition=True)
        result = outcomes[model["league"]]
        if (
            model["model_definition_id"] != result["model_definition_id"]
            or model["model_version"] != result["sha256"]
            or _sources(model) != _sources(result)
            or _timestamp(model["model_as_of_utc"]) != _timestamp(result["training_cutoff_utc"])
        ):
            raise ValueError("来源候选模型定义、SHA256 或截点与训练结果不符")
    _validate_targets(document, primary, outcomes)


def verify_source_versions(archive, manifest, run, *, allow_unknown_odds_time=False):
    """Verify candidate content as well as hashes, without deserializing any pickle."""
    extension = run.get("source_versions")
    if extension is None:
        return
    outcomes = extension["leagues"]
    params_path = extension["params_path"]
    if manifest["files"][params_path] != extension["params_sha256"]:
        raise ValueError("来源候选参数快照 SHA256 与元数据不符")
    params = _read(_path(archive, params_path))
    if not isinstance(params.get("leagues"), dict) or set(params["leagues"]) != set(outcomes):
        raise ValueError("来源候选参数范围与训练结果不符")
    for league, result in outcomes.items():
        if _sources(params["leagues"][league]) != _sources(result):
            raise ValueError("来源候选参数来源与训练结果不符")
        training = _read(_path(archive, result["training_json_path"]))
        if training.get("training_database_sha256") != result["training_database_sha256"]:
            raise ValueError("来源候选训练 JSON 与本次训练库不符")
        for field in (
            "status",
            "source_variant",
            "model_definition_id",
            "model_definition",
            "p2_ready",
            "paper_eligible",
            "training_cutoff_utc",
            "sources",
        ):
            if field in training and training[field] != result.get(field):
                raise ValueError(f"来源候选训练 JSON 与结果不符：{field}")
        if result["status"] == "trained":
            if (
                manifest["files"][result["model_path"]] != result["sha256"]
                or training.get("model_path") != result["model_path"]
                or ("sha256" in training and training["sha256"] != result["sha256"])
            ):
                raise ValueError("来源候选模型元数据 SHA256 或训练 JSON 路径不符")
        elif training.get("status") != "failed" or training.get("reason") != result["reason"]:
            raise ValueError("来源候选失败训练 JSON 必须保留同一失败原因")
    for day, forecast in run["forecasts"].items():
        primary = load_batch(
            _path(archive, forecast["path"] + "/forecast.json"),
            allow_unknown_odds_time=allow_unknown_odds_time,
        )
        document = _read(_path(archive, forecast["source_versions_path"]))
        _validate_document(
            document,
            primary,
            outcomes,
            day,
            allow_unknown_odds_time=allow_unknown_odds_time,
        )
        attached = _timestamp(forecast["source_versions_attached_utc"])
        if attached < max(
            _timestamp(document["generated_utc"]), _timestamp(primary["generated_utc"])
        ):
            raise ValueError("来源候选实际附加时间早于预测生成时间")


def attach_source_versions(archive, stage, run_id, outcomes, documents, *, clock=None):
    """Append candidate artifacts after primary publication; roll back only this addition.

    ``stage`` owns the fixed candidate model/train paths and
    ``models/{run_id}/source-expanded.params.json``. Documents cover all primary
    dates, with precisely that date's old-policy seven-league target subset.
    Failed fits remain explicit outcomes and excluded predictions. No primary
    file, model index, experimental.json or paper-account input is rewritten.
    """
    archive, stage = Path(archive), Path(stage)
    manifest = verify_forecasts(archive)
    if manifest is None or run_id not in manifest["runs"]:
        raise ValueError("来源候选只能附加到已经冻结的主运行")
    before = (archive / MANIFEST).read_bytes()
    run = manifest["runs"][run_id]
    if "source_versions" in run or "source_version_status" in run:
        raise ValueError("来源候选已冻结，不允许重复附加")
    if not isinstance(documents, dict) or set(documents) != set(run["forecasts"]):
        raise ValueError("来源候选必须覆盖本次主运行全部预测日期")
    params_path = f"models/{run_id}/source-expanded.params.json"
    params_file = _path(stage, params_path)
    if not params_file.is_file():
        raise ValueError("来源候选参数快照缺失")
    run["source_versions"] = {
        "leagues": copy.deepcopy(outcomes),
        "params_path": params_path,
        "params_sha256": hashlib.sha256(params_file.read_bytes()).hexdigest(),
    }
    payloads = {}
    for day, document in documents.items():
        forecast = run["forecasts"][day]
        path = forecast["path"] + "/source_versions.json"
        forecast["source_versions_path"] = path
        # A provisional in-memory value permits layout validation. The persisted
        # value is sampled from the actual clock after all new files are written.
        forecast["source_versions_attached_utc"] = document.get("generated_utc")
        payloads[path] = _raw(document)
    files = source_version_files(run_id, run)
    for name in files:
        if _path(archive, name).exists():
            raise ValueError("来源候选文件已存在，不允许覆盖")
        if name not in payloads:
            source = _path(stage, name)
            if not source.is_file():
                raise ValueError(f"来源候选待归档文件缺失：{name}")
            payloads[name] = source.read_bytes()
    created, committed = [], False
    try:
        for name, content in sorted(payloads.items()):
            destination = _path(archive, name)
            with destination.open("xb") as stream:
                created.append(destination)
                stream.write(content)
            manifest["files"][name] = hashlib.sha256(content).hexdigest()
        for forecast in run["forecasts"].values():
            instant = clock() if clock else datetime.now(UTC)
            forecast["source_versions_attached_utc"] = _timestamp(instant.isoformat()).isoformat()
        run["source_version_status"] = {
            **_frozen_status(outcomes),
            "recorded_utc": max(
                forecast["source_versions_attached_utc"] for forecast in run["forecasts"].values()
            ),
        }
        _validate_status(run)
        verify_source_versions(archive, manifest, run)
        if (archive / MANIFEST).read_bytes() != before:
            raise ValueError("主清单在来源候选附加期间发生变化")
        _atomic_write(archive / MANIFEST, _raw(manifest))
        committed = True
        verify_forecasts(archive)
    except BaseException:
        if committed:
            _atomic_write(archive / MANIFEST, before)
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise
    return copy.deepcopy(run)
