"""Result-selected parameters and a conservative, explicit P2 evidence gate."""

import hashlib
import json
import logging
import math
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from zucai.predict.base import utc
from zucai.predict.seasons import latest_complete_seasons

PARAMS_PATH = Path(__file__).resolve().parents[3] / "data/model/params.json"
logger = logging.getLogger(__name__)
TRAINING_POLICY = {
    "history_scope": "cumulative",
    "strict_availability": False,
    "replay_known_after": None,
}
GATE = {
    "complete_seasons": 2,
    "min_evaluated": 500,
    "min_closing_paired": 500,
    "min_uniform_log_loss_gain": 0.05,
    "max_closing_log_loss_gap": 0.05,
    "fold": "weekly",
    "bet_allowed": False,
    "require_two_nonempty_complete_seasons": True,
    "require_no_skips_or_unavailable_labels": True,
    "artifact_corpus": "matching_sources_and_cumulative_nonreplayed_policy",
    "evidence_recency": "latest_two_complete_seasons_at_prediction",
}


def _parameter_paths(path=None):
    if path is not None:
        return [Path(path).expanduser().resolve()]
    candidates = []
    if configured := os.environ.get("ZUCAI_PARAMS"):
        candidates.append(Path(configured))
    candidates.extend((Path.cwd() / "data/model/params.json", PARAMS_PATH))
    return list(dict.fromkeys(candidate.expanduser().resolve() for candidate in candidates))


def parameters_path(path=None):
    """Locate a config, keeping the final attempted path available for provenance."""
    candidates = _parameter_paths(path)
    return next((candidate for candidate in candidates if candidate.exists()), candidates[-1])


def _positive_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return value > 0 and math.isfinite(value)
    except OverflowError:
        return False


def _timestamp(value):
    if not isinstance(value, str):
        return False
    try:
        utc(value)
    except (ValueError, TypeError, OverflowError):
        return False
    return True


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _text_list(value):
    return (
        isinstance(value, list)
        and bool(value)
        and all(_text(item) for item in value)
        and len(set(value)) == len(value)
    )


def _validate_fields(league, values, validators, *, required=(), prefix=""):
    for field in required:
        if field not in values:
            raise ValueError(f"params.json 联赛 {league} 缺少字段 {prefix}{field}")
    for field, valid in validators.items():
        if field in values and not valid(values[field]):
            raise ValueError(f"params.json 联赛 {league} 字段 {prefix}{field} 类型或值无效")


def _validate_entry(league, entry):
    if not isinstance(entry, dict):
        raise ValueError(f"params.json 联赛 {league} 条目须为对象")
    required = ["half_life_days", "selection_as_of_utc"]
    if entry.get("p2_ready") is True:
        required.extend(
            (
                "generated_utc",
                "database_sha256",
                "training_policy",
                "sources",
                "unknown_team_prior",
                "draw_calibration",
                "p2_evidence",
            )
        )
    _validate_fields(
        league,
        entry,
        {
            "half_life_days": _positive_number,
            "selection_as_of_utc": _timestamp,
            "generated_utc": _timestamp,
            "database_sha256": _text,
            "training_policy": lambda value: isinstance(value, dict),
            "sources": lambda value: _text_list(value) and all("," not in s for s in value),
            "unknown_team_prior": _text,
            "draw_calibration": lambda value: isinstance(value, bool),
            "p2_ready": lambda value: isinstance(value, bool),
            "p2_evidence": lambda value: isinstance(value, dict),
            "evaluation_windows": lambda value: isinstance(value, list),
        },
        required=required,
    )
    if "training_policy" in entry:
        validators = {
            "history_scope": _text,
            "strict_availability": lambda value: isinstance(value, bool),
            "replay_known_after": lambda value: value is None or _text(value),
        }
        _validate_fields(
            league,
            entry["training_policy"],
            validators,
            required=validators,
            prefix="training_policy.",
        )
    if "p2_evidence" in entry:
        _validate_fields(
            league,
            entry["p2_evidence"],
            {"as_of_utc": _timestamp, "seasons": _text_list},
            required=("as_of_utc", "seasons") if entry.get("p2_ready") else (),
            prefix="p2_evidence.",
        )
    for index, window in enumerate(entry.get("evaluation_windows", [])):
        prefix = f"evaluation_windows[{index}]."
        if not isinstance(window, dict):
            raise ValueError(f"params.json 联赛 {league} 字段 {prefix[:-1]} 须为对象")
        _validate_fields(
            league,
            window,
            {
                "validation_season": _text,
                "selection_season": _text,
                "half_life_days": _positive_number,
                "selection_as_of_utc": _timestamp,
                "draw_calibration": lambda value: isinstance(value, bool),
            },
            required=("validation_season", "half_life_days", "selection_as_of_utc"),
            prefix=prefix,
        )


def read_parameters(path=None):
    candidates = _parameter_paths(path)
    path = parameters_path(path)
    if not path.exists():
        logger.warning(
            "参数文件不存在，回落默认90天；尝试路径：%s", ", ".join(map(str, candidates))
        )
        return {"schema_version": 1, "gate": GATE, "leagues": {}}, None
    raw = path.read_bytes()
    try:
        doc = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("params.json 格式无效") from exc
    if (
        not isinstance(doc, dict)
        or isinstance(doc.get("schema_version"), bool)
        or doc.get("schema_version") != 1
        or not isinstance(doc.get("leagues"), dict)
    ):
        raise ValueError("params.json 格式无效")
    for league, entry in doc["leagues"].items():
        _validate_entry(league, entry)
    return doc, hashlib.sha256(raw).hexdigest()


def resolve_parameters(league, *, as_of, params_file=None, half_life_days=None):
    doc, digest = read_parameters(params_file)
    entry = doc["leagues"].get(league, {})
    source, days = "default", 90.0
    usable = bool(entry) and utc(entry["selection_as_of_utc"]) <= utc(as_of)
    if usable:
        days, source = entry["half_life_days"], "params.json"
    if half_life_days is not None:
        days, source = half_life_days, "cli"
    if (
        isinstance(days, bool)
        or not isinstance(days, (float, int))
        or not math.isfinite(days)
        or days <= 0
    ):
        raise ValueError("半衰期必须是正数")
    return dict(
        half_life_days=float(days),
        source=source,
        params_path=str(parameters_path(params_file)),
        params_sha256=digest if usable else None,
        selection_as_of_utc=entry.get("selection_as_of_utc") if usable else None,
        draw_calibration=bool(entry.get("draw_calibration", False)) if usable else False,
    )


def write_parameters(league, entry, path=None):
    path = parameters_path(path)
    doc, _ = read_parameters(path)
    doc["gate"] = GATE
    doc["leagues"][league] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temp = Path(stream.name)
        stream.write(
            (json.dumps(doc, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
        )
    try:
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def p2_gate(report, *, league, as_of):
    seasons = latest_complete_seasons(as_of, league)
    rows = [report.get("seasons", {}).get(season) for season in seasons]
    reasons = []
    if report.get("fold") != "weekly":
        reasons.append("requires_weekly_folds")
    if any(not row or not row.get("complete") for row in rows):
        reasons.append("missing_complete_seasons")
    if any(
        row and (row.get("model", {}).get("n", 0) == 0 or row.get("closing", {}).get("n", 0) == 0)
        for row in rows
    ):
        reasons.append("empty_season_evidence")
    if any(row and any(row.get("skipped", {}).values()) for row in rows):
        reasons.append("skipped_evaluation_games")
    if any(row and row.get("unavailable_labels", 0) for row in rows):
        reasons.append("unavailable_evaluation_labels")
    if any(row and row.get("in_period", row["model"]["n"]) != row["model"]["n"] for row in rows):
        reasons.append("unreconciled_evaluation_population")
    totals = {}
    for name in ("model", "uniform", "closing", "closing_paired_model"):
        parts = [(r[name]["n"], r[name]["log_loss"]) for r in rows if r and name in r]
        n = sum(count for count, ll in parts if ll is not None)
        totals[name] = dict(
            n=n,
            log_loss=sum(count * ll for count, ll in parts if ll is not None) / n if n else None,
        )
    if totals["model"]["n"] < GATE["min_evaluated"]:
        reasons.append("evaluation_n_below_500")
    if (
        totals["closing"]["n"] < GATE["min_closing_paired"]
        or totals["closing"]["n"] != totals["closing_paired_model"]["n"]
    ):
        reasons.append("closing_paired_n_below_500_or_mismatch")
    gain = (
        totals["uniform"]["log_loss"] - totals["model"]["log_loss"]
        if all(totals[k]["log_loss"] is not None for k in ("uniform", "model"))
        else None
    )
    gap = (
        totals["closing_paired_model"]["log_loss"] - totals["closing"]["log_loss"]
        if all(totals[k]["log_loss"] is not None for k in ("closing_paired_model", "closing"))
        else None
    )
    if gain is None or not math.isfinite(gain) or gain < GATE["min_uniform_log_loss_gain"] - 1e-12:
        reasons.append("uniform_gain_below_0.05")
    if gap is None or not math.isfinite(gap) or gap > GATE["max_closing_log_loss_gap"] + 1e-12:
        reasons.append("closing_gap_above_0.05")
    return dict(
        p2_ready=not reasons,
        reasons=reasons,
        seasons=seasons,
        metrics=totals,
        uniform_log_loss_gain=gain,
        closing_log_loss_gap=gap,
        as_of_utc=utc(as_of).isoformat(),
        bet_allowed=False,
    )


def model_readiness(model, *, as_of, params_file=None):
    """A new config cannot silently retune or certify an incompatible fitted artifact."""
    from zucai.predict.base import model_sources

    doc, digest = read_parameters(params_file)
    entry = doc["leagues"].get(model.league, {})
    evidence = entry.get("p2_evidence", {})
    ready = bool(entry.get("p2_ready")) and bool(evidence.get("as_of_utc"))
    if ready:
        ready = (
            utc(evidence["as_of_utc"]) <= utc(as_of)
            and utc(entry["generated_utc"]) <= utc(as_of)
            and utc(entry["selection_as_of_utc"]) <= utc(model.as_of)
            and evidence.get("seasons") == latest_complete_seasons(as_of, model.league)
            and bool(entry.get("database_sha256"))
            and entry.get("training_policy") == TRAINING_POLICY
            and getattr(model, "training_policy", None) == TRAINING_POLICY
            and getattr(model, "availability_basis", "unknown") in ("strict", "mixed")
            and getattr(model, "replayed_training_share", 0) == 0
            and model.half_life_days == entry["half_life_days"]
            and model_sources(model) == sorted(entry.get("sources", []))
            and getattr(model, "unknown_team_prior", False)
            and getattr(model, "prior_parameters", {}).get("method")
            == entry.get("unknown_team_prior")
            and getattr(model, "draw_calibration", False) == entry.get("draw_calibration", False)
        )
    return dict(
        p2_ready=bool(ready),
        p2_label="P2 可用" if ready else "P2 实验",
        params_sha256=digest,
        params_path=str(parameters_path(params_file)),
        half_life_days=model.half_life_days,
        parameter_source=getattr(model, "parameter_source", "legacy_or_explicit"),
        training_database_sha256=getattr(model, "training_database_sha256", None)
        or getattr(model, "database_sha256", None),
        evaluation_database_sha256=entry.get("database_sha256"),
        training_policy=getattr(model, "training_policy", None),
        p2_evidence=evidence,
        bet_allowed=False,
    )
