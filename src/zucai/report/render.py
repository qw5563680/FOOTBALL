"""Immutable, database-independent exports of a versioned prediction batch."""

import csv
import hashlib
import html
import io
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

BEIJING = timezone(timedelta(hours=8))
EXPORT_FILES = ("forecast.json", "report.md", "report.csv")
OUTCOMES = ("h", "d", "a")


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("时间必须为带时区的 ISO8601 字符串")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("时间必须为带时区的 ISO8601 字符串") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("时间必须包含时区")
    return result


def _text(value: object, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")


def _number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _triplet(value: object, field: str, *, probabilities: bool = False) -> None:
    if not isinstance(value, dict) or set(value) != set(OUTCOMES):
        raise ValueError(f"{field} 必须包含 h/d/a")
    if not all(_number(value[key]) for key in OUTCOMES):
        raise ValueError(f"{field} 必须是有限数值")
    if probabilities and (
        any(not 0 <= value[key] <= 1 for key in OUTCOMES)
        or not math.isclose(sum(value.values()), 1.0, abs_tol=1e-6)
    ):
        raise ValueError(f"{field} 概率必须在 [0,1] 且合计为 1")


def _validate_batch(batch: object) -> None:
    """Check the interchange contract without loading a model or consulting a DB."""
    if not isinstance(batch, dict) or type(batch.get("schema_version")) is not int:
        raise ValueError("schema_version 必须为 1")
    if batch["schema_version"] != 1:
        raise ValueError("不支持的 schema_version，必须为 1")
    _text(batch.get("batch_id"), "batch_id")
    for key in ("generated_utc", "as_of_utc"):
        _timestamp(batch.get(key))
    if batch.get("mode") not in ("prematch", "historical_replay"):
        raise ValueError("mode 必须是 prematch 或 historical_replay")
    if (
        batch.get("probability_type") != "P2"
        or batch.get("validation_status") != "experimental_unadopted"
        or batch.get("bet_allowed") is not False
    ):
        raise ValueError("报告仅支持 P2 实验、experimental_unadopted 和 BET 禁用")
    if not isinstance(batch.get("input"), dict):
        raise ValueError("input 必须为对象")
    if not isinstance(batch.get("models"), list) or not isinstance(batch.get("predictions"), list):
        raise ValueError("models 和 predictions 必须为列表")
    for model in batch["models"]:
        if not isinstance(model, dict):
            raise ValueError("models 项必须为对象")
        for key in ("model_name", "model_version", "source", "league", "availability_basis"):
            _text(model.get(key), key)
        for key in ("model_as_of_utc", "last_available_utc"):
            _timestamp(model.get(key))
        if type(model.get("training_games")) is not int or model["training_games"] < 0:
            raise ValueError("training_games 必须为非负整数")
        share = model.get("assumed_training_share")
        if share is not None and (not _number(share) or not 0 <= share <= 1):
            raise ValueError("assumed_training_share 必须在 [0,1] 或为 null")
    for prediction in batch["predictions"]:
        if not isinstance(prediction, dict):
            raise ValueError("predictions 项必须为对象")
        _text(prediction.get("match_id"), "match_id")
        if prediction.get("status") == "excluded":
            _text(prediction.get("reason"), "排除原因")
            if any(
                prediction.get(key) is not None for key in ("model_probabilities", "score_matrix")
            ):
                raise ValueError("排除场次不得填写模型概率或比分矩阵")
            continue
        if prediction.get("status") != "predicted":
            raise ValueError("比赛状态必须是 predicted 或 excluded")
        for key in ("home", "away", "source", "league", "model_name", "model_version"):
            _text(prediction.get(key), key)
        _timestamp(prediction.get("kickoff_utc"))
        _triplet(prediction.get("model_probabilities"), "model_probabilities", probabilities=True)
        matrix = prediction.get("score_matrix")
        if (
            not isinstance(matrix, list)
            or not matrix
            or any(not isinstance(row, list) or len(row) != len(matrix) for row in matrix)
            or any(not _number(cell) or cell < 0 for row in matrix for cell in row)
            or not math.isclose(sum(sum(row) for row in matrix), 1.0, abs_tol=1e-6)
        ):
            raise ValueError("score_matrix 必须是非负且合计为 1 的方阵")
        scores = prediction.get("top_scores")
        if not isinstance(scores, list):
            raise ValueError("top_scores 必须为列表")
        for score in scores:
            if not isinstance(score, dict):
                raise ValueError("比分项必须为对象")
            _text(score.get("score"), "score")
            probability = score.get("probability")
            if not _number(probability) or not 0 <= probability <= 1:
                raise ValueError("比分概率必须在 [0,1]")
        market = prediction.get("market")
        if market is not None:
            if not isinstance(market, dict):
                raise ValueError("market 必须为对象或 null")
            _triplet(market.get("prices"), "market.prices")
            if any(price <= 1 for price in market["prices"].values()):
                raise ValueError("市场 SP 必须大于 1")
            _triplet(market.get("probabilities"), "market.probabilities", probabilities=True)
            _timestamp(market.get("snapshot_utc"))
        ev = prediction.get("ev")
        if ev is not None:
            if market is None:
                raise ValueError("没有市场快照时 EV 必须为空")
            _triplet(ev, "ev")
        blend = prediction.get("experimental_blend")
        if blend is not None:
            if not isinstance(blend, dict) or market is None:
                raise ValueError("实验混合必须为含市场快照的对象")
            _triplet(blend.get("probabilities"), "experimental_blend", probabilities=True)


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")


def _md(value: object) -> str:
    if value is None:
        return "—"
    result = html.escape(str(value), quote=False)
    result = re.sub(r"([\\`*_{}\[\]()#+.!|])", r"\\\1", result)
    return result.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


def _beijing(value: str | None) -> str:
    if value is None:
        return "—"
    return _timestamp(value).astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S")


def _percent(value: float | None, *, highlight: bool = False) -> str:
    if value is None:
        return "—"
    text = f"{value:.2%}"
    return f"**{text}**" if highlight and value > 0.05 else text


def _markdown(batch: dict) -> str:
    lines = [
        "# P2 实验预测报告",
        "",
        "> **非购买建议 · BET 禁用 · experimental_unadopted（未采用、未验证的实验）**",
        "",
        "常规 90 分钟含补时。纯模型仅使用球队历史比分；市场 SP 和去水概率单独对照。",
        "EV 为模型概率 × SP − 1 的点估计，EV > 0.05 仅加粗显示，不构成 BET 或购买建议。",
        "",
    ]
    if batch["mode"] == "historical_replay":
        lines.extend(
            [
                "> **历史回放（historical_replay）：非当时冻结，不是赛前实录或未见验证。**",
                "",
            ]
        )
    lines.extend(
        [
            f"批次：{_md(batch['batch_id'])}；模式：{_md(batch['mode'])}。",
            f"生成时间（北京时间）：{_beijing(batch['generated_utc'])}；"
            f"数据截止（北京时间）：{_beijing(batch['as_of_utc'])}。",
            "",
            "## 模型与训练范围",
            "",
            "版本为模型文件 SHA256；所有展示时间均为北京时间。assumed 占比表示"
            "使用假定赛果可知时间的训练场次占比，不代表已核实发布时间。",
            "",
            "| 模型 | 版本 SHA256 | 来源 | 联赛 | 模型截点 | 训练场数 | "
            "最近赛果可知时间 | 可知依据 | assumed 占比 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for model in batch["models"]:
        values = [
            _md(model["model_name"]),
            _md(model["model_version"]),
            _md(model["source"]),
            _md(model["league"]),
            _beijing(model["model_as_of_utc"]),
            str(model["training_games"]),
            _beijing(model["last_available_utc"]),
            _md(model["availability_basis"]),
            _percent(model.get("assumed_training_share")),
        ]
        lines.append("| " + " | ".join(values) + " |")
    lines.extend(
        [
            "",
            "## 纯模型与市场对照",
            "",
            "| 比赛 ID / 编号 | 联赛 / 对阵 | 开球（北京时间） | 状态 / 排除原因 | "
            "模型主胜 | 模型平局 | 模型客胜 | 市场主胜 | 市场平局 | 市场客胜 | "
            "SP 主胜 | SP 平局 | SP 客胜 | EV 主胜 | EV 平局 | EV 客胜 |",
            "|" + " --- |" * 16,
        ]
    )
    for prediction in batch["predictions"]:
        market = prediction.get("market") or {}
        model_p = prediction.get("model_probabilities") or {}
        market_p, prices, ev = (
            market.get("probabilities", {}),
            market.get("prices", {}),
            prediction.get("ev") or {},
        )
        values = [
            f"{_md(prediction['match_id'])} / {_md(prediction.get('jc_num'))}",
            f"{_md(prediction.get('league'))} / "
            f"{_md(prediction.get('home'))} vs {_md(prediction.get('away'))}",
            _beijing(prediction.get("kickoff_utc")),
            "已预测"
            if prediction["status"] == "predicted"
            else "排除：" + _md(prediction["reason"]),
            *[_percent(model_p.get(key)) for key in OUTCOMES],
            *[_percent(market_p.get(key)) for key in OUTCOMES],
            *[str(prices[key]) if key in prices else "—" for key in OUTCOMES],
            *[_percent(ev.get(key), highlight=True) for key in OUTCOMES],
        ]
        lines.append("| " + " | ".join(values) + " |")
    lines.extend(["", "## 比分前五与快照", ""])
    for prediction in batch["predictions"]:
        if prediction["status"] != "predicted":
            continue
        scores = (
            "、".join(
                f"{_md(item['score'])}（{_percent(item['probability'])}）"
                for item in prediction["top_scores"][:5]
            )
            or "—"
        )
        market = prediction.get("market")
        snapshot = _beijing(market["snapshot_utc"]) if market else "无完整赛前快照"
        lines.append(
            f"- {_md(prediction['match_id'])}：{scores}；市场快照（北京时间）：{snapshot}；"
            f"模型：{_md(prediction['model_name'])} / {_md(prediction['model_version'])}。"
        )
    blends = [p for p in batch["predictions"] if p.get("experimental_blend") is not None]
    if blends:
        lines.extend(
            [
                "",
                "## 实验混合（市场相关，仅单列）",
                "",
                "experimental_blend 不替代上表纯模型概率或比分矩阵，仍为 P2 实验，BET 禁用。",
                "",
                "| 比赛 ID | 实验模型 | 主胜 | 平局 | 客胜 | 市场权重 |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for prediction in blends:
            blend = prediction["experimental_blend"]
            values = [
                _md(prediction["match_id"]),
                _md(blend.get("model_name")),
                *[_percent(blend["probabilities"][key]) for key in OUTCOMES],
                _percent(blend.get("market_weight")),
            ]
            lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines) + "\n"


def _csv_cell(value: object) -> object:
    # Only text is protected: negative EV and probabilities stay numeric.
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r", "\n")):
        return "'" + value
    return value


def _csv_bytes(batch: dict) -> bytes:
    stream = io.StringIO(newline="")
    columns = [
        "batch_id",
        "generated_utc",
        "as_of_utc",
        "mode",
        "probability_type",
        "validation_status",
        "bet_allowed",
        "match_id",
        "jc_num",
        "source",
        "league",
        "home",
        "away",
        "kickoff_utc",
        "kickoff_beijing",
        "status",
        "reason",
        "model_name",
        "model_version",
        "model_h",
        "model_d",
        "model_a",
        "market_h",
        "market_d",
        "market_a",
        "sp_h",
        "sp_d",
        "sp_a",
        "ev_h",
        "ev_d",
        "ev_a",
        "market_snapshot_utc",
        "top_scores",
        "experimental_blend_model",
        "experimental_blend_h",
        "experimental_blend_d",
        "experimental_blend_a",
        "experimental_blend_market_weight",
    ]
    writer = csv.DictWriter(stream, fieldnames=columns)
    writer.writeheader()
    for prediction in batch["predictions"]:
        row = {key: batch[key] for key in columns[:7]}
        row.update({key: prediction.get(key) for key in columns[7:19]})
        row["kickoff_beijing"] = (
            _timestamp(prediction["kickoff_utc"]).astimezone(BEIJING).isoformat()
            if prediction.get("kickoff_utc")
            else None
        )
        market = prediction.get("market") or {}
        blend = prediction.get("experimental_blend") or {}
        for prefix, values in (
            ("model", prediction.get("model_probabilities")),
            ("market", market.get("probabilities")),
            ("sp", market.get("prices")),
            ("ev", prediction.get("ev")),
            ("experimental_blend", blend.get("probabilities")),
        ):
            row.update({f"{prefix}_{key}": (values or {}).get(key) for key in OUTCOMES})
        row.update(
            {
                "market_snapshot_utc": market.get("snapshot_utc"),
                "top_scores": json.dumps(prediction.get("top_scores", [])[:5], ensure_ascii=False),
                "experimental_blend_model": blend.get("model_name"),
                "experimental_blend_market_weight": blend.get("market_weight"),
            }
        )
        writer.writerow({key: _csv_cell(value) for key, value in row.items()})
    return stream.getvalue().encode("utf-8-sig")


def write_report(batch: dict, output: Path) -> Path:
    """Write four exclusive files into a new directory; return its forecast JSON.

    On failure only fixed files successfully opened by this invocation are removed.
    No existing output directory is accepted, even if empty.
    """
    _validate_batch(batch)
    output = Path(output)
    forecast = _json_bytes(batch)
    frozen = json.loads(forecast)
    exports = {
        "forecast.json": forecast,
        "report.md": _markdown(frozen).encode("utf-8"),
        "report.csv": _csv_bytes(frozen),
    }
    manifest = {
        "schema_version": 1,
        "algorithm": "sha256",
        "files": {name: hashlib.sha256(content).hexdigest() for name, content in exports.items()},
    }
    exports["manifest.json"] = _json_bytes(manifest)
    output.mkdir(parents=True, exist_ok=False)
    created = []
    try:
        for name, content in exports.items():
            path = output / name
            with path.open("xb") as stream:
                created.append(path)
                stream.write(content)
    except BaseException:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        try:
            output.rmdir()
        except OSError:
            pass  # A file not owned by this invocation must survive cleanup.
        raise
    return output / "forecast.json"


def load_batch(path: Path) -> dict:
    """Read a frozen batch only after verifying its sibling manifest hashes."""
    path = Path(path)
    if path.name != "forecast.json":
        raise ValueError("批次文件必须为 forecast.json")
    manifest = json.loads((path.parent / "manifest.json").read_text(encoding="utf-8"))
    if (
        not isinstance(manifest, dict)
        or type(manifest.get("schema_version")) is not int
        or manifest["schema_version"] != 1
        or manifest.get("algorithm") != "sha256"
    ):
        raise ValueError("无效的 SHA256 manifest schema_version 或 algorithm")
    files = manifest.get("files")
    if (
        not isinstance(files, dict)
        or "forecast.json" not in files
        or not set(files) <= set(EXPORT_FILES)
    ):
        raise ValueError("manifest 必须包含 forecast.json 且只能引用固定导出文件")
    contents = {}
    for name, digest in files.items():
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("无效的 SHA256 校验值")
        target = path.parent / name
        if target.is_symlink():
            raise ValueError("冻结文件不能是符号链接")
        content = target.read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError(f"SHA256 校验失败：{name}")
        contents[name] = content
    batch = json.loads(contents["forecast.json"])
    _validate_batch(batch)
    return batch
