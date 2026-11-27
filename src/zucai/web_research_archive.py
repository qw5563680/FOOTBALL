"""Freeze T70 evidence alongside its forecast without modifying forecast archives."""

import hashlib
import json
import re
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path

from zucai.forecast_archive import MANIFEST, _path, _read
from zucai.report.render import _timestamp, _triplet
from zucai.scoreboard.score import frozen_market

RUNS = "web_research/runs"
RECORD_STATUSES = {"ok", "unparsed", "identity_mismatch", "failed", "partial"}
IDENTITY_FIELDS = ("match_id", "jc_num", "league", "home", "away")
_ATTACHMENT_FIELDS = {"report_path", "response_path"}
_INDEX_FIELDS = {"version", "frozen_utc", "documents", "files", "record_count"}


def _run_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise ValueError("联网研究运行编号无效")
    return value


def _checksum(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("联网研究 SHA256 无效")
    return value


def _instant(value):
    result = _timestamp(value) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.utcoffset() is None:
        raise ValueError("联网研究时间必须包含时区")
    return result.astimezone(UTC)


def _json_bytes(value):
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _marker(archive, run_id):
    return _path(archive, f"{RUNS}/{_run_id(run_id)}/manifest.json")


def _run_times(meta):
    _run_id(meta["run_id"])
    _checksum(meta["prompt_sha256"])
    if meta["status"] not in {"complete", "partial"} or any(
        not isinstance(meta.get(key), str) or not meta[key].strip()
        for key in ("provider", "model", "prompt_version")
    ):
        raise ValueError("联网研究运行元数据无效")
    started, generated = map(_instant, (meta["started_utc"], meta["generated_utc"]))
    if generated < started:
        raise ValueError("联网研究运行生成时刻早于开始时刻")
    return started, generated


def _forecast_location(value, run_id):
    root, day, actual_id, filename = value.split("/")
    if (
        root != "forecasts"
        or filename != "forecast.json"
        or actual_id != _run_id(run_id)
        or date.fromisoformat(day).isoformat() != day
    ):
        raise ValueError("联网研究预测来源路径无效")
    return day, value.removesuffix("/forecast.json")


def _forecast_receipt(entry, row):
    stored = entry.get("publication_evidence")
    receipt = row.get("forecast_publication_evidence") or stored
    if (
        not isinstance(receipt, dict)
        or (stored is not None and stored != receipt)
        or (entry.get("publication_required") and stored is None)
        or receipt.get("method")
        != (
            "post_push_confirmation" if stored is not None else "historical_first_containing_commit"
        )
        or not re.fullmatch(r"[0-9a-f]{40,64}", receipt.get("commit", ""))
        or not re.fullmatch(r"refs/remotes/[^/]+/data/football-feeds", receipt.get("ref", ""))
        or receipt.get("candidate_path") != row["forecast_path"]
        or receipt.get("candidate_sha256") != row["forecast_sha256"]
        or _instant(receipt.get("published_utc")) != _instant(row["forecast_published_utc"])
        or (
            stored is not None
            and _instant(entry.get("published_utc")) != _instant(receipt["published_utc"])
        )
    ):
        raise ValueError("联网研究预测来源的发布回执无效")


def _checked_bytes(root, path, sha):
    file = _path(root, path)
    if not file.is_file():
        raise ValueError("联网研究归档文件缺失")
    raw = file.read_bytes()
    if _sha(raw) != _checksum(sha):
        raise ValueError("联网研究归档 SHA256 不符")
    return raw


def _checked_forecast(archive, row, cache):
    if MANIFEST not in cache:
        cache[MANIFEST] = _forecast_manifest(archive)
    manifest = cache[MANIFEST]
    path, run_id, sha = (row[k] for k in ("forecast_path", "forecast_run_id", "forecast_sha256"))
    day, directory = _forecast_location(path, run_id)
    entry = manifest["runs"][run_id]["forecasts"][day]
    if entry["path"] != directory or manifest["files"][path] != sha:
        raise ValueError("联网研究预测来源不在冻结清单中")
    _forecast_receipt(entry, row)
    if path not in cache:
        batch = json.loads(_checked_bytes(archive, path, sha))
        predictions = {p["match_id"]: p for p in batch["predictions"]}
        if (
            batch["mode"] != "prematch"
            or batch["batch_id"] != entry["batch_id"]
            or batch["generated_utc"] != entry["generated_utc"]
            or len(predictions) != len(batch["predictions"])
        ):
            raise ValueError("联网研究来源不是唯一有效赛前预测批次")
        cache[path] = batch, predictions
    batch, predictions = cache[path]
    original = predictions[row["match_id"]]
    market = frozen_market(
        {**original, **{key: batch[key] for key in ("as_of_utc", "generated_utc")}}
    )
    if (
        original["status"] != "predicted"
        or original["source"] != "jc"
        or any(original[key] != row["identity"][key] for key in IDENTITY_FIELDS)
        or any(
            _instant(value) != _instant(row["kickoff_utc"])
            for value in (original["kickoff_utc"], row["identity"]["kickoff_utc"])
        )
        or row["model_probabilities"] != original["model_probabilities"]
        or row["market_probabilities"] != (market["probabilities"] if market else None)
        or not _instant(batch["as_of_utc"])
        <= _instant(batch["generated_utc"])
        <= _instant(row["forecast_published_utc"])
        <= _instant(row["cutoff_utc"])
    ):
        raise ValueError("联网研究比赛与冻结预测来源不符")
    _triplet(row["model_probabilities"], "联网研究球队基准", probabilities=True)
    return directory


def _checked_row(row, started, generated):
    match_id = row["match_id"]
    if (
        not isinstance(match_id, str)
        or not re.fullmatch(r"jc:[0-9]+", match_id)
        or row["identity"]["match_id"] != match_id
        or row["status"] not in RECORD_STATUSES
        or (row["status"] == "ok") != isinstance(row["structured"], dict)
        or (row["status"] != "ok" and row["structured"] is not None)
        or not started
        <= _instant(row["cutoff_utc"])
        <= _instant(row["completed_utc"])
        <= generated
        < _instant(row["kickoff_utc"])
    ):
        raise ValueError("联网研究结果无效或已超过开球时刻")


def _forecast_manifest(archive):
    manifest = _read(_path(archive, MANIFEST))
    if manifest.get("algorithm") != "sha256" or manifest.get("version") != 1:
        raise ValueError("联网研究预测来源清单无效")
    return manifest


def _prepared(archive, run, artifacts):
    started, generated = _run_times(run)
    rows = run["records"]
    ids = {row["match_id"] for row in rows}
    if len(ids) != len(rows) or not artifacts.keys() <= ids or _INDEX_FIELDS & run.keys():
        raise ValueError("联网研究运行包含重复比赛、未记录原件或保留字段")
    groups = defaultdict(list)
    payloads, cache = {}, {}
    for row in rows:
        if _ATTACHMENT_FIELDS & row.keys():
            raise ValueError("联网研究记录包含保留路径")
        _checked_row(row, started, generated)
        directory = _checked_forecast(archive, row, cache)
        group = f"{directory}/web_research/{run['run_id']}"
        record = {**row, "report_path": None, "response_path": None}
        if row["match_id"] in artifacts:
            payload = artifacts[row["match_id"]]
            if not isinstance(payload, dict):
                raise ValueError("联网研究接口原件必须是对象")
            stem = f"{group}/{len(groups[group]) + 1}"
            record["response_path"] = f"{stem}.response.json"
            payloads[record["response_path"]] = _json_bytes(payload)
            if payload.get("report_text") is not None:
                record["report_path"] = f"{stem}.report.md"
                payloads[record["report_path"]] = payload["report_text"].encode("utf-8")
        groups[group].append(record)
    documents = []
    for group, records in sorted(groups.items()):
        doc_path = f"{group}/web_research.json"
        documents.append(doc_path)
        payloads[doc_path] = _json_bytes(
            {"version": 1, "run_id": run["run_id"], "records": records}
        )
    return payloads, {
        **{key: value for key, value in run.items() if key != "records"},
        "version": 1,
        "record_count": len(rows),
        "documents": documents,
        "files": {name: _sha(raw) for name, raw in payloads.items()},
    }


def _read_index(path, run_id):
    manifest = _read(path)
    started, generated = _run_times(manifest)
    frozen = _instant(manifest["frozen_utc"])
    if (
        type(manifest["version"]) is not int
        or manifest["version"] != 1
        or manifest["run_id"] != run_id
        or frozen < generated
        or type(manifest["record_count"]) is not int
        or manifest["record_count"] < 0
        or not isinstance(manifest["documents"], list)
        or manifest["documents"] != sorted(set(manifest["documents"]))
        or not isinstance(manifest["files"], dict)
    ):
        raise ValueError("联网研究运行清单结构无效")
    return manifest, started, generated, frozen


def _read_rows(archive, manifest, started, generated, frozen, cache):
    run_id, files = manifest["run_id"], manifest["files"]
    result, seen, required = [], set(), set()
    for path in manifest["documents"]:
        if not re.fullmatch(
            rf"forecasts/[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}/[A-Za-z0-9][A-Za-z0-9_.-]*/"
            rf"web_research/{re.escape(run_id)}/web_research\.json",
            path,
        ):
            raise ValueError("联网研究结构化原件路径无效")
        sha = files[path]
        document = json.loads(_checked_bytes(archive, path, sha))
        if document["version"] != 1 or document["run_id"] != run_id or not document["records"]:
            raise ValueError("联网研究结构化原件无效")
        required.add(path)
        for ordinal, row in enumerate(document["records"], 1):
            _checked_row(row, started, generated)
            directory = _checked_forecast(archive, row, cache)
            group = f"{directory}/web_research/{run_id}"
            if (
                path != f"{group}/web_research.json"
                or row["match_id"] in seen
                or frozen >= _instant(row["kickoff_utc"])
            ):
                raise ValueError("联网研究归档重复、路径不符或实际冻结晚于开球")
            seen.add(row["match_id"])
            for field, suffix in (("response_path", "response.json"), ("report_path", "report.md")):
                attachment = row[field]
                if attachment is not None:
                    if attachment != f"{group}/{ordinal}.{suffix}":
                        raise ValueError("联网研究原件路径无效")
                    _checked_bytes(archive, attachment, files[attachment])
                    required.add(attachment)
            result.append(
                {
                    **row,
                    **{
                        key: manifest[key]
                        for key in (
                            "run_id",
                            "provider",
                            "model",
                            "prompt_version",
                            "prompt_sha256",
                            "frozen_utc",
                        )
                    },
                    "archive_path": path,
                    "archive_sha256": sha,
                }
            )
    if set(files) != required or len(result) != manifest["record_count"]:
        raise ValueError("联网研究运行清单与结构化原件不符")
    return result


def has_runs(archive):
    """A disabled installation has no completed research-run markers."""
    return any(_path(Path(archive), RUNS).glob("*/manifest.json"))


def read_run(archive, run_id):
    """Read a verified, immutable run before deciding whether to spend on it again."""
    archive = Path(archive)
    marker = _marker(archive, run_id)
    if not marker.exists():
        if marker.parent.exists():
            raise ValueError("联网研究运行尚未完整冻结，请使用新运行编号")
        return None
    try:
        manifest, started, generated, frozen = _read_index(marker, run_id)
        _read_rows(archive, manifest, started, generated, frozen, {})
        return manifest
    except (KeyError, TypeError, AttributeError, OSError) as exc:
        raise ValueError("联网研究归档结构无效") from exc


def load(archive, as_of):
    """Return verified independent rows visible at a historical cutoff."""
    archive, clock = Path(archive), _instant(as_of)
    rows, cache = [], {}
    try:
        for path in sorted(_path(archive, RUNS).glob("*/manifest.json")):
            run_id = path.parent.name
            manifest, started, generated, frozen = _read_index(_marker(archive, run_id), run_id)
            if frozen <= clock:
                rows.extend(_read_rows(archive, manifest, started, generated, frozen, cache))
    except (KeyError, TypeError, AttributeError, OSError) as exc:
        raise ValueError("联网研究归档结构无效") from exc
    return sorted(
        rows, key=lambda row: (_instant(row["frozen_utc"]), row["run_id"], row["match_id"])
    )


def publish(archive, run, artifacts, *, clock=None):
    """Publish all hashes and payloads with a last, atomic run-visibility marker."""
    archive = Path(archive)
    try:
        payloads, manifest = _prepared(archive, run, artifacts)
        marker = _marker(archive, run["run_id"])
        existing = read_run(archive, run["run_id"])
    except (KeyError, TypeError, AttributeError, OSError) as exc:
        raise ValueError("联网研究待归档数据结构无效") from exc
    if existing is not None:
        if manifest != {key: value for key, value in existing.items() if key != "frozen_utc"}:
            raise ValueError("联网研究运行已冻结，不允许覆盖")
        return existing

    clock = clock or (lambda: datetime.now(UTC))
    marker.parent.mkdir(parents=True)
    staged_marker = marker.with_name(".manifest.tmp")
    created, directories = [], []
    try:
        for name, raw in sorted(payloads.items()):
            target = _path(archive, name)
            if target.parent not in directories:
                target.parent.mkdir(parents=True)
                directories.append(target.parent)
            with target.open("xb") as stream:
                created.append(target)
                stream.write(raw)
        frozen = _instant(clock())
        if frozen < _instant(run["generated_utc"]) or any(
            frozen >= _instant(row["kickoff_utc"]) for row in run["records"]
        ):
            raise ValueError("联网研究实际冻结时刻无效或已超过开球时刻")
        manifest["frozen_utc"] = frozen.isoformat()
        with staged_marker.open("xb") as stream:
            created.append(staged_marker)
            stream.write(_json_bytes(manifest))
        staged_marker.replace(marker)
    except BaseException:
        # Preserve a complete snapshot if cancellation arrived after the marker rename.
        if not marker.exists():
            for path in reversed(created):
                path.unlink(missing_ok=True)
            for directory in reversed(directories):
                directory.rmdir()
            marker.parent.rmdir()
        raise
    return manifest
