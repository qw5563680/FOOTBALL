"""One-off FD history collection; the existing adapter owns all data/import rules."""

import csv
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import requests
from requests.adapters import HTTPAdapter

from zucai.league_registry import provider_config
from zucai.request_budget import DeferredRequestBudget, defer_scope
from zucai.sources.base import RateLimiter, RawStore, make_session
from zucai.sources.fd_ingest import COUNT_KEYS, fetch_fd
from zucai.sources.football_data import (
    LEAGUES_PATH,
    ROOT_URL,
    FootballDataCoUk,
    csv_url,
    verified_league,
)

LOG = logging.getLogger(__name__)


def _year(value, default):
    if value is None:
        return default
    value = str(value)
    if re.fullmatch(r"\d{4}", value):
        number = int(value)
        if 2000 <= number <= 2099:
            return number
        if int(value[2:]) == (int(value[:2]) + 1) % 100:
            return 2000 + int(value[:2])
    raise ValueError("范围须为起始自然年（2021）或连续赛季代码（2122）")


def backfill_scopes(leagues, codes=None, first=None, last=None):
    start, end = _year(first, 2021), _year(last, 2026)
    if start > end:
        raise ValueError("--from 不能晚于 --to")
    selected = (
        [code for code, entry in leagues.items() if entry.get("status") == "verified"]
        if codes is None
        else [code.strip() for code in codes.split(",")]
    )
    if not selected or any(not code for code in selected) or len(set(selected)) != len(selected):
        raise ValueError("--leagues 必须是非空且不重复的联赛代码")
    scopes = []
    for code in sorted(selected):
        entry = verified_league(leagues, code)
        if entry.get("publisher_country"):
            scopes.append((code, dict(country=code, from_season=start, to_season=end)))
        else:
            for year in range(start, end + 1):
                season = f"{year % 100:02d}{(year + 1) % 100:02d}"
                scopes.append((code, dict(div=code, seasons=[season])))
    return scopes


class _ObservedAdapter(HTTPAdapter):
    """Measure actual send starts, including failed requests and redirect hops."""

    def __init__(self):
        super().__init__(max_retries=0)
        # Keep the shared source limiter intact. A small send-entry margin avoids
        # the sub-2s preparation jitter observed during T15's first discarded run.
        self.limiter = RateLimiter(2.1)
        self.records = []
        self.kind = "robots"

    def send(self, request, **kwargs):
        self.limiter.wait()
        started = time.monotonic()
        row = {
            "kind": self.kind,
            "url": request.url,
            "started_utc": datetime.now(UTC).isoformat(),
            "monotonic_seconds": started,
            "interval_seconds": started - self.records[-1]["monotonic_seconds"]
            if self.records
            else None,
            "status_code": None,
            "sha256": None,
            "error_type": None,
        }
        self.records.append(row)
        try:
            response = super().send(request, **kwargs)
            row.update(
                status_code=response.status_code,
                sha256=hashlib.sha256(response.content).hexdigest(),
            )
            return response
        except requests.RequestException as exc:
            row["error_type"] = type(exc).__name__
            raise


def _read_robots(source, urls):
    url = ROOT_URL + "/robots.txt"
    allowed = {url, url.replace("//www.", "//")}
    visited = set()
    for _ in range(3):
        if url not in allowed or url in visited:
            raise ValueError("robots 重定向改变来源或出现循环")
        visited.add(url)
        source.limiter.wait()
        response = source.session.get(url, allow_redirects=False)
        path = source.store.save_response(source.name, "robots", response, "txt")
        response.raise_for_status()
        if response.status_code in {301, 302, 303, 307, 308}:
            url = urljoin(response.url, response.headers.get("Location", ""))
            continue
        if response.status_code != 200 or "user-agent:" not in response.text.lower():
            raise ValueError("robots 响应无有效规则，停止回填")
        policy = RobotFileParser()
        policy.parse(response.text.splitlines())
        if not all(policy.can_fetch("ZucaiResearch", item) for item in urls):
            raise ValueError("robots 禁止本项目采集器访问计划 CSV，停止回填")
        return {
            **source.store.load_metadata(path),
            "allowed": True,
            "user_agent": source.session.headers["User-Agent"],
            "path": path.relative_to(source.store.root).as_posix(),
        }
    raise ValueError("robots 重定向超过允许次数")


def _result_counts(database):
    with sqlite3.connect(database) as connection:
        return dict(
            connection.execute(
                "SELECT m.league,count(*) FROM matches m JOIN results r USING(match_id) "
                "WHERE m.source='fd' GROUP BY m.league"
            )
        )


def _failure(exc):
    if isinstance(exc, DeferredRequestBudget):
        return "请求中途达到本批上限，分页/范围未完成"
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, requests.RequestException):
        return f"网络请求失败 ({type(exc).__name__})"
    return f"{type(exc).__name__}: {exc}"[:160]


def _request_audit(path, run_id, records):
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=["run_id", *records[0]])
        if output.tell() == 0:
            writer.writeheader()
        writer.writerows({"run_id": run_id, **row} for row in records)


def collect_backfill(
    database: Path,
    run_id: str,
    *,
    codes=None,
    first=None,
    last=None,
    _scopes=None,
    _source=None,
    _raw_root=None,
):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)*", run_id):
        raise ValueError("无效 run ID")
    config = provider_config("fd")
    scopes = backfill_scopes(config, codes, first, last) if _scopes is None else _scopes
    before = _result_counts(database)
    audit = database.parent / "state/audit"
    raw = Path(_raw_root) if _raw_root is not None else database.parent / "raw"
    session = make_session() if _source is None else _source.session
    observer = _ObservedAdapter()
    session.mount("https://", observer)
    source = _source or FootballDataCoUk(store=RawStore(raw), session=session, leagues=config)
    report = dict.fromkeys(COUNT_KEYS, 0)
    report.update(
        status="complete",
        operation="backfill",
        run_id=run_id,
        downloaded_files=0,
        processed_files=0,
        scopes=[],
        failed_scopes=[],
        deferred_scopes=[],
        errors=[],
    )
    urls = [
        csv_url(
            div=scope.get("div"),
            season=scope.get("seasons", [None])[0],
            country=scope.get("country"),
        )
        for _, scope in scopes
    ]
    try:
        report["robots"] = _read_robots(source, urls)
        observer.kind = "csv"
        for (code, scope), url in zip(scopes, urls, strict=True):
            label = (
                f"{code}/{scope['seasons'][0]}"
                if "div" in scope
                else (f"{code}/{scope['from_season']}-{scope['to_season']}")
            )
            item = {"code": code, "scope": label, "url": url, "status": "complete"}
            budget = getattr(source, "request_budget", None)
            request_start = budget.used if budget else 0
            try:
                path = source.fetch_raw(
                    div=scope.get("div"),
                    season=scope.get("seasons", [None])[0],
                    country=scope.get("country"),
                )
                report["downloaded_files"] += 1
                item["raw"] = {
                    **source.store.load_metadata(path),
                    "path": path.relative_to(source.store.root).as_posix(),
                }
                # Replay through the exact existing CLI importer, with its unchanged
                # parser, aliases, official-first deduplication and assumed policy.
                imported = fetch_fd(
                    **scope,
                    assume=timedelta(hours=3),
                    db=database,
                    leagues_path=LEAGUES_PATH,
                    raw_file=path,
                    raw_root=raw,
                    audit_dir=audit,
                )
                for key in (*COUNT_KEYS, "processed_files"):
                    report[key] += imported[key]
                    item[key] = imported[key]
                if imported["rejected_rows"] or imported["suspected_duplicates"]:
                    item["status"] = report["status"] = "partial"
                    report["errors"].append(f"{label}: 单行拒绝或疑似重复，详见 state/audit")
                LOG.info(
                    "FD回填 %s：新增%d，未变化%d，拒绝%d，可疑重复%d",
                    label,
                    imported["inserted"],
                    imported["unchanged"],
                    imported["rejected_rows"],
                    imported["suspected_duplicates"],
                )
            except Exception as exc:
                if defer_scope(
                    report, item, exc, requests_started=bool(budget and budget.used > request_start)
                ):
                    report["scopes"].append(item)
                    continue
                reason = _failure(exc)
                item.update(status="partial", error=reason)
                report["status"] = "partial"
                report["failed_scopes"].append({"scope": label, "reason": reason})
                report["errors"].append(f"{label}: {reason}")
                LOG.warning("FD回填 %s 失败，继续下一文件：%s", label, reason)
            report["scopes"].append(item)
    finally:
        session.close()
        _request_audit(audit / "fd_backfill_requests.csv", run_id, observer.records)
    after = _result_counts(database)
    report["leagues"] = {
        code: {
            "league": config[code]["league"],
            "results_before": before.get(config[code]["league"], 0),
            "results_after": after.get(config[code]["league"], 0),
            "results_added": after.get(config[code]["league"], 0)
            - before.get(config[code]["league"], 0),
        }
        for code in sorted({code for code, _ in scopes})
    }
    csv_starts = [r["monotonic_seconds"] for r in observer.records if r["kind"] == "csv"]
    report.update(
        csv_requests=len(csv_starts),
        robots_requests=len(observer.records) - len(csv_starts),
        requests=len(observer.records),
        csv_min_interval_seconds=min(
            (b - a for a, b in zip(csv_starts, csv_starts[1:], strict=False)), default=None
        ),
        min_interval_seconds=min(
            (r["interval_seconds"] for r in observer.records[1:]), default=None
        ),
    )
    source.store.save(
        source.name,
        f"backfill-{run_id}",
        json.dumps(report, ensure_ascii=False, indent=2).encode(),
        "json",
    )
    LOG.info(
        "FD回填 CSV请求%d，robots请求%d，实际CSV最短间隔%s秒，总新增%d",
        report["csv_requests"],
        report["robots_requests"],
        report["csv_min_interval_seconds"],
        report["inserted"],
    )
    return report


def write_summary(report):
    if not os.environ.get("GITHUB_STEP_SUMMARY"):
        return
    lines = [
        f"### FD 一次性历史回填：{report['status']}",
        "",
        f"CSV 请求 {report['csv_requests']} 次（含重定向和失败），"
        f"robots 请求 {report['robots_requests']} 次；"
        f"实际 CSV 最短间隔：{report['csv_min_interval_seconds']} 秒。",
        "",
        f"robots 检查允许；原件 SHA256：`{report['robots']['sha256']}`。",
        f"新增 {report['inserted']}，未变化 {report['unchanged']}，拒绝 {report['rejected_rows']}，"
        f"可疑重复 {report['suspected_duplicates']}；"
        "逐跳请求明细：`state/audit/fd_backfill_requests.csv`。",
        "",
        "| 代码 | 联赛 | 回填前 fd 赛果 | 本次新增 fd 赛果 | 回填后 fd 赛果 |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for code, counts in report["leagues"].items():
        lines.append(
            f"| {code} | {counts['league']} | {counts['results_before']} | "
            f"{counts['results_added']} | {counts['results_after']} |"
        )
    lines += ["", "原件及完整运行记录位于本次 Actions artifact；逐场拒绝/疑似重复沿用原规则。"]
    for error in report["errors"]:
        lines.append("- " + error.replace("\n", " ").replace("|", "\\|"))
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as output:
        output.write("\n".join(lines) + "\n")
