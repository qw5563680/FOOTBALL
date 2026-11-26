"""A separate experimental probability derived from the same frozen TEAM and T71 inputs."""

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from zucai.market_reference import MARKET_OPERATOR, MARKET_REFERENCE_VERSION, MARKET_SUPPLIER

LOG = logging.getLogger(__name__)
MODEL_NAME = "team-market-blend-v1"
METHOD_VERSION = "log-linear-v1"
PROTOCOL_PATH = Path(__file__).resolve().parents[2] / "data/model/team-market-blend.json"
OUTCOMES = "hda"
COMMON = {
    "model_name": MODEL_NAME,
    "method_version": METHOD_VERSION,
    "market_dependent": True,
    "paper_eligible": False,
}
SHA256 = re.compile(r"[0-9a-f]{64}")
GIT_COMMIT = re.compile(r"[0-9a-f]{40}")
VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
WEIGHT_REVIEW_STEP = 200


@dataclass(frozen=True)
class Weight:
    version: str
    value: float
    effective_from: datetime
    protocol_sha256: str
    checkpoint: int = 0
    previous: "Weight | None" = None


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _probabilities(values):
    return (
        isinstance(values, dict)
        and set(values) == set(OUTCOMES)
        and all(_finite(p) and 0 <= p <= 1 for p in values.values())
        and math.isclose(math.fsum(values.values()), 1, rel_tol=0, abs_tol=1e-6)
    )


def _weight(value):
    return _finite(value) and 0 <= value <= 1


def _clock(value):
    try:
        stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
        if isinstance(stamp, datetime) and stamp.utcoffset() is not None:
            return stamp
    except (ValueError, OverflowError):
        pass
    raise ValueError("融合证据时间必须带时区")


def blend_probabilities(team: dict, market: dict, weight: float) -> dict:
    """Normalize a weighted geometric mean with exact endpoints and zero support."""
    if not _probabilities(team) or not _probabilities(market) or not _weight(weight):
        raise ValueError("融合需要两组完整概率和 [0,1] 内的权重")
    if weight == 0:
        return dict(team)
    if weight == 1:
        return dict(market)
    logs = {
        key: (1 - weight) * math.log(team[key]) + weight * math.log(market[key])
        for key in OUTCOMES
        if team[key] > 0 and market[key] > 0
    }
    if not logs:
        raise ValueError("两组概率没有共同支撑")
    peak = max(logs.values())
    numerators = {key: math.exp(logs[key] - peak) if key in logs else 0.0 for key in OUTCOMES}
    total = math.fsum(numerators.values())
    return {key: value / total for key, value in numerators.items()}


def _matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _read_weight(path):
    return _parse_weight(json.loads(Path(path).read_bytes()))


def _parse_weight(data):
    if (
        not isinstance(data, dict)
        or type(data.get("schema_version")) is not int
        or data["schema_version"] != 1
        or data.get("model_name") != MODEL_NAME
        or not _matches(VERSION, data.get("weight_version"))
        or not _weight(data.get("market_weight"))
    ):
        raise ValueError("融合权重协议格式不合法")
    evidence = data.get("retrospective")
    if (
        not isinstance(evidence, dict)
        or not _matches(GIT_COMMIT, evidence.get("archive_commit"))
        or any(
            not _matches(SHA256, evidence.get(key)) for key in ("protocol_sha256", "report_sha256")
        )
    ):
        raise ValueError("融合权重协议缺少回溯证据")
    effective = _clock(data.get("effective_from_utc"))
    if not (
        _clock(evidence.get("training_end_utc"))
        < _clock(evidence.get("validation_end_utc"))
        < _clock(evidence.get("holdout_end_utc"))
        <= effective
    ):
        raise ValueError("融合回溯切分或生效时间不合法")
    previous = None
    checkpoint = 0
    if "prospective" in data or "previous" in data:
        earlier = data.get("previous")
        previous = _parse_weight(earlier)
        proof = data.get("prospective")
        version = previous
        while version is not None:
            if version.version == data["weight_version"]:
                raise ValueError("融合权重版本不得重复")
            version = version.previous
        if (
            not isinstance(proof, dict)
            or type(proof.get("checkpoint")) is not int
            or proof["checkpoint"] != previous.checkpoint + WEIGHT_REVIEW_STEP
            or proof.get("previous_weight_version") != previous.version
            or earlier["retrospective"] != evidence
            or not previous.effective_from <= _clock(proof.get("as_of_utc")) < effective
            or any(
                not _matches(SHA256, proof.get(key)) for key in ("score_sha256", "report_sha256")
            )
        ):
            raise ValueError("融合前瞻复估证据不合法")
        checkpoint = proof["checkpoint"]
    # The same version has the same evidence hash inside a later configuration.
    version_proof = {key: value for key, value in data.items() if key != "previous"}
    evidence_hash = hashlib.sha256(
        json.dumps(version_proof, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return Weight(
        data["weight_version"],
        float(data["market_weight"]),
        effective,
        evidence_hash,
        checkpoint,
        previous,
    )


def _jc_scope(row):
    match_id = row.get("match_id")
    return (
        row.get("sfc_only") is not True
        and isinstance(match_id, str)
        and match_id.startswith("jc:")
        and row.get("source") in (None, "jc")
    )


def eligible_market(row, as_of):
    """Return an intact T71 snapshot at the prescribed cutoff, or no market."""
    if row.get("source") != "jc" or not _jc_scope(row):
        return None
    field = "market" if row.get("status") == "predicted" else "market_reference"
    market = row.get(field)
    if not isinstance(market, dict):
        return None
    if (
        market.get("source") != "stored_had_snapshot"
        or market.get("type") != "P3"
        or market.get("execution_verified") is not False
        or market.get("method_version") != MARKET_REFERENCE_VERSION
        or market.get("operator") != MARKET_OPERATOR
        or market.get("supplier") != MARKET_SUPPLIER
    ):
        return None
    try:
        snapshot = _clock(market.get("snapshot_utc"))
        collected = _clock(market.get("collected_utc"))
        if (
            market.get("snapshot_id") != market.get("snapshot_utc")
            or snapshot != collected
            or collected > _clock(as_of)
            or collected >= _clock(row.get("kickoff_utc"))
            or _clock(as_of) >= _clock(row.get("kickoff_utc"))
        ):
            return None
    except ValueError:
        return None
    prices, probs = market.get("prices"), market.get("probabilities")
    if (
        not isinstance(prices, dict)
        or set(prices) != set(OUTCOMES)
        or not all(_finite(p) and p > 1 for p in prices.values())
        or not _probabilities(probs)
    ):
        return None
    inverses = {key: 1 / price for key, price in prices.items()}
    total = math.fsum(inverses.values())
    if not all(
        math.isclose(probs[key], inverses[key] / total, rel_tol=0, abs_tol=1e-12)
        for key in OUTCOMES
    ):
        return None
    return market


def _team(row):
    values = row.get("model_probabilities")
    if (
        row.get("status") == "predicted"
        and row.get("model_name") == "dixon-coles"
        and _probabilities(values)
    ):
        return values
    return None


def _missing(as_of, reason):
    return {**COMMON, "as_of_utc": as_of, "status": "not_generated", "reason": reason}


def _freeze_row(row, as_of, weight, reason):
    team = _team(row)
    if team is None:
        return _missing(as_of, "球队模型未生成")
    market = eligible_market(row, as_of)
    if market is None:
        missing = row.get("market_missing_reason")
        return _missing(
            as_of,
            missing if isinstance(missing, str) and missing.strip() else "无合格的同截点市场快照",
        )
    if weight is None:
        return _missing(as_of, reason)
    return {
        **COMMON,
        "as_of_utc": as_of,
        "status": "generated",
        "reason": None,
        "model_version": weight.version,
        "market_weight": weight.value,
        "protocol_sha256": weight.protocol_sha256,
        "market_snapshot_id": market["snapshot_id"],
        "market_collected_utc": market["collected_utc"],
        "probabilities": blend_probabilities(team, market["probabilities"], weight.value),
    }


def prepare_blends(as_of, *, archive=None, protocol_path=None):
    """Read one committed version and its prior score checkpoint for a whole run."""
    try:
        weight = _read_weight(PROTOCOL_PATH if protocol_path is None else protocol_path)
    except FileNotFoundError:
        return None, "融合权重尚未登记"
    except Exception:
        LOG.exception("融合权重协议读取失败")
        return None, "融合权重协议不合法或无法读取"
    cutoff = _clock(as_of)
    while weight is not None and weight.effective_from > cutoff:
        weight = weight.previous
    if weight is None:
        return None, "该版融合权重尚未生效"
    if archive is not None:
        try:
            from zucai.scoreboard.team_market_blend import read_stop

            stopped = read_stop(archive, as_of, weight.version)
        except Exception:
            LOG.exception("融合关停记录校验失败")
            return None, "融合关停记录无法核实"
        if stopped:
            return None, "融合实验达到预定关停门槛"
    return weight, None


def freeze_blends(batch, *, archive=None, protocol_path=None, deployment=None):
    """Annotate only the optional fusion field after ordinary TEAM prediction has finished."""
    if batch["mode"] != "prematch" or batch["input"].get("market_weight", 0) != 0:
        return
    as_of = batch["as_of_utc"]
    if deployment is None:
        deployment = prepare_blends(as_of, archive=archive, protocol_path=protocol_path)
    weight, reason = deployment
    for row in batch["predictions"]:
        if not _jc_scope(row):
            continue
        try:
            blend = _freeze_row(row, as_of, weight, reason)
            validate_frozen_blend({**row, "experimental_blend": blend}, as_of)
        except Exception:
            LOG.exception("比赛 %s 融合计算失败", row["match_id"])
            blend = _missing(as_of, "融合计算异常")
        row["experimental_blend"] = blend


def validate_frozen_blend(row, as_of):
    """Verify a typed T72 result using only inputs in its own frozen row."""
    blend = row.get("experimental_blend")
    if not isinstance(blend, dict) or blend.get("model_name") != MODEL_NAME:
        return
    if (
        not _jc_scope(row)
        or blend.get("method_version") != METHOD_VERSION
        or blend.get("paper_eligible") is not False
        or blend.get("market_dependent") is not True
        or _clock(blend.get("as_of_utc")) != _clock(as_of)
    ):
        raise ValueError("融合记录的范围、截点或实验资格不合法")
    common_keys = {*COMMON, "as_of_utc", "status", "reason"}
    if blend.get("status") == "not_generated":
        if (
            set(blend) != common_keys
            or not isinstance(blend.get("reason"), str)
            or not blend["reason"].strip()
        ):
            raise ValueError("未生成的融合记录必须给出原因")
        return
    if (
        set(blend)
        != common_keys
        | {
            "model_version",
            "market_weight",
            "protocol_sha256",
            "market_snapshot_id",
            "market_collected_utc",
            "probabilities",
        }
        or blend.get("status") != "generated"
        or blend.get("reason") is not None
        or not _matches(VERSION, blend.get("model_version"))
        or not _matches(SHA256, blend.get("protocol_sha256"))
    ):
        raise ValueError("融合记录的协议和字段不完整")
    market = eligible_market(row, as_of)
    if market is None or (
        blend["market_snapshot_id"] != market["snapshot_id"]
        or blend["market_collected_utc"] != market["collected_utc"]
    ):
        raise ValueError("融合记录与冻结市场快照不符")
    expected = blend_probabilities(_team(row), market["probabilities"], blend["market_weight"])
    values = blend["probabilities"]
    if not _probabilities(values) or any(
        not math.isclose(values[key], expected[key], rel_tol=0, abs_tol=1e-12) for key in OUTCOMES
    ):
        raise ValueError("融合概率不能由同批冻结输入复算")
