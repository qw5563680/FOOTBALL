"""Isolated experiment restore, computation and immutable Release evidence."""

import logging
import shutil
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy.orm import Session

from zucai.db import get_engine
from zucai.forecast_archive import attach_experiments
from zucai.predict.experiments.global_rating_contract import (
    RATING_RUN_KEY,
    RATING_VERSION,
    is_rating_target,
)
from zucai.predict.experiments.parallel import (
    AVAILABILITY_MODES,
    compute,
    raw_json,
    sha,
    unavailable,
)
from zucai.scoreboard.score import instant
from zucai.sources.sporttery import BEIJING
from zucai.stats_release import ReleaseStore, upload_checksums, upload_verified
from zucai.stats_rows import restore_rows

LOG = logging.getLogger(__name__)
TARGET_FIELDS = ("match_id", "source", "league", "home", "away", "kickoff_utc")


def _targets(batches):
    return [
        {key: row[key] for key in TARGET_FIELDS}
        for batch in batches
        for row in batch["predictions"]
    ]


def _rating_targets(batches, run):
    if run.get(RATING_RUN_KEY) != RATING_VERSION:
        return []
    targets = []
    for batch in batches:
        if batch["mode"] != "prematch":
            continue
        model_cutoffs = {
            model["model_version"]: model.get("model_as_of_utc") for model in batch["models"]
        }
        for row in batch["predictions"]:
            if is_rating_target(row):
                targets.append(
                    {
                        **{key: row[key] for key in TARGET_FIELDS},
                        "formal_model_version": row["model_version"],
                        "formal_model_cutoff_utc": model_cutoffs.get(row["model_version"]),
                        "target_cutoff_utc": batch["as_of_utc"],
                    }
                )
    return targets


def _rating_rows(database, targets, cutoff):
    if not targets:
        return [], []
    from zucai.predict.experiments.global_rating_runtime import fit_and_forecast

    try:
        updates, fit = fit_and_forecast(database, targets, cutoff=cutoff)
    except Exception as exc:
        LOG.exception("跨联赛评分计算失败；原有并行实验继续冻结")
        reason = "rating_fit_failed:" + type(exc).__name__
        return unavailable([], cutoff, reason, rating_targets=targets), []
    rows = unavailable([], cutoff, "missing_team_rating", rating_targets=targets)
    return [dict(row, **updates[row["match_id"]]) for row in rows], [fit]


def _documents(batches, rows, cutoff, generated):
    documents = {}
    for batch in batches:
        day = batch["input"]["kickoff_date_beijing"]
        ids = {r["match_id"] for r in batch["predictions"]}
        documents[day] = dict(
            schema_version=1,
            batch_id=batch["batch_id"],
            generated_utc=generated,
            as_of_utc=cutoff,
            mode="prematch",
            validation_status="experimental_unadopted",
            p2_ready=False,
            availability_mode="strict",
            **AVAILABILITY_MODES,
            predictions=[r for r in rows if r["match_id"] in ids],
        )
    return documents


def freeze_unavailable(archive, batches, run, *, reason, clock):
    """Attach the normal experiment rows with an explicit absence and no probabilities."""
    cutoff = run["training_cutoff_utc"]
    rows = unavailable(
        _targets(batches), cutoff, reason, rating_targets=_rating_targets(batches, run)
    )
    docs = _documents(batches, rows, cutoff, clock().isoformat())
    return attach_experiments(Path(archive), docs, run["run_id"], clock=clock)


def freeze_parallel(
    archive,
    database,
    feed,
    batches,
    run,
    *,
    clock=None,
    release_factory=ReleaseStore,
    on_restored=None,
):
    """Restore and fit in isolation after the production forecast has been frozen."""
    clock = clock or (lambda: datetime.now(UTC))
    cutoff = run["training_cutoff_utc"]
    targets = _targets(batches)
    rating_targets = _rating_targets(batches, run)
    release, receipt, rows, fits, error = None, None, None, [], None
    with TemporaryDirectory(prefix="zucai-experiments-") as temporary:
        root = Path(temporary)
        try:
            working = root / "data/experiment.db"
            working.parent.mkdir()
            shutil.copyfile(database, working)
            # Restore into a separate DB: production training DB/hash is never changed.
            if feed.get("technical_data", {}).get("version") in (2, 3):
                release = release_factory()
                restore_rows(working, archive, release=release)
            if on_restored is not None:
                try:
                    on_restored(working)
                except Exception:
                    LOG.exception("纸面复式伤停证据读取失败；实验计算继续，缺口保持未知")
            engine = get_engine(working)
            try:
                with engine.connect() as connection:
                    connection.exec_driver_sql("PRAGMA query_only=ON")
                    with Session(bind=connection) as session:
                        rows, fits = compute(session, targets, cutoff=cutoff)
            finally:
                engine.dispose()
            rating_rows, rating_fits = _rating_rows(working, rating_targets, cutoff)
            rows.extend(rating_rows)
            fits.extend(rating_fits)
        except Exception as exc:
            LOG.exception("实验数据恢复或计算失败；正式预测已保留")
            error = "experiment_failed:" + type(exc).__name__
            rows = unavailable(targets, cutoff, error, rating_targets=rating_targets)

        generated = clock().isoformat()
        for row in rows:
            if instant(row["kickoff_utc"]) <= instant(generated):
                row.update(
                    status="missing",
                    model_probabilities=None,
                    top_score=None,
                    fallback_reason="already_started",
                )
        docs = _documents(batches, rows, cutoff, generated)
        try:
            if error is None:
                release = release or release_factory()
                tag = "experiments-" + instant(generated).astimezone(BEIJING).strftime("%Y%m")
                # Keep the explicit daily freeze in forecasts; the complete fit/feature
                # archive goes to Release, never experiments/ on the data branch.
                contents = {f"{day}/experimental.json": raw_json(doc) for day, doc in docs.items()}
                contents["training.json"] = raw_json(fits)
                identity = sha({key: value.decode() for key, value in contents.items()})
                bundle = root / f"parallel-{run['run_id']}-{identity[:16]}.zip"
                with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as output:
                    for name, raw in sorted(contents.items()):
                        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                        info.compress_type = zipfile.ZIP_DEFLATED
                        output.writestr(info, raw)
                asset = upload_verified(release, tag, bundle)
                checksums = upload_checksums(
                    release, tag, root, run["run_id"] + "-" + identity[:16], [asset]
                )
                receipt = dict(asset=asset, checksum_asset=checksums)
        except Exception as exc:
            LOG.exception("实验 Release 上传失败；正式预测已保留")
            error = "experiment_archive_failed:" + type(exc).__name__
            for doc in docs.values():
                for row in doc["predictions"]:
                    row.update(
                        status="missing",
                        model_probabilities=None,
                        top_score=None,
                        fallback_reason=error,
                        features=None,
                        coefficients_sha256=None,
                    )
        finally:
            if release is not None and hasattr(release, "session"):
                release.session.close()
        # Publication time is separate from immutable bytes inside the Release.
        # Consumers exclude late publication from prospective scoring/coverage.
        return attach_experiments(Path(archive), docs, run["run_id"], release=receipt, clock=clock)
