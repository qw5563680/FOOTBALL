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
    """Production is already published; even restore/fit/upload errors cannot undo it."""
    clock = clock or (lambda: datetime.now(UTC))
    cutoff = run["training_cutoff_utc"]
    targets = [
        {key: row[key] for key in ("match_id", "source", "league", "home", "away", "kickoff_utc")}
        for batch in batches
        for row in batch["predictions"]
    ]
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
        except Exception as exc:
            LOG.exception("实验数据恢复或计算失败；正式预测已保留")
            error = "experiment_failed:" + type(exc).__name__
            rows = unavailable(targets, cutoff, error)

        generated = clock().isoformat()
        for row in rows:
            if instant(row["kickoff_utc"]) <= instant(generated):
                row.update(
                    status="missing",
                    model_probabilities=None,
                    top_score=None,
                    fallback_reason="already_started",
                )
        docs = {}
        for batch in batches:
            day = batch["input"]["kickoff_date_beijing"]
            ids = {r["match_id"] for r in batch["predictions"]}
            docs[day] = dict(
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
