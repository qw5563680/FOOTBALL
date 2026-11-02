"""Append-only model/report archives, separate from the portable feed manifest."""

import json
import re
import shutil
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from zucai.cloud_feeds import digest
from zucai.report.render import load_batch

MANIFEST = "forecast_manifest.json"
LATEST = "models/latest.json"
REPORT_FILES = ("forecast.json", "report.md", "report.csv", "manifest.json")


def _component(value: str) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise ValueError("无效归档路径")
    if any(c in value for c in "/\\\x00\n\r"):
        raise ValueError("无效归档路径")
    return value


def _path(root: Path, relative: str) -> Path:
    parts = relative.split("/")
    for part in parts:
        _component(part)
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f"归档不允许符号链接：{relative}")
    return path


def _read(path: Path) -> dict:
    try:
        result = json.loads(path.read_bytes())
        if not isinstance(result, dict):
            raise ValueError("清单必须是对象")
        return result
    except (OSError, ValueError) as exc:
        raise ValueError(f"无法读取归档 JSON：{path}") from exc


def _write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _run_files(run_id: str, run: dict) -> set[str]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", run_id) or run["run_id"] != run_id:
        raise ValueError("无效 run ID")
    files = set()
    if not run["leagues"] or len(run["forecasts"]) != 2:
        raise ValueError("归档必须包含联赛训练结果和两个北京日")
    for league, result in run["leagues"].items():
        _component(league)
        train_path = f"models/{run_id}/{league}.train.json"
        if result["training_json_path"] != train_path:
            raise ValueError("训练 JSON 路径与 run ID 不符")
        files.add(train_path)
        if result["status"] == "trained":
            model_path = f"models/{run_id}/{league}.pkl"
            if result["model_path"] != model_path:
                raise ValueError("模型路径与 run ID 不符")
            if result["training_database_sha256"] != run["database_sha256"]:
                raise ValueError("模型与本次训练库不符")
            files.add(model_path)
        elif result["status"] != "failed":
            raise ValueError("无效联赛训练状态")
    for day, forecast in run["forecasts"].items():
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError("无效北京日")
        directory = f"forecasts/{day}/{run_id}"
        if forecast["path"] != directory:
            raise ValueError("预测目录与北京日 / run ID 不符")
        files.update(f"{directory}/{name}" for name in REPORT_FILES)
    return files


def verify_forecasts(archive: Path) -> dict | None:
    """Verify bytes and frozen report evidence without loading executable model pickles."""
    manifest_path = _path(archive, MANIFEST)
    if not manifest_path.exists():
        if any((archive / name).exists() for name in ("models", "forecasts")):
            raise ValueError("预测归档缺少 forecast_manifest.json")
        return None
    manifest = _read(manifest_path)
    try:
        if manifest["version"] != 1 or manifest["algorithm"] != "sha256":
            raise ValueError("不支持的预测清单版本")
        expected = {LATEST}
        for run_id, run in manifest["runs"].items():
            expected.update(_run_files(run_id, run))
        if set(manifest["files"]) != expected:
            raise ValueError("预测清单文件列表不完整或有额外路径")
        for name, sha in manifest["files"].items():
            path = _path(archive, name)
            if not path.is_file() or digest(path) != sha:
                raise ValueError(f"预测归档 SHA256 不符：{name}")
        known_models = {}
        for run in manifest["runs"].values():
            successes = {}
            for league, result in run["leagues"].items():
                if result["status"] == "trained":
                    if manifest["files"][result["model_path"]] != result["sha256"]:
                        raise ValueError("模型元数据 SHA256 不符")
                    known_models[result["model_path"]] = (league, result)
                    successes[league] = result["sha256"]
            for forecast in run["forecasts"].values():
                batch = load_batch(archive / forecast["path"] / "forecast.json")
                if (batch["batch_id"], batch["generated_utc"]) != (
                    forecast["batch_id"],
                    forecast["generated_utc"],
                ):
                    raise ValueError("预测批次与清单不符")
                if {m["league"]: m["model_version"] for m in batch["models"]} != successes:
                    raise ValueError("预测模型与本次训练结果不符")
        latest = _read(archive / LATEST)
        for league, result in latest["models"].items():
            if known_models.get(result["model_path"]) != (league, result):
                raise ValueError("最新模型索引指向未知模型")
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("预测清单结构不完整") from exc
    return manifest


def publish_forecasts(archive: Path, stage: Path, run: dict, *, before_commit=None) -> dict:
    """Copy a new immutable run, then replace indexes; undo only this run on error."""
    old = verify_forecasts(archive)
    run_id = run["run_id"]
    files = _run_files(run_id, run)
    if old and run_id in old["runs"]:
        raise ValueError(f"run ID 已存在：{run_id}")
    directories = [f"models/{run_id}", *(f["path"] for f in run["forecasts"].values())]
    for name in directories:
        if _path(archive, name).exists():
            raise ValueError(f"归档目录已存在：{name}")
    for name in files:
        if not _path(stage, name).is_file():
            raise ValueError(f"待归档文件缺失：{name}")
    archive.mkdir(parents=True, exist_ok=True)
    latest = _read(archive / LATEST) if old else {"models": {}}
    latest["generated_utc"] = run["generated_utc"]
    latest["models"].update(
        {
            league: result
            for league, result in run["leagues"].items()
            if result["status"] == "trained"
        }
    )
    manifest = {
        "version": 1,
        "algorithm": "sha256",
        "generated_utc": run["generated_utc"],
        "runs": {**(old["runs"] if old else {}), run_id: run},
        "files": dict(old["files"]) if old else {},
    }
    backups = {name: (archive / name).read_bytes() if old else None for name in (LATEST, MANIFEST)}
    created = []
    try:
        for directory in directories:
            destination = archive / directory
            destination.mkdir(parents=True)  # Refuse replacement even if a concurrent writer raced.
            created.append(destination)
            for name in sorted(files):
                if Path(name).parent.as_posix() == directory:
                    shutil.copyfile(stage / name, archive / name)
                    manifest["files"][name] = digest(archive / name)
        with TemporaryDirectory(prefix=".forecast-", dir=archive) as temporary:
            latest_tmp, manifest_tmp = Path(temporary) / "latest.json", Path(temporary) / MANIFEST
            _write(latest_tmp, latest)
            manifest["files"][LATEST] = digest(latest_tmp)
            _write(manifest_tmp, manifest)
            latest_tmp.replace(archive / LATEST)
            manifest_tmp.replace(archive / MANIFEST)
            verify_forecasts(archive)
            if before_commit:
                before_commit()
    except BaseException:
        for name, data in backups.items():
            if data is None:
                (archive / name).unlink(missing_ok=True)
            else:
                (archive / name).write_bytes(data)
        for directory in reversed(created):
            shutil.rmtree(directory)
        # Remove empty parent directories from a failed first publication as well.
        for directory in [
            *(archive / f"forecasts/{d}" for d in run["forecasts"]),
            archive / "forecasts",
            archive / "models",
        ]:
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        raise
    return manifest
