"""Hosted API collection; existing adapters own parsing, identity and import policy."""

import json
import logging
import re
from datetime import UTC, timedelta
from pathlib import Path

from zucai.sources.apif_command import fetch_apif
from zucai.sources.apif_quota import DailyQuota
from zucai.sources.base import RawStore, _atomic_write
from zucai.sources.fd_ingest import fetch_fd
from zucai.sources.fdorg_command import COMPETITIONS, _requests, fetch_fdorg
from zucai.sources.football_data import LEAGUES_PATH, read_json, verified_league
from zucai.sources.football_data_org import FootballDataOrg

LOG = logging.getLogger(__name__)
COMPETITION_CODES = "PL,ELC,PD,SA,BL1,FL1,DED,PPL,BSA"


def fd_due(now, slot):
    return now.astimezone(UTC).weekday() == 0 and slot == "early"


def fd_scopes(today, leagues):
    """European seasons start in July; configured country feeds use calendar years."""
    start = today.year - (today.month < 7)
    for code, config in sorted(leagues.items()):
        if config.get("status") != "verified":
            continue
        verified_league(leagues, code)
        if config.get("publisher_country"):
            year = int(config.get("current_season", today.year))
            yield code, dict(country=code, from_season=year, to_season=year)
        else:
            season = config.get("current_season", f"{start % 100:02d}{(start + 1) % 100:02d}")
            yield code, dict(div=code, seasons=[season])


def failure_reason(exc):
    """Bounded diagnostics: never emit a transport exception, body or credentials."""
    message = str(exc)
    status = re.search(r"HTTP\s+(\d{3})\b", message)
    if status:
        return "HTTP " + status[1]
    if "配额" in message or "reserve" in message:
        return "配额不足或配额状态无效"
    if "请设置有效" in message:
        return "缺少有效的环境凭据"
    if "网络" in message or isinstance(exc, (ConnectionError, TimeoutError)):
        return "网络请求失败"
    return "响应、配置或导入校验失败 (" + type(exc).__name__ + ")"


def _record(report, result):
    for key, value in result.items():
        if key == "remaining_quota":
            report[key] = value
        elif type(value) is int:
            report[key] = report.get(key, 0) + value
    if result.get("rejected_rows") or result.get("suspected_duplicates"):
        report["status"] = "partial"
        note = "单行拒绝或疑似重复，详见 state/audit"
        if note not in report["errors"]:
            report["errors"].append(note)


def _failed(report, scope, exc):
    reason = failure_reason(exc)
    report["status"] = "partial"
    report["errors"].append(f"{scope}: {reason}")
    LOG.warning("%s: %s", scope, reason)


def collect_apis(database: Path, now, days=3, slot="auto"):
    from zucai.cloud_aliases import write_candidates

    now = now.astimezone(UTC)
    today = now.date()
    if slot == "auto":
        slot = "early" if now.hour < 9 else "late"
    state = database.parent / "state"
    audit = state / "audit"
    raw = database.parent / "raw"
    quota = state / "api_football_quota.json"
    reports = {
        source: dict(
            status="complete",
            inserted=0,
            linked_existing=0,
            linked_fd=0,
            linked_fdorg=0,
            rejected_rows=0,
            requests=0,
            errors=[],
        )
        for source in ("fd", "fdorg", "apif")
    }
    captures = {"fdorg": [], "apif": []}
    reports["fd"].pop("requests")
    reports["fd"]["downloaded_files"] = 0
    common = dict(db=database, audit_dir=audit, raw_root=raw)

    # Higher-priority FD is imported first; a bad league does not stop other scopes.
    if fd_due(now, slot):
        try:
            scopes = list(fd_scopes(today, read_json(LEAGUES_PATH)))
        except Exception as exc:
            scopes = []
            _failed(reports["fd"], "configuration", exc)
        for code, scope in scopes:
            try:
                _record(
                    reports["fd"],
                    fetch_fd(
                        **scope, assume=timedelta(hours=3), leagues_path=LEAGUES_PATH, **common
                    ),
                )
            except Exception as exc:
                _failed(reports["fd"], code, exc)
    else:
        reports["fd"].update(status="skipped", reason="仅 UTC 周一早班采集")

    source = None
    try:
        source = FootballDataOrg(store=RawStore(raw))
        scopes = _requests(
            COMPETITION_CODES, None, days, None, False, today, read_json(COMPETITIONS)
        )
        for scope in scopes:
            try:
                path = source.fetch_raw(**scope)
                meta = source.store.load_metadata(path)
                meta["window_anchor_date"] = today.isoformat()
                _atomic_write(
                    path.with_suffix(path.suffix + ".meta.json"), json.dumps(meta).encode()
                )
                result = fetch_fdorg(
                    competition=COMPETITION_CODES, days=days, raw_file=path, **common
                )
                _record(reports["fdorg"], result)
                captures["fdorg"].extend(result["files"])
            except Exception as exc:
                _failed(reports["fdorg"], scope["date_from"], exc)
    except Exception as exc:
        _failed(reports["fdorg"], "configuration", exc)
    finally:
        if source is not None:
            source.session.close()
            reports["fdorg"].update(
                requests=source.request_count, remaining_quota=source.remaining_quota
            )

    budget = DailyQuota(quota)
    reports["apif"]["remaining_quota"] = None
    for offset in range(3):
        day = (today - timedelta(days=offset)).isoformat()
        try:
            before = budget._read()
        except Exception as exc:
            _failed(reports["apif"], "quota", exc)
            break
        try:
            result = fetch_apif(date=day, quota_path=quota, **common)
            _record(reports["apif"], result)
            captures["apif"].extend(result["files"])
        except Exception as exc:
            _failed(reports["apif"], day, exc)
            saved = budget._read()
            reports["apif"]["requests"] += saved["used"] - (
                before["used"] if saved["date_utc"] == before["date_utc"] else 0
            )
        reports["apif"]["remaining_quota"] = budget._read()["remaining"]

    for provider in ("fdorg", "apif"):
        try:
            reports[provider]["alias_candidates"] = write_candidates(
                provider, database, raw, captures[provider], state / "alias_candidates"
            )
        except Exception as exc:
            _failed(reports[provider], "alias verification", exc)
    return reports
