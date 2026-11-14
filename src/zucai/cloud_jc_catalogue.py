"""Read only official JC result pages to cross-check twelve complete calendar months."""

import csv
import hashlib
import json
import logging
import os
import re
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from requests import RequestException

from zucai.sources.base import RawStore, _atomic_write
from zucai.sources.results import SportteryResults
from zucai.sources.sporttery import API, BEIJING, official_value

LOG = logging.getLogger(__name__)
MASTER = (
    Path(__file__).resolve().parents[2]
    / "docs/research/2026-11-14-jc-sfc-competition-master-table.csv"
)


def require_jc_catalogue_actions() -> None:
    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or os.environ.get("RUNNER_ENVIRONMENT") != "self-hosted"
    ):
        raise ValueError("JC catalogue 仅允许 self-hosted Actions workflow_dispatch")


def complete_month_window(now: datetime) -> tuple[date, date]:
    if now.utcoffset() is None:
        raise ValueError("目录窗口时间必须包含时区")
    current = now.astimezone(BEIJING).date().replace(day=1)
    return current.replace(year=current.year - 1), current - timedelta(days=1)


def _integer(value, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"JC catalogue {name} 无效")
    return value


def _page(raw: bytes, day: date, page: int) -> tuple[dict, list[dict]]:
    value = official_value(raw)
    total = _integer(value.get("total"), "total")
    pages = _integer(value.get("pages"), "pages")
    number = _integer(value.get("pageNo"), "pageNo")
    size = _integer(value.get("pageSize"), "pageSize", 1)
    rows = value.get("matchResult")
    if not isinstance(rows, list) or size != 30:
        raise ValueError("JC catalogue 分页大小或行数组无效")
    empty = total == 0 and pages in (0, 1) and page == 1 and number in (0, 1)
    if not empty and (number != page or pages != (total + size - 1) // size or page > pages):
        raise ValueError("JC catalogue 分页号或总页数不符")
    if len(rows) != min(size, max(0, total - (page - 1) * size)):
        raise ValueError("JC catalogue 分页缺行或总数不符")
    identities = []
    for row in rows:
        if not isinstance(row, dict) or row.get("matchDate") != day.isoformat():
            raise ValueError("JC catalogue 接口返回了其他日期")
        match_id = _integer(row.get("matchId"), "matchId", 1)
        league_id = _integer(row.get("leagueId"), "leagueId", 1)
        names = (row.get("leagueName"), row.get("leagueNameAbbr"))
        if any(not isinstance(name, str) or not name.strip() for name in names):
            raise ValueError("JC catalogue 缺少赛事名称")
        identities.append(
            dict(match_id=match_id, league_id=league_id, league_name=names[0], league_abbr=names[1])
        )
    return {"total": total, "pages": pages, "pageSize": size}, identities


def parse_day_pages(day: date, pages: list[bytes]) -> list[dict]:
    """Validate one complete day, returning only league identity and an internal dedup key."""
    if not pages:
        raise ValueError("JC catalogue 缺少日期页")
    result, ids, expected = [], set(), None
    for number, raw in enumerate(pages, 1):
        metadata, rows = _page(raw, day, number)
        if expected is None:
            expected = metadata
        elif metadata != expected:
            raise ValueError("JC catalogue 采集期间分页总数变动")
        new_ids = {row["match_id"] for row in rows}
        if len(new_ids) != len(rows) or ids & new_ids:
            raise ValueError("JC catalogue 分页重复")
        ids.update(new_ids)
        result.extend(rows)
    if len(pages) != max(1, expected["pages"]) or len(result) != expected["total"]:
        raise ValueError("JC catalogue 未取得完整日期页")
    return result


def _preserve_page(source: SportteryResults, path: Path, folder: Path, day: date, page: int):
    raw = source.store.load(path)
    sidecar_raw = path.with_suffix(path.suffix + ".meta.json").read_bytes()
    sidecar = json.loads(sidecar_raw)
    parsed = urlparse(sidecar.get("url") or "")
    expected = urlparse(API + "getUniformMatchResultV1.qry")
    query = parse_qs(parsed.query, keep_blank_values=True)
    captured = datetime.fromisoformat(sidecar["fetched_at_utc"])
    if (
        (parsed.scheme, parsed.netloc, parsed.path)
        != (expected.scheme, expected.netloc, expected.path)
        or query.get("matchBeginDate") != [day.isoformat()]
        or query.get("matchEndDate") != [day.isoformat()]
        or query.get("pageNo") != [str(page)]
        or query.get("pageSize") != ["30"]
        or captured.utcoffset() is None
        or sidecar.get("sha256") != hashlib.sha256(raw).hexdigest()
        or type(sidecar.get("status_code")) is not int
    ):
        raise ValueError("JC catalogue 原件来源或哈希无效")
    target = folder / "jc" / day.strftime("%Y-%m") / f"{day}-p{page}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(target.with_suffix(".json.meta.json"), sidecar_raw)
    _atomic_write(target, raw)
    return raw


def _crosscheck(observations: Counter) -> tuple[list[dict], list[dict]]:
    with MASTER.open(encoding="utf-8-sig", newline="") as stream:
        master = list(csv.DictReader(stream))
    if len(master) != 90 or len({row["赛事"] for row in master}) != 90:
        raise ValueError("赛事总表必须保留全部 90 行")
    names = {}
    for number, row in enumerate(master, 1):
        for name in row["来源出现过的名称"].split("、"):
            names.setdefault(name, set()).add(number)
    matched = {number: [] for number in range(1, 91)}
    decisions = []
    for (league_id, league_name, league_abbr), count in sorted(observations.items()):
        observed_names = {league_name, league_abbr}
        candidates = set().union(*(names.get(name, set()) for name in observed_names))
        observed = dict(
            league_id=league_id,
            league_name=league_name,
            league_abbr=league_abbr,
            match_count=count,
            master_rows=sorted(candidates),
        )
        if len(candidates) != 1:
            reason = "ambiguous_source_name" if candidates else "observed_name_not_in_master"
            decisions.append(dict(reason=reason, **observed))
        for number in candidates:
            matched[number].append(observed)
    crosscheck = []
    for number, row in enumerate(master, 1):
        observed = matched[number]
        if not observed:
            status = "not_observed_in_window"
            decisions.append(
                dict(
                    reason="master_not_observed_in_window",
                    master_row=number,
                    master_name=row["赛事"],
                )
            )
        elif any(len(item["master_rows"]) != 1 for item in observed):
            status = "owner_decision"
        else:
            status = "observed_exact"
        crosscheck.append(
            dict(
                master_row=number,
                master_name=row["赛事"],
                project_league=row["项目内赛事名"],
                source_names=row["来源出现过的名称"].split("、"),
                status=status,
                observations=observed,
            )
        )
    return crosscheck, decisions


def _save_json(path: Path, value: dict) -> None:
    _atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def _previous_pointers(root: Path) -> dict:
    from zucai.cloud_feeds import CATALOGUE_RUN_ID

    latest = root / "state/catalogue/latest.json"
    if not latest.exists():
        return {}
    index = json.loads(latest.read_bytes())
    if index.get("version") != 1 or not isinstance(index.get("providers"), dict):
        raise ValueError("JC catalogue 原有目录索引无效")
    for provider, entry in index["providers"].items():
        if provider not in {"apif", "fdorg", "fd", "jc"} or not isinstance(entry, dict):
            raise ValueError("JC catalogue 原有目录索引无效")
        run_id = entry.get("run_id")
        if not isinstance(run_id, str) or not re.fullmatch(CATALOGUE_RUN_ID, run_id):
            raise ValueError("JC catalogue 原有目录运行编号无效")
        expected = f"state/catalogue/{run_id}/{provider}.json"
        if entry.get("path") != expected:
            raise ValueError("JC catalogue 原有目录指针路径无效")
        raw = (root / expected).read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry.get("sha256"):
            raise ValueError("JC catalogue 原有目录指针哈希无效")
    return index["providers"]


def collect_jc_catalogue(database: Path, run_id: str, *, now: datetime | None = None) -> dict:
    """Use the existing one-second Sporttery page transport; never open or change the DB."""
    require_jc_catalogue_actions()
    from zucai.cloud_feeds import CATALOGUE_RUN_ID, _catalogue_files

    if not re.fullmatch(CATALOGUE_RUN_ID, run_id):
        raise ValueError("JC catalogue run_id 无效")
    start, end = complete_month_window(now if now is not None else datetime.now(UTC))
    _catalogue_files(database.parent)
    pointers = _previous_pointers(database.parent)
    folder = database.parent / "state/catalogue" / run_id
    folder.mkdir(parents=True, exist_ok=False)
    source = SportteryResults(store=RawStore(database.parent / "raw/jc-catalogue" / run_id))
    report = dict(
        collection="catalogue",
        provider="jc",
        status="complete",
        run_id=run_id,
        started_utc=datetime.now(UTC).isoformat(),
        requests=0,
        window=dict(
            start_beijing=start.isoformat(),
            end_beijing=end.isoformat(),
            basis="previous_12_complete_beijing_calendar_months",
        ),
        count_basis=(
            "official result matchDate in Asia/Shanghai; master uses sale-number days/positions"
        ),
        months=[],
        errors=[],
    )
    observed, all_ids = Counter(), set()
    day = start
    while day <= end:
        month = day.strftime("%Y-%m")
        if not report["months"] or report["months"][-1]["month"] != month:
            report["months"].append(
                dict(month=month, complete_days=0, failed_days=[], match_count=0, leagues=[])
            )
            month_counts = Counter()
        current = report["months"][-1]
        pages, number = [], 1
        try:
            while True:
                report["requests"] += 1
                try:
                    path = source.fetch_raw(day=day, page=number)
                except RequestException:
                    # fetch_raw retains HTTP failures before raising; retain that same original.
                    for saved in source.store.root.glob(
                        f"{source.name}/*/results-{day}-p{number}_*.json"
                    ):
                        if not saved.name.endswith(".meta.json"):
                            _preserve_page(source, saved, folder, day, number)
                    raise
                raw = _preserve_page(source, path, folder, day, number)
                pages.append(raw)
                metadata, _ = _page(raw, day, number)
                if number >= metadata["pages"]:
                    break
                # Stop changing page totals before following a new pagination bound.
                if number > 1 and metadata != _page(pages[0], day, 1)[0]:
                    raise ValueError("JC catalogue 采集期间分页总数变动")
                number += 1
            rows = parse_day_pages(day, pages)
            ids = {row["match_id"] for row in rows}
            if all_ids & ids:
                raise ValueError("JC catalogue 不同日期重复 matchId")
            all_ids.update(ids)
            counts = Counter((r["league_id"], r["league_name"], r["league_abbr"]) for r in rows)
            observed.update(counts)
            month_counts.update(counts)
            current["complete_days"] += 1
            current["match_count"] += len(rows)
            current["leagues"] = [
                dict(league_id=key[0], league_name=key[1], league_abbr=key[2], match_count=count)
                for key, count in sorted(month_counts.items())
            ]
        except (RequestException, ValueError, KeyError, TypeError) as exc:
            current["failed_days"].append(day.isoformat())
            report["status"] = "partial"
            report["errors"].append(
                dict(day=day.isoformat(), page=number, error=type(exc).__name__)
            )
            LOG.warning("JC catalogue %s page %s failed (%s)", day, number, type(exc).__name__)
        day += timedelta(days=1)
    report["master_crosscheck"], report["owner_decisions"] = _crosscheck(observed)
    report["captured_utc"] = datetime.now(UTC).isoformat()
    _save_json(folder / "jc.json", report)
    _save_json(folder / "summary.json", report)
    if report["status"] == "complete":
        pointers["jc"] = dict(
            path=f"state/catalogue/{run_id}/jc.json",
            run_id=run_id,
            sha256=hashlib.sha256((folder / "jc.json").read_bytes()).hexdigest(),
            raw_sha256s=[
                json.loads(path.read_bytes())["sha256"]
                for path in sorted((folder / "jc").rglob("*.json.meta.json"))
            ],
        )
    _save_json(folder.parent / "latest.json", dict(version=1, providers=pointers))
    return report


def write_summary(report: dict) -> None:
    if not os.environ.get("GITHUB_STEP_SUMMARY"):
        return
    window = report["window"]
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
        stream.write(
            f"### JC monthly catalogue: {report['status']}\n\n"
            f"北京时间完整月份：{window['start_beijing']} 至 {window['end_beijing']}；"
            f"请求 {report['requests']} 页；总表核对 {len(report['master_crosscheck'])} 行。\n\n"
            f"待所有者决定 {len(report['owner_decisions'])} 项；窗口内未见不代表接口无数据。\n\n"
            "只统计赛事身份与场数；完整原件、捕获 UTC 时间与 SHA256 随 state/catalogue 归档。\n"
        )
