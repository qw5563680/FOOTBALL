"""Build SFC diagnostics from today's frozen forecasts; never run a model or collector."""

import argparse
import copy
import hashlib
import json
import logging
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import select
from sqlalchemy.orm import Session

from zucai.cloud_feeds import digest, restore_feeds
from zucai.db import get_engine
from zucai.forecast_archive import _path, verify_forecasts
from zucai.models import SfcIssue
from zucai.report.render import _json_bytes, _timestamp, load_batch
from zucai.report.sfc import build_sfc_report, write_sfc_report
from zucai.sfc_publication import (
    PUBLICATION_FIELDS,
    PUBLISHED_REF,
    deadline_message,
    deadline_status,
    publication_evidence,
    with_publication,
)
from zucai.sources.base import _atomic_write
from zucai.sources.c500 import issue_number
from zucai.sources.sporttery import BEIJING

LOG = logging.getLogger(__name__)
FILES = ("sfc.json", "report.md", "report.csv", "manifest.json")


def read_report(archive, directory):
    root = _path(archive, directory)
    manifest = json.loads(_path(root, "manifest.json").read_bytes())
    if manifest.get("algorithm") != "sha256" or set(manifest.get("files", {})) != set(FILES[:-1]):
        raise ValueError("SFC 报告清单无效")
    for name, sha in manifest["files"].items():
        if digest(_path(root, name)) != sha:
            raise ValueError(f"SFC 报告 SHA256 不符：{directory}/{name}")
    report = json.loads((root / "sfc.json").read_bytes())
    if report["issue_no"] != directory.split("/")[1] or len(report["rows"]) != 14:
        raise ValueError("SFC 报告期号或行数不符")
    for entry in report.get("forecast_sources", []):
        if not re.fullmatch(r"forecasts/[0-9-]+/[A-Za-z0-9_.-]+/forecast.json", entry["path"]):
            raise ValueError("SFC 预测来源路径无效")
        if digest(_path(archive, entry["path"])) != entry["sha256"]:
            raise ValueError("SFC 预测原件 SHA256 不符")
        if load_batch(archive / entry["path"]) != entry["batch"]:
            raise ValueError("SFC 嵌入预测与原件不符")
    if "publication_schema" in report:
        if report["publication_schema"] != 1 or any(
            key not in report or key not in manifest or manifest[key] != report[key]
            for key in PUBLICATION_FIELDS
        ):
            raise ValueError("SFC 报告与清单发布时间元数据不符")
        if report["sale_end_utc"] != report["source"]["sale_end_utc"] or report[
            "sale_state"
        ] != deadline_status(report["sale_end_utc"], report["published_utc"]):
            raise ValueError("SFC 停售判断与发布时间不符")
        evidence = report["publication_evidence"]
        if evidence:
            candidate_path = evidence.get("candidate_path", "")
            if (
                not re.fullmatch(
                    rf"sfc/{report['issue_no']}/history/[A-Za-z0-9_.-]+/sfc.json", candidate_path
                )
                or candidate_path == directory + "/sfc.json"
                or not re.fullmatch(r"[0-9a-f]{40,64}", evidence.get("commit", ""))
                or not re.fullmatch(
                    r"refs/remotes/[^/]+/data/football-feeds", evidence.get("ref", "")
                )
                or digest(_path(archive, candidate_path)) != evidence.get("candidate_sha256")
            ):
                raise ValueError("SFC 发布时间缺少有效原报告证据")
            candidate = read_report(archive, candidate_path.removesuffix("/sfc.json"))
            if (
                candidate.get("publication_evidence")
                or with_publication(candidate, evidence) != report
            ):
                raise ValueError("SFC 发布确认改变了原报告证据")
        elif report["published_utc"] is not None or report["publication_commit"] is not None:
            raise ValueError("SFC 存好时间缺少发布证据")
    return report


def read_reports(archive, *, include_unconfirmed=False):
    """Read immutable versions, checking the mutable view belongs to that history."""
    root = _path(archive, "sfc")
    result = []
    if not root.exists():
        return result
    for directory in sorted(root.iterdir()):
        if not directory.is_dir():
            continue
        number = issue_number(directory.name)
        history = _path(archive, f"sfc/{number}/history")
        versions = []
        for version in sorted(history.iterdir()) if history.exists() else []:
            path = f"sfc/{number}/history/{version.name}"
            report = read_report(archive, path)
            versions.append(path)
            result.append((path, report))
        if not versions:
            raise ValueError("SFC 报告缺少冻结历史")
        latest = read_report(archive, f"sfc/{number}")
        if not any(latest == r for p, r in result if p in versions):
            raise ValueError("SFC 当前报告不属于冻结历史")
    return [
        (path, report)
        for path, report in result
        if include_unconfirmed
        or not report.get("publication_schema")
        or report["sale_state"] == "published_before_deadline"
    ]


def _batch(archive, run):
    sources, models, predictions = [], {}, {}
    for entry in sorted(run["forecasts"].values(), key=lambda f: f["path"]):
        path = entry["path"] + "/forecast.json"
        batch = load_batch(archive / path)
        if batch["mode"] != "prematch":
            raise ValueError("SFC 自动报告只引用赛前冻件")
        sources.append({"path": path, "sha256": digest(archive / path), "batch": batch})
        for model in batch["models"]:
            key = (model["league"], model["model_version"])
            if key in models and model != models[key]:
                raise ValueError("同次预测的模型元数据冲突")
            models[key] = model
        for row in batch["predictions"]:
            if row["match_id"] in predictions:
                raise ValueError("同次预测包含重复比赛")
            predictions[row["match_id"]] = row
    combined = copy.deepcopy(sources[-1]["batch"])
    combined.update(
        batch_id="sfc-reference-" + run["run_id"],
        generated_utc=max((s["batch"]["generated_utc"] for s in sources), key=_timestamp),
        as_of_utc=max((s["batch"]["as_of_utc"] for s in sources), key=_timestamp),
        models=list(models.values()),
        predictions=list(predictions.values()),
        input={"reference_only": True, "run_id": run["run_id"]},
    )
    return combined, sources


def publish_reports(archive, *, now=None):
    archive = Path(archive).resolve()
    at = now or datetime.now(UTC)
    at = _timestamp(at.isoformat() if isinstance(at, datetime) else at)
    manifest = verify_forecasts(archive)
    existing = read_reports(archive, include_unconfirmed=True)
    runs = [
        run
        for run in (manifest or {}).get("runs", {}).values()
        if _timestamp(run["generated_utc"]) <= at
        and _timestamp(run["generated_utc"]).astimezone(BEIJING).date()
        == at.astimezone(BEIJING).date()
    ]
    if not runs:
        LOG.warning("尚无当日冻结预测，SFC 报告未更新")
        return []
    run = max(runs, key=lambda r: (_timestamp(r["generated_utc"]), r["run_id"]))
    batch, sources = _batch(archive, run)
    published = []
    with TemporaryDirectory(prefix="sfc-reports-") as temporary:
        stage = Path(temporary)
        database = stage / "data/zucai.db"
        restore_feeds(archive, database)
        original = digest(database)
        engine = get_engine(database)
        try:
            with engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA query_only=ON")
                connection.exec_driver_sql("BEGIN")
                with Session(bind=connection) as session:
                    for issue in session.scalars(select(SfcIssue).order_by(SfcIssue.issue_no)):
                        if not issue.sale_end_utc or _timestamp(issue.sale_end_utc) <= at:
                            continue
                        directory = f"sfc/{issue.issue_no}"
                        history = f"{directory}/history/{run['run_id']}"
                        report = build_sfc_report(
                            session,
                            issue,
                            batch,
                            generated=at,
                            max_lines=64,
                            allow_missing=True,
                            forecast_sha256=hashlib.sha256(_json_bytes(batch)).hexdigest(),
                        )
                        report["forecast_sources"] = sources
                        report["run_id"] = run["run_id"]
                        references = {
                            p["match_id"]: {
                                "path": source["path"],
                                "sha256": source["sha256"],
                                **{
                                    key: source["batch"][key]
                                    for key in ("batch_id", "generated_utc", "as_of_utc")
                                },
                            }
                            for source in sources
                            for p in source["batch"]["predictions"]
                        }
                        for row in report["rows"]:
                            row["forecast_reference"] = references.get(row["match_id"])
                        # Calculation completion is separate from the later Git
                        # publication timestamp; never replace one with the other.
                        report["generated_utc"] = (at if now else datetime.now(UTC)).isoformat()
                        report = with_publication(report)
                        if any(
                            old.get("run_id") == run["run_id"]
                            and old["rows"] == report["rows"]
                            and old["source"] == report["source"]
                            for path, old in existing
                            if path.startswith(directory + "/")
                        ):
                            continue
                        if _path(archive, history).exists():
                            # A newly resolved mapping may use the same frozen
                            # forecasts. Preserve the first report, add a version.
                            fingerprint = hashlib.sha256(
                                _json_bytes(dict(rows=report["rows"], source=report["source"]))
                            ).hexdigest()[:16]
                            history += "-mapping-" + fingerprint
                        write_sfc_report(report, stage / issue.issue_no)
                        cutoff = min(
                            [_timestamp(issue.sale_end_utc)]
                            + [
                                _timestamp(r["kickoff_utc"])
                                for r in report["rows"]
                                if r["match_id"] is not None
                            ]
                        )
                        if (at if now else datetime.now(UTC)) >= cutoff:
                            LOG.warning("SFC %s 在写入前已截止，未发布", issue.issue_no)
                            continue
                        root = _path(archive, directory)
                        backups = {
                            name: (root / name).read_bytes() if (root / name).exists() else None
                            for name in FILES
                        }
                        target = _path(archive, history)
                        try:
                            target.mkdir(parents=True)
                            for name in FILES:
                                raw = (stage / issue.issue_no / name).read_bytes()
                                _atomic_write(target / name, raw)
                                _atomic_write(_path(root, name), raw)
                        except BaseException:
                            shutil.rmtree(target, ignore_errors=True)
                            for name, raw in backups.items():
                                if raw is None:
                                    (root / name).unlink(missing_ok=True)
                                else:
                                    _atomic_write(root / name, raw)
                            if (root / "history").exists() and not any(
                                (root / "history").iterdir()
                            ):
                                (root / "history").rmdir()
                            if root.exists() and not any(root.iterdir()):
                                root.rmdir()
                            raise
                        published.append(issue.issue_no)
        finally:
            engine.dispose()
        if digest(database) != original:
            raise ValueError("SFC 报告改变了输入数据库")
    LOG.info("SFC 诊断报告已更新：%s", published)
    return published


def confirm_publication(archive, *, published_ref=PUBLISHED_REF, summary=None):
    """Append a receipt version after a successful first push; never rewrite history."""
    archive = Path(archive).resolve()
    reports = read_reports(archive, include_unconfirmed=True)
    pending = [
        (path, report)
        for path, report in reports
        if report.get("publication_schema") and not report.get("publication_evidence")
    ]
    evidence = publication_evidence(archive, pending, published_ref=published_ref)
    confirmed_paths = {
        report["publication_evidence"]["candidate_path"]
        for _, report in reports
        if report.get("publication_evidence")
    }
    added = []
    for path, report in sorted(
        pending, key=lambda entry: (_timestamp(entry[1]["generated_utc"]), entry[0])
    ):
        if path not in evidence or path + "/sfc.json" in confirmed_paths:
            continue
        confirmed = with_publication(report, evidence[path])
        history = path + "-published-" + evidence[path]["commit"][:12]
        target = _path(archive, history)
        root = _path(archive, f"sfc/{report['issue_no']}")
        current = read_report(archive, f"sfc/{report['issue_no']}")
        update_current = _timestamp(report["generated_utc"]) >= _timestamp(current["generated_utc"])
        backups = {name: (root / name).read_bytes() for name in FILES}
        with TemporaryDirectory(prefix="sfc-publication-") as temporary:
            stage = Path(temporary) / "confirmed"
            write_sfc_report(confirmed, stage)
            try:
                target.mkdir(parents=True)
                for name in FILES:
                    raw = (stage / name).read_bytes()
                    _atomic_write(target / name, raw)
                    if update_current:
                        _atomic_write(root / name, raw)
            except BaseException:
                shutil.rmtree(target, ignore_errors=True)
                for name, raw in backups.items():
                    _atomic_write(root / name, raw)
                raise
        added.append(history)
    if summary:
        write_publication_summary(archive, summary)
    return added


def write_publication_summary(archive, summary):
    """Include unknown publication explicitly, even before a failed first push."""
    lines = ["\n### 14 场发布时间核验", ""]
    current_reports = {}
    for path, report in read_reports(archive, include_unconfirmed=True):
        key = (_timestamp(report["generated_utc"]), path)
        if (
            report["issue_no"] not in current_reports
            or key > current_reports[report["issue_no"]][0]
        ):
            current_reports[report["issue_no"]] = (key, report)
    for number, (_, report) in sorted(current_reports.items()):
        view = report if report.get("publication_schema") else with_publication(report)
        lines += [
            f"- 第 {number} 期：{deadline_message(view)}；"
            f"算完 UTC `{view['generated_utc']}`；"
            f"存好 UTC `{view['published_utc'] or '未知'}`；"
            f"停售 UTC `{view['sale_end_utc'] or '未知'}`；"
            f"发布提交 `{view['publication_commit'] or '未知'}`。"
        ]
    if not current_reports:
        lines += ["尚无 SFC 报告；算完、存好、停售时间未知。"]
    with Path(summary).open("a", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--confirm-publication", action="store_true")
    parser.add_argument("--published-ref", default=PUBLISHED_REF)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.confirm_publication:
        confirm_publication(args.archive, published_ref=args.published_ref, summary=args.summary)
    else:
        publish_reports(args.archive)
        if args.summary:
            write_publication_summary(args.archive, args.summary)


if __name__ == "__main__":
    main()
