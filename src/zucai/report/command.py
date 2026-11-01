"""CLI transaction boundary for trusted models and immutable report artifacts."""

import csv
import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import event, select

from zucai.db import get_engine, init_db, session_scope
from zucai.models import Prediction
from zucai.predict.base import utc
from zucai.report.render import load_batch, write_report
from zucai.sources.sporttery import BEIJING

LOG = logging.getLogger(__name__)


def _csv_ids(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        allowed = {"match_id", "home", "away", "league", "source", "kickoff_utc"}
        fields = reader.fieldnames
        if (
            not fields
            or "match_id" not in fields
            or len(set(fields)) != len(fields)
            or not set(fields) <= allowed
        ):
            raise ValueError(
                "比赛CSV须有 match_id；可附 home/away/league/source/kickoff_utc 核对身份"
            )
        keys, expected = [], {}
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError("比赛CSV行列数量不一致")
            key = row.pop("match_id").strip()
            keys.append(key)
            expected[key] = {field: value.strip() for field, value in row.items() if value.strip()}
    if not keys or not all(keys):
        raise ValueError("比赛CSV含空ID或没有数据")
    return keys, expected


def _deadline(batch, instant):
    if batch["mode"] != "prematch":
        return
    if any(
        row["status"] == "predicted" and utc(row["kickoff_utc"]) <= utc(instant)
        for row in batch["predictions"]
    ):
        raise ValueError("运行期间有比赛已开赛，本批未冻结，请重新选择未开赛比赛")


def run_predict(args):
    from zucai.report.batch import predict_batch

    created = datetime.now(UTC)
    if bool(args.as_of) != bool(args.replay):
        raise ValueError("历史回放必须同时指定 --replay 和带时区的 --as-of")
    cutoff = utc(args.as_of) if args.as_of else utc(created)
    keys, expected = _csv_ids(args.matches) if args.matches else (args.match_id, {})
    day = args.date if args.date else (None if keys else cutoff.tz_convert(BEIJING).date())
    output = (
        Path(args.out)
        if args.out
        else Path("reports") / (created.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    )
    if output.exists():
        raise ValueError("报告目录已存在，请使用新目录保留旧版")
    engine = get_engine(args.db)
    made = False
    try:
        init_db(engine)
        with session_scope(engine) as session:
            batch = predict_batch(
                session,
                model_files=[Path(p) for p in (args.model_file or ["models/dixon-coles.pkl"])],
                as_of=cutoff,
                generated=created,
                replay=args.replay,
                day=day,
                match_ids=keys,
                market_weight=args.market_weight,
                expected_identities=expected,
            )
            completed = datetime.now(UTC)
            _deadline(batch, completed)
            batch["generated_utc"] = completed.isoformat()
            for prediction in session.scalars(
                select(Prediction).where(Prediction.batch_id == batch["batch_id"])
            ):
                prediction.created_utc = completed
            write_report(batch, output)
            made = True
            _deadline(batch, datetime.now(UTC))
            event.listen(
                session, "before_commit", lambda _: _deadline(batch, datetime.now(UTC)), once=True
            )
        LOG.info("已冻结批次 %s，报告 %s", batch["batch_id"], output / "report.md")
        return batch
    except Exception:
        # Only remove the fixed files created by this invocation, never an existing report.
        if made:
            for name in ("forecast.json", "report.md", "report.csv", "manifest.json"):
                (output / name).unlink(missing_ok=True)
            output.rmdir()
        raise
    finally:
        engine.dispose()


def run_report(args):
    batch = load_batch(Path(args.batch))
    output = Path(args.out)
    write_report(batch, output)
    LOG.info("已从冻结批次 %s 导出 %s", batch["batch_id"], output / "report.md")
    return batch
