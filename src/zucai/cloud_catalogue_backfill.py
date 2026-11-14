"""Reviewed catalogue plans, bounded downloads and durable success checkpoints."""

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from zucai.cloud_api_backfill import collect_planned_api_backfill
from zucai.cloud_backfill import collect_backfill
from zucai.cloud_probe import require_probe_actions
from zucai.league_registry import load_registry, provider_config
from zucai.sources.api_football import ApiFootball
from zucai.sources.apif_ingest import COUNT_KEYS
from zucai.sources.base import RawStore, _atomic_write
from zucai.sources.football_data import FootballDataCoUk
from zucai.sources.football_data_org import FootballDataOrg


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _safe_path(root, relative):
    path = root / relative
    if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != root.parent):
        raise ValueError("目录证据路径不得为符号链接")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("目录证据路径越界")
    return path


def _catalogue(root, provider):
    latest = json.loads(_safe_path(root, "state/catalogue/latest.json").read_bytes())
    pointer = latest.get("providers", {}).get(provider, {})
    relative = pointer.get("path", "")
    if latest.get("version") != 1 or not re.fullmatch(
        rf"state/catalogue/[A-Za-z0-9][A-Za-z0-9_-]{{0,99}}/{provider}\.json", relative
    ):
        raise ValueError("缺少已归档且可追溯的接口目录")
    payload = _safe_path(root, relative).read_bytes()
    if _sha(payload) != pointer.get("sha256"):
        raise ValueError("目录 SHA 不一致")
    doc = json.loads(payload)
    if doc.get("version") != 1 or doc.get("provider") != provider:
        raise ValueError("目录来源或版本不一致")
    rows, by_code = doc.get("leagues"), {}
    if not isinstance(rows, list):
        raise ValueError("目录缺少赛事列表")
    for row in rows:
        code = row.get("code") if provider == "fdorg" else row.get("id")
        # The full FDORG directory legitimately includes entries with no code.
        # Retain them in the catalogue, but do not let an unrelated unrouteable
        # row prevent downloads of reviewed code-based routes.
        if provider == "fdorg" and code is None:
            continue
        if not isinstance(code, str) or not code or code in by_code:
            raise ValueError("目录赛事ID缺失或重复")
        by_code[code] = row
    return by_code, pointer


def _years(row):
    result, seen = set(), set()
    if not isinstance(row.get("seasons"), list):
        raise ValueError("目录缺少赛季列表")
    for season in row["seasons"]:
        year = season.get("year")
        if type(year) is not int or not 1900 <= year <= 9999 or year in seen:
            raise ValueError("目录赛季无效或重复")
        seen.add(year)
        # Explicit absence is different from missing coverage metadata.
        if season.get("fixtures") is False and season.get("results") is False:
            continue
        if season.get("status") == "conflict" or season.get("access") in {"denied", "conflict"}:
            continue
        result.add(year)
    return result


def catalogue_scopes(root, provider, codes=None, first=None, last=None, *, registry=None):
    """No network. An approved mapping is necessary even when scope=all."""
    registry = load_registry() if registry is None else registry
    if provider not in {"apif", "fdorg", "fd"}:
        raise ValueError("目录回填仅支持 apif/fdorg/fd")
    entries = registry["providers"][provider]
    selected = list(entries) if codes is None else [c.strip() for c in codes.split(",")]
    eligible = {
        code: entry
        for code, entry in entries.items()
        if entry["status"] == "verified"
        and entry["mode"] == "import"
        and entry["backfill"]
        and (entry["relevant"] or registry["download_policy"]["scope"] == "all")
    }
    if codes is None:
        selected = list(eligible)
    if (
        not selected
        or len(selected) != len(set(selected))
        or any(c not in eligible for c in selected)
    ):
        raise ValueError("仅允许 verified 且已核准回填的赛事；候选/身份/索引不能下载")
    bounds = []
    for value in (first, last):
        if value is not None and not re.fullmatch(r"[12]\d{3}", str(value)):
            raise ValueError("赛季边界须为四位起始年")
        bounds.append(int(value) if value is not None else None)
    start, end = bounds
    if start is not None and end is not None and start > end:
        raise ValueError("赛季起点晚于终点")
    catalogue, pointer = _catalogue(Path(root), provider)
    scopes, missing, excluded = [], [], []
    for code in selected:
        row, config = catalogue.get(code), eligible[code]["config"]
        if row is None:
            missing.append(dict(code=code, reason="最新目录没有该已核准ID；尚不能认定接口无"))
            continue
        if config.get("api_name") and config["api_name"] != row.get("name"):
            raise ValueError(f"{code} 目录名称与已核准映射不符，须人工核对")
        years = _years(row)
        if provider == "apif" and config.get("require_result_permission"):
            years &= set(config.get("result_seasons_available", []))

        excluded.extend(
            dict(code=code, season=season["year"], reason="目录同年记录冲突；禁止回填")
            for season in row["seasons"]
            if season.get("status") == "conflict" or season.get("access") == "conflict"
        )
        # /competitions gives currentSeason only. Add only prior successful probes,
        # never infer years from numberOfAvailableSeasons or earliest_season.
        if provider == "fdorg":
            years |= {
                int(year)
                for year, proof in config.get("season_probes", {}).items()
                if proof.get("accessible") is True
                and proof.get("http") == 200
                and proof.get("raw_sha256")
                and re.fullmatch(r"[12]\d{3}", year)
            }
        chosen = sorted(
            y for y in years if (start is None or y >= start) and (end is None or y <= end)
        )
        if not chosen:
            if first is not None or last is not None:
                raise ValueError(f"{code} 请求赛季没有已列出的目录/历史核验依据")
            missing.append(dict(code=code, reason="目录未给出可追溯年份；待核实，不猜测历史范围"))
            continue
        if provider == "fd" and config.get("publisher_country"):
            # One publisher-country CSV contains all seasons; filter the exact year set
            # after verifying it is contiguous (legacy importer takes an inclusive range).
            if chosen != list(range(chosen[0], chosen[-1] + 1)):
                raise ValueError("FD合并CSV年份不连续，须分次明确选取，不能隐含导入空档年份")
            scopes.append((code, dict(country=code, from_season=chosen[0], to_season=chosen[-1])))
        else:
            for year in chosen:
                scope = (
                    dict(div=code, seasons=[f"{year % 100:02d}{(year + 1) % 100:02d}"])
                    if provider == "fd"
                    else {
                        "league_id" if provider == "apif" else "competition": code,
                        "season": year,
                    }
                )
                scopes.append((code, scope))
    return dict(
        provider=provider,
        scopes=scopes,
        missing=missing,
        excluded=excluded,
        catalogue_sha256=pointer["sha256"],
        catalogue_path=pointer["path"],
        registry_sha256=_sha(_bytes(registry)),
        access_is_verified=False,
    )


class RequestBudget:
    def __init__(self, limit):
        if type(limit) is not int or limit < 1:
            raise ValueError("请求上限须为正整数")
        self.limit, self.used = limit, 0

    def check(self):
        if self.used >= self.limit:
            raise ValueError("已达本次请求上限；已完成部分保留，下次可继续")

    def call(self, function, *args, **kwargs):
        self.check()
        self.used += 1
        return function(*args, **kwargs)


class _Session:
    def __init__(self, session, budget):
        self._session, self.budget = session, budget

    def get(self, *args, **kwargs):
        return self.budget.call(self._session.get, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._session, name)


def _bind_budget(source, budget, provider):
    source.session = _Session(source.session, budget)
    # Check before the APIF quota is reserved; exhausted jobs must not consume
    # local daily credit without issuing a request.
    if provider == "apif":
        original = source._request

        def request(*args, **kwargs):
            budget.check()
            return original(*args, **kwargs)

        source._request = request


def _checkpoint_key(provider, code, scope, registry):
    return _sha(
        _bytes(
            dict(
                provider=provider,
                code=code,
                scope=scope,
                mapping=registry["providers"][provider][code],
            )
        )
    )


def collect_catalogue_backfill(
    database: Path,
    run_id: str,
    *,
    provider,
    codes=None,
    first=None,
    last=None,
    max_requests=500,
    resume=True,
):
    require_probe_actions()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", run_id):
        raise ValueError("无效 run ID")
    root, registry = database.parent, load_registry()
    plan = catalogue_scopes(root, provider, codes, first, last, registry=registry)
    requested = RequestBudget(max_requests).limit
    budget = RequestBudget(min(requested, registry["download_policy"]["max_requests"]))
    folder = _safe_path(root, f"state/catalogue/{run_id}")
    folder.mkdir(parents=True, exist_ok=False)
    checkpoint_path = _safe_path(root, "state/catalogue/backfill-checkpoint.json")
    checkpoint = (
        json.loads(checkpoint_path.read_bytes())
        if checkpoint_path.exists()
        else dict(version=1, completed={})
    )
    if checkpoint.get("version") != 1 or not isinstance(checkpoint.get("completed"), dict):
        raise ValueError("回填游标损坏，不能假定空进度")
    keys = {
        _checkpoint_key(provider, code, scope, registry): (code, scope)
        for code, scope in plan["scopes"]
    }
    completed = checkpoint["completed"]
    if resume:
        for key in set(keys) & completed.keys():
            proof = completed[key]
            if not isinstance(proof, dict) or not isinstance(proof.get("raw_path"), str):
                raise ValueError("回填游标缺少原件证据，不能跳过")
            relative = proof["raw_path"]
            if not relative.startswith("state/catalogue/") or "/backfill/" not in relative:
                raise ValueError("回填游标原件路径无效")
            original = _safe_path(root, relative)
            if not original.is_file() or _sha(original.read_bytes()) != proof.get("raw_sha256"):
                raise ValueError("回填游标原件缺失或SHA不符，不能跳过")
    pending = [(key, pair) for key, pair in keys.items() if not (resume and key in completed)]
    skipped = len(keys) - len(pending)
    raw = folder / "backfill"
    report = dict.fromkeys(COUNT_KEYS, 0)
    report.update(
        status="complete",
        source=provider,
        operation="catalogue-backfill",
        run_id=run_id,
        scopes=[],
        failed_scopes=[],
        errors=[],
        downloaded_files=0,
        processed_files=0,
    )
    if pending:
        configs, store = provider_config(provider), RawStore(raw)
        if provider == "apif":
            source = ApiFootball(
                store=store,
                leagues=configs,
                quota_path=root / "state/api_football_quota.json",
                reserve=registry["download_policy"]["daily_reserve"],
            )
        elif provider == "fdorg":
            source = FootballDataOrg(store=store, competitions=configs)
        else:
            source = FootballDataCoUk(store=store, leagues=configs)
        _bind_budget(source, budget, provider)
        scopes = [pair for _, pair in pending]
        if provider == "fd":
            report = collect_backfill(
                database, run_id, _scopes=scopes, _source=source, _raw_root=raw
            )
        else:
            report = collect_planned_api_backfill(
                database,
                run_id,
                provider=provider,
                scopes=scopes,
                source=source,
                raw_root=raw,
                status_probe=False,
            )
        # Importers write only after parsing. Checkpoint only completed imports;
        # partial/rejected/ambiguous scopes must remain retryable and inspectable.
        for (key, _), item in zip(pending, report["scopes"], strict=True):
            if item["status"] == "complete" and item.get(
                "processed_files", report["processed_files"]
            ):
                evidence = item.get("raw", {})
                completed[key] = dict(
                    run_id=run_id,
                    raw_sha256=evidence.get("sha256"),
                    raw_path=(raw / evidence["path"]).relative_to(root).as_posix(),
                    catalogue_sha256=plan["catalogue_sha256"],
                    scope=item["scope"],
                    provider=provider,
                )
        _atomic_write(checkpoint_path, _bytes(checkpoint))
    now = datetime.now(UTC)
    report.update(
        source=provider,
        operation="catalogue-backfill",
        requests=budget.used,
        max_requests=budget.limit,
        requested_max_requests=requested,
        daily_reserve=registry["download_policy"]["daily_reserve"],
        resumed_scopes=skipped,
        planned_scopes=len(keys),
        missing=plan["missing"],
        catalogue_sha256=plan["catalogue_sha256"],
        registry_sha256=plan["registry_sha256"],
        captured_utc=now.isoformat(),
        captured_beijing=now.astimezone(ZoneInfo("Asia/Shanghai")).isoformat(),
        checkpoint="state/catalogue/backfill-checkpoint.json",
    )
    if plan["missing"]:
        report["status"] = "partial"
    _atomic_write(folder / "backfill.json", _bytes(report))
    return report


def write_summary(report):
    destination = os.environ.get("GITHUB_STEP_SUMMARY")
    if destination:
        with Path(destination).open("a", encoding="utf-8") as stream:
            stream.write(
                f"### 目录历史回填：{report['status']}\n\n"
                f"请求 {report['requests']}/{report['max_requests']}，"
                f"本次新增 {report['inserted']}，续跑跳过 {report['resumed_scopes']} 个已完成范围。"
                f"每日保留 {report['daily_reserve']}。\n\n"
                "目录列出不等于账号可读；失败、缺年份与原件见 state/catalogue 本批记录。\n"
            )
