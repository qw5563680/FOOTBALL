"""Read only official JC result pages to cross-check twelve complete calendar months."""

import csv
import hashlib
import json
import logging
import os
import re
from collections import Counter
from dataclasses import dataclass
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


def _official_leagues(value: dict) -> list[dict]:
    if "leagueList" not in value:
        return []  # Keep the existing pagination contract for responses without a dictionary.
    leagues = value["leagueList"]
    if not isinstance(leagues, list):
        raise ValueError("JC catalogue 官方赛事字典无效")
    result, seen = [], set()
    for league in leagues:
        if not isinstance(league, dict):
            raise ValueError("JC catalogue 官方赛事字典条目无效")
        league_id = _integer(league.get("leagueId"), "leagueId", 1)
        names = (league.get("leagueAllName"), league.get("leagueAbbName"))
        if any(not isinstance(name, str) or not name.strip() for name in names):
            raise ValueError("JC catalogue 官方赛事字典缺少名称")
        if league_id in seen:
            raise ValueError("JC catalogue 官方赛事字典 id 重复")
        seen.add(league_id)
        result.append(dict(league_id=league_id, league_name=names[0], league_abbr=names[1]))
    return result


def parse_official_leagues(raw: bytes) -> list[dict]:
    """Extract only official identities; dictionary entries never become match records."""
    return _official_leagues(official_value(raw))


def _page(raw: bytes, day: date, page: int) -> tuple[dict, list[dict]]:
    value = official_value(raw)
    leagues = _official_leagues(value)
    total = _integer(value.get("total"), "total")
    pages = _integer(value.get("pages"), "pages")
    number = _integer(value.get("pageNo"), "pageNo")
    size = _integer(value.get("pageSize"), "pageSize")
    rows = value.get("matchResult")
    if size == 0:
        # Observed in all twenty empty days of run 43539673769-1. Do not infer
        # an empty result from absent pagination, absent rows, or a failed envelope.
        if (
            _integer(value.get("resultCount"), "resultCount") != 0
            or (total, pages, number, page) != (0, 0, 0, 1)
            or rows != []
            or not leagues
        ):
            raise ValueError("JC catalogue 真实空页形状无效")
        return {"total": 0, "pages": 0, "pageSize": 0}, []
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
        if (
            leagues
            and dict(league_id=league_id, league_name=names[0], league_abbr=names[1]) not in leagues
        ):
            raise ValueError("JC catalogue 比赛事项与官方字典冲突")
        identities.append(
            dict(match_id=match_id, league_id=league_id, league_name=names[0], league_abbr=names[1])
        )
    return {"total": total, "pages": pages, "pageSize": size}, identities


def parse_day_pages(day: date, pages: list[bytes]) -> list[dict]:
    """Validate one complete day, returning only league identity and an internal dedup key."""
    if not pages:
        raise ValueError("JC catalogue 缺少日期页")
    result, ids, expected, leagues = [], set(), None, {}
    for number, raw in enumerate(pages, 1):
        metadata, rows = _page(raw, day, number)
        for league in parse_official_leagues(raw):
            league_id = league["league_id"]
            if league_id in leagues and leagues[league_id] != league:
                raise ValueError("JC catalogue 跨页官方赛事字典冲突")
            leagues[league_id] = league
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


@dataclass(frozen=True)
class _ArchivedPage:
    day: date
    number: int
    raw: bytes
    sidecar_raw: bytes
    metadata: dict

    @property
    def relative_path(self) -> str:
        return f"jc/{self.day:%Y-%m}/{self.day}-p{self.number}.json"


def _utc(value, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"JC catalogue {name} 时间无效")
    instant = datetime.fromisoformat(value)
    if instant.utcoffset() != timedelta(0):
        raise ValueError(f"JC catalogue {name} 必须是 UTC 时间")
    return instant


def _archived_page(
    raw: bytes, sidecar_raw: bytes, day: date, page: int, *, require_success: bool = True
) -> _ArchivedPage:
    sidecar = json.loads(sidecar_raw)
    if not isinstance(sidecar, dict):
        raise ValueError("JC catalogue 原件 sidecar 无效")
    parsed = urlparse(sidecar.get("url") or "")
    expected = urlparse(API + "getUniformMatchResultV1.qry")
    query = parse_qs(parsed.query, keep_blank_values=True)
    _utc(sidecar.get("fetched_at_utc"), "fetched_at_utc")
    expected_query = dict(
        matchBeginDate=[day.isoformat()],
        matchEndDate=[day.isoformat()],
        leagueId=[""],
        pageSize=["30"],
        pageNo=[str(page)],
        isFix=["0"],
        matchPage=["1"],
        pcOrWap=["1"],
    )
    if (
        (parsed.scheme, parsed.netloc, parsed.path)
        != (expected.scheme, expected.netloc, expected.path)
        or parsed.fragment
        or query != expected_query
        or sidecar.get("sha256") != hashlib.sha256(raw).hexdigest()
        or type(sidecar.get("status_code")) is not int
        or not 100 <= sidecar["status_code"] <= 599
        or (require_success and sidecar["status_code"] != 200)
    ):
        raise ValueError("JC catalogue 原件来源或哈希无效")
    return _ArchivedPage(day, page, raw, sidecar_raw, sidecar)


def _write_page(folder: Path, page: _ArchivedPage) -> None:
    target = folder / page.relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(target.with_suffix(".json.meta.json"), page.sidecar_raw)
    _atomic_write(target, page.raw)


def _preserve_page(
    source: SportteryResults,
    path: Path,
    folder: Path,
    day: date,
    page: int,
    *,
    require_success: bool = True,
) -> _ArchivedPage:
    archived = _archived_page(
        source.store.load(path),
        path.with_suffix(path.suffix + ".meta.json").read_bytes(),
        day,
        page,
        require_success=require_success,
    )
    _write_page(folder, archived)
    return archived


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


def _new_report(run_id: str, start: date, end: date) -> dict:
    return dict(
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


def _aggregate(report: dict, load_page, source_run_id: str) -> list[dict]:
    """The live collector and offline replay share pagination, identities and totals."""
    observed, all_ids, leagues, ranges = Counter(), set(), {}, {}
    day = date.fromisoformat(report["window"]["start_beijing"])
    end = date.fromisoformat(report["window"]["end_beijing"])
    while day <= end:
        month = day.strftime("%Y-%m")
        if not report["months"] or report["months"][-1]["month"] != month:
            report["months"].append(
                dict(
                    month=month,
                    complete_days=0,
                    empty_days=[],
                    failed_days=[],
                    match_count=0,
                    leagues=[],
                )
            )
            month_counts = Counter()
        current = report["months"][-1]
        pages, number = [], 1
        try:
            while True:
                archived = load_page(day, number)
                pages.append(archived)
                metadata, _ = _page(archived.raw, day, number)
                # Stop changing page totals before following a new pagination bound.
                if number > 1 and metadata != _page(pages[0].raw, day, 1)[0]:
                    raise ValueError("JC catalogue 采集期间分页总数变动")
                if number >= metadata["pages"]:
                    break
                number += 1
            rows = parse_day_pages(day, [page.raw for page in pages])
            ids = {row["match_id"] for row in rows}
            if all_ids & ids:
                raise ValueError("JC catalogue 不同日期重复 matchId")
            # Commit a day's dictionary only after all pages and identities agree.
            day_leagues = dict(leagues)
            for page in pages:
                for league in parse_official_leagues(page.raw):
                    league_id = league["league_id"]
                    previous = day_leagues.get(league_id)
                    if previous and any(previous[key] != value for key, value in league.items()):
                        raise ValueError(f"JC catalogue 官方赛事字典冲突: {league_id}")
                    if previous is None:
                        day_leagues[league_id] = dict(
                            **league,
                            first_observed_run_id=source_run_id,
                            first_observed_day=day.isoformat(),
                            first_observed_captured_utc=page.metadata["fetched_at_utc"],
                            raw_path=page.relative_path,
                            raw_sha256=page.metadata["sha256"],
                        )
            leagues = day_leagues
            all_ids.update(ids)
            counts = Counter((r["league_id"], r["league_name"], r["league_abbr"]) for r in rows)
            for key in counts:
                ranges.setdefault(key, [day.isoformat(), day.isoformat()])[1] = day.isoformat()
            observed.update(counts)
            month_counts.update(counts)
            current["complete_days"] += 1
            current["match_count"] += len(rows)
            if not rows:
                current["empty_days"].append(day.isoformat())
            current["leagues"] = [
                dict(league_id=key[0], league_name=key[1], league_abbr=key[2], match_count=count)
                for key, count in sorted(month_counts.items())
            ]
        except (RequestException, ValueError, KeyError, TypeError) as exc:
            current["failed_days"].append(day.isoformat())
            report["status"] = "partial"
            report["errors"].append(
                dict(day=day.isoformat(), page=number, error=type(exc).__name__, reason=str(exc))
            )
            LOG.warning("JC catalogue %s page %s failed (%s)", day, number, type(exc).__name__)
        day += timedelta(days=1)
    report["observations"] = [
        dict(
            league_id=key[0],
            league_name=key[1],
            league_abbr=key[2],
            match_count=count,
            first_match_day=ranges[key][0],
            last_match_day=ranges[key][1],
        )
        for key, count in sorted(observed.items())
    ]
    report["master_crosscheck"], report["owner_decisions"] = _crosscheck(observed)
    dictionary = [leagues[key] for key in sorted(leagues)]
    if dictionary:
        from zucai.catalogue_mappings import crosscheck_official

        report["legacy_name_crosscheck"] = report["master_crosscheck"]
        report["legacy_owner_decisions"] = report["owner_decisions"]
        report["master_crosscheck"], report["owner_decisions"] = crosscheck_official(
            observed, dictionary, observed_ranges=report["observations"]
        )
    return dictionary


def _write_catalogue(folder: Path, report: dict, leagues: list[dict]) -> None:
    dictionary = dict(
        version=1,
        provider="jc",
        run_id=report["run_id"],
        captured_utc=report["captured_utc"],
        leagues=leagues,
    )
    if "replayed_utc" in report:
        dictionary["replayed_utc"] = report["replayed_utc"]
        dictionary["source_run_id"] = report["source_run_id"]
    _save_json(folder / "jc-leagues.json", dictionary)
    report["official_league_dictionary"] = dict(
        path=f"state/catalogue/{report['run_id']}/jc-leagues.json",
        sha256=hashlib.sha256((folder / "jc-leagues.json").read_bytes()).hexdigest(),
        count=len(leagues),
    )
    _save_json(folder / "jc.json", report)
    _save_json(folder / "summary.json", report)


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
    report = _new_report(run_id, start, end)

    def load_page(day: date, number: int) -> _ArchivedPage:
        report["requests"] += 1
        try:
            path = source.fetch_raw(day=day, page=number)
        except RequestException:
            # fetch_raw retains HTTP failures before raising; retain that same original.
            for saved in source.store.root.glob(f"{source.name}/*/results-{day}-p{number}_*.json"):
                if not saved.name.endswith(".meta.json"):
                    _preserve_page(source, saved, folder, day, number, require_success=False)
            raise
        return _preserve_page(source, path, folder, day, number)

    leagues = _aggregate(report, load_page, run_id)
    report["captured_utc"] = datetime.now(UTC).isoformat()
    _write_catalogue(folder, report, leagues)
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


def _read_replay_source(source: Path) -> tuple[dict, bytes, dict[tuple[date, int], _ArchivedPage]]:
    from zucai.cloud_feeds import CATALOGUE_RUN_ID

    if source.is_symlink() or any(path.is_symlink() for path in source.rglob("*")):
        raise ValueError("JC catalogue 重算来源不得包含符号链接")
    try:
        report_raw = (source / "jc.json").read_bytes()
        report = json.loads(report_raw)
        if not isinstance(report, dict) or report != json.loads(
            (source / "summary.json").read_bytes()
        ):
            raise ValueError("JC catalogue 原运行摘要不一致")
        if (
            not re.fullmatch(CATALOGUE_RUN_ID, source.name)
            or report.get("run_id") != source.name
            or report.get("provider") != "jc"
            or report.get("collection") != "catalogue"
            or report.get("status") not in {"partial", "complete"}
        ):
            raise ValueError("JC catalogue 原运行身份无效")
        started = _utc(report.get("started_utc"), "started_utc")
        captured = _utc(report.get("captured_utc"), "captured_utc")
        window = report["window"]
        start = date.fromisoformat(window["start_beijing"])
        end = date.fromisoformat(window["end_beijing"])
        if (
            started > captured
            or (start, end) != complete_month_window(started)
            or window
            != dict(
                start_beijing=start.isoformat(),
                end_beijing=end.isoformat(),
                basis="previous_12_complete_beijing_calendar_months",
            )
        ):
            raise ValueError("JC catalogue 原运行窗口无效")
        pages, by_day, actual_files = {}, {}, set()
        for path in sorted((source / "jc").rglob("*")):
            if path.is_dir():
                continue
            relative = path.relative_to(source).as_posix()
            actual_files.add(relative)
            if relative.endswith(".meta.json"):
                continue
            match = re.fullmatch(
                r"jc/(\d{4}-\d{2})/(\d{4}-\d{2}-\d{2})-p([1-9][0-9]*)\.json", relative
            )
            if match is None:
                raise ValueError("JC catalogue 原件路径无效或页码重复")
            day, number = date.fromisoformat(match[2]), int(match[3])
            if (
                day.strftime("%Y-%m") != match[1]
                or not start <= day <= end
                or (day, number) in pages
            ):
                raise ValueError("JC catalogue 原件日期或页码无效")
            archived = _archived_page(
                path.read_bytes(), path.with_suffix(".json.meta.json").read_bytes(), day, number
            )
            if (
                not started
                <= _utc(archived.metadata["fetched_at_utc"], "fetched_at_utc")
                <= captured
            ):
                raise ValueError("JC catalogue 原件捕获时间超出原运行")
            pages[day, number] = archived
            by_day.setdefault(day, {})[number] = archived
        expected_files = {
            page.relative_path + suffix for page in pages.values() for suffix in ("", ".meta.json")
        }
        if actual_files != expected_files or len(pages) != _integer(
            report.get("requests"), "requests", 1
        ):
            raise ValueError("JC catalogue 原件、sidecar 或请求页数不完整")
        day = start
        while day <= end:
            day_pages = by_day.get(day, {})
            if set(day_pages) != set(range(1, len(day_pages) + 1)):
                raise ValueError("JC catalogue 原运行缺页")
            parse_day_pages(day, [day_pages[number].raw for number in sorted(day_pages)])
            day += timedelta(days=1)
    except (OSError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError("JC catalogue 原运行归档不完整或结构无效") from exc
    return report, report_raw, pages


def replay_jc_catalogue(
    source_folder: Path, output_root: Path, run_id: str, *, now: datetime | None = None
) -> dict:
    """Recompute an immutable run offline, without opening a DB or publishing latest."""
    from zucai.cloud_feeds import CATALOGUE_RUN_ID

    if not isinstance(run_id, str) or not re.fullmatch(CATALOGUE_RUN_ID, run_id):
        raise ValueError("JC catalogue 重算 run_id 无效")
    source = Path(source_folder)
    output = Path(output_root)
    folder = output / "state/catalogue" / run_id
    if folder.exists() or output.resolve().is_relative_to(source.resolve()):
        raise ValueError("JC catalogue 重算必须使用独立的新运行目录")
    if any(path.is_symlink() for path in (output, folder, *folder.parents)):
        raise ValueError("JC catalogue 重算输出不得包含符号链接")
    original, original_raw, pages = _read_replay_source(source)
    instant = now if now is not None else datetime.now(UTC)
    replayed = _utc(instant.isoformat(), "replayed_utc")
    if replayed < _utc(original["captured_utc"], "captured_utc"):
        raise ValueError("JC catalogue 重算时间早于原件捕获时间")
    report = _new_report(
        run_id,
        date.fromisoformat(original["window"]["start_beijing"]),
        date.fromisoformat(original["window"]["end_beijing"]),
    )
    report.update(
        started_utc=original["started_utc"],
        captured_utc=original["captured_utc"],
        replayed_utc=replayed.isoformat(),
        source_run_id=original["run_id"],
        source_report_sha256=hashlib.sha256(original_raw).hexdigest(),
        replayed_pages=len(pages),
    )
    leagues = _aggregate(report, lambda day, number: pages[day, number], original["run_id"])
    if report["status"] != "complete":
        raise ValueError(f"JC catalogue 原运行无法完整重算: {report['errors']}")
    # All originals, their provenance and the complete year have passed validation.
    # The existing run, latest pointer and any database remain untouched.
    folder.mkdir(parents=True, exist_ok=False)
    for page in pages.values():
        _write_page(folder, page)
    _write_catalogue(folder, report, leagues)
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
