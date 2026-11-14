"""Parallel result-source candidates, outside the existing three experiment lines."""

import copy
import json
import logging
from datetime import timedelta

from zucai.cloud_feeds import digest
from zucai.predict.base import utc
from zucai.predict.dixon_coles import DixonColes
from zucai.predict.model_definitions import LEGACY_LEAGUES, entry_definition_metadata
from zucai.predict.training_sources import apif_history_sources
from zucai.report.batch import predict_batch

LOG = logging.getLogger(__name__)


def candidate_parameters(config):
    """Keep selected numeric policy; a new source definition inherits no readiness."""
    document = copy.deepcopy(config)
    document["leagues"] = {
        league: entry
        for league, entry in document["leagues"].items()
        if league in LEGACY_LEAGUES and set(entry.get("sources", [])) == {"fd", "jc"}
    }
    policy = apif_history_sources()
    for league, entry in document["leagues"].items():
        entry["sources"] = sorted(policy[league])
        entry["p2_ready"] = False
        entry["p2_evidence"] = dict(p2_ready=False, reasons=["new_source_definition_unapproved"])
        entry.pop("database_sha256", None)
    return document


def _write(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )


def train_candidates(database, stage, run_id, cutoff, config, *, trainer):
    """Called after the primary forecast is safely frozen; retain its fit cutoff."""
    document = candidate_parameters(config)
    if not document["leagues"]:
        return {}, {}, None
    params_path = stage / f"models/{run_id}/source-expanded.params.json"
    params_path.parent.mkdir(parents=True, exist_ok=True)
    _write(params_path, document)
    outcomes, fitted = {}, {}
    for league, entry in document["leagues"].items():
        model_path = f"models/{run_id}/{league}.source-expanded.pkl"
        train_path = f"models/{run_id}/{league}.source-expanded.train.json"
        result = dict(
            **entry_definition_metadata(league, entry),
            training_json_path=train_path,
            training_cutoff_utc=cutoff.isoformat(),
            training_database_sha256=digest(database),
            sources=sorted(entry["sources"]),
            half_life_days=entry["half_life_days"],
            parameter_source="params.json",
            predicted=0,
            p2_ready=False,
            p2_label="实验",
            paper_eligible=False,
        )
        try:
            training = trainer(
                db=database,
                league=league,
                source=",".join(entry["sources"]),
                since=cutoff - timedelta(days=56),
                as_of=cutoff,
                output=stage / model_path,
                params_file=params_path,
            )
            model = DixonColes.load(stage / model_path)
            if model.model_definition_id != result["model_definition_id"]:
                raise ValueError("候选拟合定义与预先声明的版本不符")
            fitted[league] = stage / model_path
            result.update(
                status="trained",
                model_path=model_path,
                sha256=digest(stage / model_path),
                training_rows=training["training_rows"],
            )
            training.update(
                model_path=model_path,
                p2_ready=False,
                p2_label="实验",
                paper_eligible=False,
                smoke_backtest_is_p2_evidence=False,
            )
            _write(stage / train_path, training)
        except Exception as exc:
            LOG.exception("%s 新来源候选失败，已冻结旧版本保持原样", league)
            fitted.pop(league, None)
            (stage / model_path).unlink(missing_ok=True)
            result.pop("model_path", None)
            result.pop("sha256", None)
            result.update(status="failed", reason=f"{type(exc).__name__}: {exc}")
            _write(stage / train_path, result)
        outcomes[league] = result
    return outcomes, fitted, params_path


def predict_candidates(session, *, primary, outcomes, fitted, params_path, clock):
    """Use the primary target IDs and cutoff; late generation is an explicit exclusion."""
    originals = [row for row in primary["predictions"] if row.get("league") in outcomes]
    cutoff = utc(primary["as_of_utc"])
    batch = predict_batch(
        session,
        model_files=list(fitted.values()),
        as_of=cutoff,
        generated=cutoff,
        match_ids=[row["match_id"] for row in originals],
        params_file=params_path,
        market_weight=0,
        persist=False,
        allow_empty=True,
    )
    batch.update(
        batch_id=primary["batch_id"],
        generated_utc=clock().isoformat(),
        validation_status="experimental_unadopted",
        candidate_kind="source_change",
        source_variant="expanded",
        p2_ready=False,
        p2_label="实验",
        paper_eligible=False,
        model_definition_schema=1,
    )
    batch["input"]["kickoff_date_beijing"] = primary["input"]["kickoff_date_beijing"]
    if "scope" in primary["input"]:
        batch["input"]["scope"] = copy.deepcopy(primary["input"]["scope"])
    batch["input"]["source_params_path"] = f"models/{params_path.parent.name}/{params_path.name}"
    batch["input"]["source_params_sha256"] = digest(params_path)
    original_by_id = {row["match_id"]: row for row in originals}
    for row in batch["predictions"]:
        original = original_by_id[row["match_id"]]
        if "sfc_only" in original:
            row["sfc_only"] = original["sfc_only"]
        outcome = outcomes[row["league"]]
        for key in ("model_definition_id", "model_definition", "source_variant"):
            row[key] = copy.deepcopy(outcome[key])
        row.update(p2_ready=False, p2_label="实验", paper_eligible=False)
        if outcome["status"] == "failed":
            row.update(
                status="excluded",
                reason="新来源候选训练失败：" + outcome["reason"],
                expected_definition=True,
                model_version=None,
            )
        else:
            row.update(model_name="dixon-coles", model_version=outcome["sha256"])
            row.pop("expected_definition", None)
            if row["status"] == "predicted" and utc(row["kickoff_utc"]) <= utc(
                batch["generated_utc"]
            ):
                row.update(status="excluded", reason="候选生成时已开赛")
                for key in (
                    "model_probabilities",
                    "score_matrix",
                    "metadata",
                    "top_scores",
                    "market",
                    "ev",
                    "experimental_blend",
                ):
                    row.pop(key, None)
        if row["status"] == "predicted":
            outcome["predicted"] += 1
    return batch
