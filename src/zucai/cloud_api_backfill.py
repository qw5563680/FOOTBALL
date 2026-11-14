"""Verified API history: fdorg cups and APIF cups/domestic leagues, same import policy."""

import json
import logging
import os
import re
import sqlite3
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

from zucai.cloud_apis import failure_reason
from zucai.league_registry import provider_config
from zucai.request_budget import defer_scope
from zucai.sources.api_football import APIF_LEAGUE_IDS, LEAGUES, ApiFootball
from zucai.sources.apif_command import ALIASES as APIF_ALIASES
from zucai.sources.apif_command import fetch_apif
from zucai.sources.apif_ingest import COUNT_KEYS
from zucai.sources.base import RawStore, _atomic_write
from zucai.sources.cup_identity import (
    CUP_CODES,
    _persisted_bindings,
    alignment_counts,
    identity_capture,
    league_counts,
    resolve_cup_aliases,
)
from zucai.sources.fd_ingest import _canonical
from zucai.sources.fdorg_command import ALIASES as FDORG_ALIASES
from zucai.sources.fdorg_command import COMPETITIONS, fetch_fdorg
from zucai.sources.football_data import read_json
from zucai.sources.football_data_org import FootballDataOrg

LOG = logging.getLogger(__name__)
_DEFAULT_CONFIG_PATHS = {"apif": LEAGUES, "fdorg": COMPETITIONS}


def _config(provider):
    path = COMPETITIONS if provider == "fdorg" else LEAGUES
    if path != _DEFAULT_CONFIG_PATHS[provider]:
        return read_json(path)  # Explicit offline path injection retains its existing semantics.
    return provider_config(provider)


def _year(value, default):
    if value is None:
        return default
    if not re.fullmatch(r"20\d{2}", str(value)):
        raise ValueError("API 回填范围须为四位起始年份，例如 2023，不接受 2324 赛季代码")
    return int(value)


def api_backfill_scopes(provider, codes=None, first=None, last=None, *, today=None):
    if provider not in CUP_CODES:
        raise ValueError("API 回填来源仅支持 fdorg / apif")
    config = _config(provider)
    allowed_codes = (
        CUP_CODES[provider]
        if provider == "fdorg"
        else {
            str(code)
            for code in APIF_LEAGUE_IDS
            if str(code) in config and not config[str(code)].get("index_only", False)
        }
    )
    selected = list(CUP_CODES[provider]) if codes is None else [c.strip() for c in codes.split(",")]
    if (
        not selected
        or len(set(selected)) != len(selected)
        or any(c not in allowed_codes for c in selected)
    ):
        raise ValueError("仅回填 fdorg CL 或 apif 两项欧战与既有九联赛；不得回填其他赛事")
    today = today or datetime.now(UTC).date()
    scopes = []
    for code in sorted(selected):
        if provider == "fdorg":
            allowed = list(
                range(config[code]["earliest_season"], today.year - (today.month < 7) + 1)
            )
        else:
            allowed = (
                config[code].get("result_seasons_available", [])
                if config[code].get("require_result_permission")
                else config[code]["seasons_available"]
            )
        if not allowed:
            raise ValueError(f"{code} 尚缺实际赛果读取权限核验，不能按目录或身份年份回填")
        start, end = _year(first, min(allowed)), _year(last, max(allowed))
        if start > end or any(year not in allowed for year in range(start, end + 1)):
            raise ValueError("API 回填年份超出已核验可读范围，或 --from 晚于 --to")
        key = "competition" if provider == "fdorg" else "league_id"
        scopes.extend((code, {key: code, "season": year}) for year in range(start, end + 1))
    return scopes


def _scope_counts(database, provider, games):
    ids = sorted({f"{provider}:{g.provider_match_id}" for g in games})
    if not ids:
        return dict(matches=0, results=0)
    with sqlite3.connect(database) as con:
        matches, results = con.execute(
            "SELECT count(*),count(r.match_id) FROM matches m LEFT JOIN results r USING(match_id) "
            "WHERE m.match_id IN (" + ",".join("?" for _ in ids) + ")",
            ids,
        ).fetchone()
    return dict(matches=matches, results=results)


def _existing_apif_aliases(database, aliases, leagues):
    """Metrics only: count already-established bindings before this run's cup inference."""
    known = deepcopy(aliases)
    for league in leagues:
        entries = known.setdefault(league, {})
        for key, entry in _persisted_bindings(database, "apif", league).items():
            if not _canonical(known, league, key):
                entries[key] = entry
    return known


def collect_api_backfill(
    database: Path, run_id: str, *, provider, codes=None, first=None, last=None
):
    return collect_planned_api_backfill(
        database,
        run_id,
        provider=provider,
        scopes=api_backfill_scopes(provider, codes, first, last),
    )


def collect_planned_api_backfill(
    database: Path,
    run_id: str,
    *,
    provider,
    scopes,
    source=None,
    raw_root=None,
    status_probe=True,
):
    """Shared importer; the caller must validate the approved plan before constructing a source."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)*", run_id):
        raise ValueError("无效 run ID")
    before = league_counts(database, provider)
    raw = Path(raw_root) if raw_root is not None else database.parent / "raw"
    audit = database.parent / "state/audit"
    store = RawStore(raw)
    source = source or (
        FootballDataOrg(store=store)
        if provider == "fdorg"
        else ApiFootball(
            store=store,
            leagues=_config("apif"),
            quota_path=database.parent / "state/api_football_quota.json",
            reserve=10,
        )
    )
    report = dict.fromkeys(COUNT_KEYS, 0)
    report.update(
        source=provider,
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
    names = {code: config["league"] for code, config in _config(provider).items()}
    captures, pending = [], []

    def failed(item, exc):
        reason = failure_reason(exc)
        item.update(status="partial", error=reason)
        report["status"] = "partial"
        report["failed_scopes"].append(dict(scope=item["scope"], reason=reason))
        report["errors"].append(f"{item['scope']}: {reason}")
        LOG.warning("%s 回填 %s：%s", provider, item["scope"], reason)

    try:
        if provider == "apif":
            from zucai.apif_identity_metrics import (
                jc_links,
                jc_window_metrics,
                record_matches,
                record_scope,
            )

            prior_links = jc_links(database)
            report.update(new_linked_jc=0, isolated_2026=0)
            initial_aliases = read_json(APIF_ALIASES)
            from zucai.sources.apif_aliases import validate_approvals

            validate_approvals(initial_aliases, require_metadata=True)
            before_aliases = _existing_apif_aliases(
                database, initial_aliases, {names[code] for code, _ in scopes}
            )
            report["status_requests"] = 0
            try:
                if status_probe and source.quota._read()["status_free"] is True:
                    source.status()
            except Exception as exc:
                report["status"] = "partial"
                report["errors"].append("status: " + failure_reason(exc))
            finally:
                report["status_requests"] = source.request_count
        # Gather every requested scope before resolving identity. Current-season JC
        # witnesses can then identify the same provider IDs in earlier seasons.
        for code, scope in scopes:
            item = dict(
                code=code,
                league=names[code],
                season=scope["season"],
                scope=f"{code}/{scope['season']}",
                status="complete",
            )
            report["scopes"].append(item)
            budget = getattr(source, "request_budget", None)
            request_start = budget.used if budget else source.request_count
            if provider == "apif":
                record_scope(audit / "apif_identity_scopes.csv", code, names[code], scope["season"])
            try:
                path = source.fetch_raw(**scope)
                meta = store.load_metadata(path)
                item["raw"] = {**meta, "path": path.relative_to(raw).as_posix()}
                report["downloaded_files"] += 1
                payload = path.read_bytes()
                games = source.parse(payload, **scope)
                capture = identity_capture(
                    games,
                    source.rejected,
                    scope,
                    payload,
                    meta,
                    path.with_suffix(path.suffix + ".meta.json"),
                )
                captures.append(capture)
                item["parsed_matches"] = len(games)
                if provider == "apif":
                    prior = alignment_counts(games, before_aliases).get(item["league"], {})
                    item.update({f"{key}_before": value for key, value in prior.items()})
                item.update(
                    {
                        f"{key}_before": value
                        for key, value in _scope_counts(database, provider, games).items()
                    }
                )
                pending.append((scope, path, capture, item))
            except Exception as exc:
                requests_now = budget.used if budget else source.request_count
                if not defer_scope(
                    report, item, exc, requests_started=requests_now > request_start
                ):
                    failed(item, exc)
        aliases = read_json(FDORG_ALIASES) if provider == "fdorg" else initial_aliases
        if provider == "apif":
            record_matches(audit / "apif_identity_matches.csv", captures)
        try:
            aliases = resolve_cup_aliases(provider, database, aliases, captures, audit)
        except Exception as exc:
            # A contradictory identity never falls back to an unverified import.
            for _, _, _, item in pending:
                failed(item, exc)
            pending = []
        runtime_aliases = raw / provider / f"cup-aliases-{run_id}.json"
        runtime_aliases.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(runtime_aliases, json.dumps(aliases, ensure_ascii=False, indent=2).encode())
        for scope, path, capture, item in pending:
            try:
                common = dict(
                    seasons=[scope["season"]],
                    assume=timedelta(hours=3),
                    db=database,
                    aliases_path=runtime_aliases,
                    raw_file=path,
                    raw_root=raw,
                    audit_dir=audit,
                )
                result = (
                    fetch_fdorg(competition=scope["competition"], **common)
                    if provider == "fdorg"
                    else fetch_apif(league_id=scope["league_id"], **common)
                )
                report["processed_files"] += 1
                for key in COUNT_KEYS:
                    item[key] = result.get(key, 0)
                    report[key] += item[key]
                item.update(
                    {
                        f"{key}_after": value
                        for key, value in _scope_counts(
                            database, provider, capture["games"]
                        ).items()
                    }
                )
                item.update(
                    alignment_counts(capture["games"], aliases).get(
                        item["league"],
                        dict(teams=0, aligned_teams=0, unmatched_teams=0, alignment_rate=None),
                    )
                )
                if provider == "apif":
                    scope_ids = {f"apif:{g.provider_match_id}" for g in capture["games"]}
                    item["new_linked_jc"] = len((jc_links(database) - prior_links) & scope_ids)
                    item["isolated_2026"] = (
                        len(
                            {
                                str(row["provider_match_id"])
                                for file in result.get("files", [])
                                for row in file.get("suspected", [])
                            }
                        )
                        if scope["season"] == 2026
                        else 0
                    )
                    item.update(jc_window_metrics(database, capture["games"], aliases))
                    report["new_linked_jc"] = len(jc_links(database) - prior_links)
                    report["isolated_2026"] += item["isolated_2026"]
                    item.update(
                        {
                            f"{key}_after": item[key]
                            for key in (
                                "teams",
                                "aligned_teams",
                                "unmatched_teams",
                                "alignment_rate",
                            )
                        }
                    )
                    item.setdefault("alignment_rate_before", None)
                if result["rejected_rows"] or result["suspected_duplicates"]:
                    item["status"] = report["status"] = "partial"
                    report["errors"].append(
                        f"{item['scope']}: 单行拒绝或疑似重复，详见 state/audit"
                    )
                elif result.get("held_unbound"):
                    item["status"] = "held"
                    item["reason"] = "held_unbound"
                LOG.info(
                    "%s 回填 %s：新增%d，链接竞彩%d，拒绝%d，可疑%d",
                    provider,
                    item["scope"],
                    item["inserted"],
                    item["linked_existing"] + item.get("linked_late_jc", 0),
                    item["rejected_rows"],
                    item["suspected_duplicates"],
                )
            except Exception as exc:
                failed(item, exc)
    finally:
        source.session.close()
        report.update(
            requests=source.request_count - report.get("status_requests", 0),
            remaining_quota=source.remaining_quota,
        )
        if provider == "apif":
            try:
                report.update(source.quota.describe())
            except ValueError as exc:
                report["status"] = "partial"
                report["errors"].append("quota: " + failure_reason(exc))
    after = league_counts(database, provider)
    alignment = alignment_counts([g for capture in captures for g in capture["games"]], aliases)
    report["leagues"] = {
        code: dict(
            league=league,
            matches_before=before.get(league, {}).get("matches", 0),
            results_before=before.get(league, {}).get("results", 0),
            matches_after=after.get(league, {}).get("matches", 0),
            results_after=after.get(league, {}).get("results", 0),
            **alignment.get(league, {}),
        )
        for code, league in names.items()
        if any(c == code for c, _ in scopes)
    }
    if provider == "apif":
        prior = alignment_counts(
            [g for capture in captures for g in capture["games"]], before_aliases
        )
        for stats in report["leagues"].values():
            stats["alignment_rate_before"] = prior.get(stats["league"], {}).get("alignment_rate")
            stats["alignment_rate_after"] = stats.get("alignment_rate")
    store.save(
        source.name,
        f"backfill-{run_id}",
        json.dumps(report, ensure_ascii=False, indent=2).encode(),
        "json",
    )
    return report


def write_summary(report):
    if not os.environ.get("GITHUB_STEP_SUMMARY"):
        return
    paid = report["source"] == "apif"
    title = "欧战及九联赛历史回填" if paid else "欧战历史回填"
    alignment_label = "球队对齐率（前→后）" if paid else "球队对齐率"
    lines = [
        f"### {report['source']} {title}：{report['status']}",
        "",
        f"请求 {report['requests']} 次（含失败与分页），剩余配额 {report['remaining_quota']}；"
        f"新增 {report['inserted']}，拒绝 {report['rejected_rows']}，"
        f"疑似重复 {report['suspected_duplicates']}。",
        f"未绑定暂缓 {report.get('held_unbound', 0)}；"
        f"未请求待续范围 {len(report.get('deferred_scopes', []))}；"
        f"失败范围 {len(report.get('failed_scopes', []))}。",
        "",
        "| 赛事/赛季 | 状态 | 解析 | 库内比赛 | 库内赛果 | 本次新增 | 链接竞彩 | "
        f"拒绝 | 疑似重复 | {alignment_label} | 未对齐球队 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in report["scopes"]:
        rate = item.get("alignment_rate")
        rate_text = f"{rate:.1%}" if rate is not None else "—"
        if paid:
            prior = item.get("alignment_rate_before")
            rate_text = (f"{prior:.1%}" if prior is not None else "—") + " → " + rate_text
        cells = [
            item["league"] + "/" + str(item["season"]),
            item["status"],
            *(
                str(item.get(k, "—"))
                for k in (
                    "parsed_matches",
                    "matches_after",
                    "results_after",
                    "inserted",
                )
            ),
            str(item.get("linked_existing", 0) + item.get("linked_late_jc", 0)),
            str(item.get("rejected_rows", "—")),
            str(item.get("suspected_duplicates", "—")),
            rate_text,
            str(item.get("unmatched_teams", "—")),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines += [
        "",
        "库内比赛/赛果仅计该来源行；链接竞彩单列，不覆盖竞彩赛果。对齐率按该范围不同球队 ID 统计。",
        "原件、SHA256 和完整记录在 Actions artifact；身份依据和拒绝记录在 state/audit。",
    ]
    if paid:
        lines += [
            "",
            f"本轮新链接到 jc：{report.get('new_linked_jc', 0)}；"
            f"2026 季本次仍隔离：{report.get('isolated_2026', 0)}。",
            "",
            "| 赛事/赛季 | 新链接 jc | 2026 隔离 | jc 窗口候选球队对齐率 |",
            "| --- | ---: | ---: | ---: |",
        ]
        for item in report["scopes"]:
            rate = item.get("jc_window_alignment_rate")
            text = (
                f"{rate:.1%} ({item['jc_window_aligned_teams']}/{item['jc_window_teams']})"
                if rate is not None
                else "—"
            )
            lines.append(
                f"| {item['scope']} | {item.get('new_linked_jc', '—')} | "
                f"{item.get('isolated_2026', '—')} | {text} |"
            )
        lines += [
            "",
            "jc 窗口口径：同联赛 ±3h 内所有 APIF 候选球队，"
            "未确认的同时开球球队也计入分母；不能当成已确认在售球队名单。",
        ]
    lines.extend(
        "- " + message.replace("\n", " ").replace("|", "\\|") for message in report["errors"]
    )
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as output:
        if paid:
            from zucai.cloud_feeds import write_apif_quota

            write_apif_quota(output, report)
        output.write("\n".join(lines) + "\n")
