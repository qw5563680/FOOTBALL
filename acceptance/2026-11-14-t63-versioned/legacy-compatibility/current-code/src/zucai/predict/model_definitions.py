"""Stable method identities, separate from daily data and fitted-artifact versions."""

import copy
import hashlib
import json
from collections.abc import Mapping

from zucai.predict.base import model_sources

LEGACY_LEAGUES = frozenset({"英超", "西甲", "意甲", "德甲", "法甲", "葡超", "巴甲"})
EXPERIMENTAL_LEAGUES = frozenset({"英冠", "法乙", "荷甲", "日职", "瑞超", "挪超", "芬超", "美职"})
T63_LEAGUES = LEGACY_LEAGUES | EXPERIMENTAL_LEAGUES
DEFINITION_FIELDS = ("model_definition_id", "model_definition", "source_variant")
FROZEN_DEFINITION_FIELDS = (*DEFINITION_FIELDS, "paper_eligible")
_FIELDS = {
    "league",
    "model_family",
    "training_sources",
    "fitting_method",
    "prior_method",
    "calibration_method",
    "selection_protocol",
}
_PROTOCOL_FIELDS = {
    "selection_basis",
    "calibration_policy",
    "selection_method",
    "evaluation_method",
}
_LEGACY_SELECTION_BASIS = (
    "minimum season-S walk-forward model log loss; complete equal samples; tie: shorter half-life"
)
_LEGACY_CALIBRATION_POLICY = (
    "enable in S+1 only if selected uncalibrated monthly S draw bias exceeds 3pp"
)


def _get(model, key, default=None):
    return model.get(key, default) if isinstance(model, Mapping) else getattr(model, key, default)


def _text(value, fallback="unknown"):
    return value if isinstance(value, str) and value.strip() else fallback


def source_variant(league, sources):
    sources = set(sources)
    if league in LEGACY_LEAGUES and sources == {"fd", "jc"}:
        return "legacy"
    if league in T63_LEAGUES and sources & {"apif", "fdorg"}:
        return "expanded"
    return "current"


def _digest(definition):
    return hashlib.sha256(
        json.dumps(
            definition, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def validate_definition_metadata(metadata):
    """Validate a standalone frozen definition without loading data or current config."""
    if any(key not in metadata for key in DEFINITION_FIELDS):
        raise ValueError("模型定义标记不完整")
    definition = metadata["model_definition"]
    if not isinstance(definition, dict) or set(definition) != _FIELDS:
        raise ValueError("模型定义字段无效")
    if any(
        not isinstance(definition[key], str) or not definition[key].strip()
        for key in _FIELDS - {"training_sources", "selection_protocol"}
    ):
        raise ValueError("模型定义方法与联赛必须明确")
    sources = model_sources({"sources": definition["training_sources"]})
    if sources != definition["training_sources"]:
        raise ValueError("模型定义来源必须排序且唯一")
    protocol = definition["selection_protocol"]
    if (
        not isinstance(protocol, dict)
        or set(protocol) != _PROTOCOL_FIELDS
        or any(not isinstance(value, str) or not value.strip() for value in protocol.values())
    ):
        raise ValueError("模型定义选参协议无效")
    if metadata["model_definition_id"] != _digest(definition):
        raise ValueError("模型定义 ID 与定义内容不符")
    if metadata["source_variant"] != source_variant(definition["league"], sources):
        raise ValueError("模型定义来源版本不符")


def _metadata(definition):
    result = dict(
        model_definition_id=_digest(definition),
        model_definition=definition,
        source_variant=source_variant(definition["league"], definition["training_sources"]),
    )
    validate_definition_metadata(result)
    return result


def _protocol(entry):
    basis = _text(entry.get("selection_basis"))
    return dict(
        selection_basis=basis,
        calibration_policy=_text(entry.get("calibration_policy")),
        selection_method=_text(
            entry.get("selection_method"),
            "monthly_walk_forward_season_S" if basis != "unknown" else "unknown",
        ),
        evaluation_method=_text(
            entry.get("evaluation_method"),
            "weekly_walk_forward_season_S_plus_1" if basis != "unknown" else "unknown",
        ),
    )


def legacy_definition_metadata(league):
    """The fixed b83 legacy methods; this is not an approval or promotion registry."""
    if league not in LEGACY_LEAGUES:
        raise ValueError("该联赛没有 T63 旧规基线")
    return _metadata(
        dict(
            league=league,
            model_family="dixon-coles",
            training_sources=["fd", "jc"],
            fitting_method="penaltyblog_slsqp",
            prior_method="weakest_quartile_half_shrinkage",
            calibration_method="draw_log_odds_intercept",
            selection_protocol=dict(
                selection_basis=_LEGACY_SELECTION_BASIS,
                calibration_policy=_LEGACY_CALIBRATION_POLICY,
                selection_method="monthly_walk_forward_season_S",
                evaluation_method="weekly_walk_forward_season_S_plus_1",
            ),
        )
    )


def legacy_definition_compatible(metadata):
    league = metadata["model_definition"]["league"]
    return (
        league in LEGACY_LEAGUES
        and metadata["model_definition_id"]
        == (legacy_definition_metadata(league)["model_definition_id"])
    )


def entry_definition_metadata(league, entry):
    """Describe the intended method even when no fitted artifact could be produced."""
    family = _text(entry.get("model_family"), "dixon-coles")
    return _metadata(
        dict(
            league=league,
            model_family=family,
            training_sources=model_sources({"sources": entry.get("sources")}),
            fitting_method=_text(
                entry.get("fitting_method"),
                "penaltyblog_slsqp" if family == "dixon-coles" else "unknown",
            ),
            prior_method=_text(entry.get("prior_method"), _text(entry.get("unknown_team_prior"))),
            calibration_method=_text(
                entry.get("calibration_method"),
                "draw_log_odds_intercept" if entry.get("calibration_policy") else "unknown",
            ),
            selection_protocol=_protocol(entry),
        )
    )


def definition_metadata(model, entry):
    """Bind method, corpus and selection policy; never hash selected annual values."""
    league, sources = _get(model, "league"), model_sources(model)
    family = _text(_get(model, "model_family"), _text(_get(model, "model_name"), "dixon-coles"))
    prior = _get(model, "prior_parameters", {}) or {}
    prior_method = (
        "none"
        if _get(model, "unknown_team_prior", True) is False
        else _text(
            _get(model, "prior_method"),
            _text(prior.get("method"), _text(entry.get("unknown_team_prior"))),
        )
    )
    provenance = _get(model, "parameter_provenance", {}) or {}
    manual = _get(model, "parameter_source") == "cli" or provenance.get("source") == "cli"
    if any(_get(model, key) is not None for key in DEFINITION_FIELDS):
        stored = {key: _get(model, key) for key in DEFINITION_FIELDS}
        validate_definition_metadata(stored)
        definition = stored["model_definition"]
        if (
            definition["league"] != league
            or definition["training_sources"] != sources
            or definition["model_family"] != family
            or definition["prior_method"] != prior_method
            or (
                manual and definition["selection_protocol"]["selection_method"] != "manual_override"
            )
            or any(
                _get(model, key) is not None and _get(model, key) != definition[key]
                for key in ("fitting_method", "calibration_method")
            )
        ):
            raise ValueError("已存模型定义与模型实际方法或训练来源不符")
        return copy.deepcopy(stored)
    policy = dict(entry, sources=sources, model_family=family, prior_method=prior_method)
    if manual:
        policy.update(
            selection_basis="explicit_manual_half_life",
            selection_method="manual_override",
            evaluation_method="unknown",
        )
    for key in ("fitting_method", "calibration_method"):
        if _get(model, key) is not None:
            policy[key] = _get(model, key)
    return entry_definition_metadata(league, policy)
