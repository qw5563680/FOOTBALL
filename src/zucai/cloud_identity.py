"""Reviewer-triggered, identity-only season captures. Never import matches or aliases."""

import json
import logging
import re
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime

from zucai.apif_identity_metrics import record_matches, record_scope
from zucai.cloud_aliases import _validate_candidates
from zucai.cloud_apis import failure_reason
from zucai.cloud_identity_probe import identity_codes, require_confirmed_leagues
from zucai.cloud_probe import append_csv, require_probe_actions
from zucai.identity_evidence import read_counterparts
from zucai.sources.api_football import ApiFootball
from zucai.sources.apif_aliases import validate_approvals
from zucai.sources.apif_command import ALIASES
from zucai.sources.apif_verify import infer_aliases
from zucai.sources.base import RawStore, _atomic_write
from zucai.sources.cup_identity import _covered
from zucai.sources.fd_ingest import WINDOW, _canonical
from zucai.sources.fd_verify import _utc
from zucai.sources.football_data import CONFIG_ROOT, read_json

LOG = logging.getLogger(__name__)
FD_LEAGUES = CONFIG_ROOT / "football_data_co_uk/leagues.json"
FD_ALIASES = CONFIG_ROOT / "team_aliases/football_data_co_uk.json"
FIELDS = (
    "run_id",
    "league_id",
    "league",
    "team_id",
    "name",
    "country",
    "canonical",
    "status",
    "appearances_2025",
    "appearances_2026",
    "jc_references",
    "reason",
    "evidence",
)


def identity_scopes(codes=None, seasons=None):
    selected = identity_codes(codes)
    seasons = "2025,2026" if seasons is None else seasons
    if seasons == "2025-2026":
        seasons = "2025,2026"
    if not re.fullmatch(r"202[56](?:,202[56])*", seasons):
        raise ValueError("identity 仅核验 2025、2026 两季")
    years = [int(year) for year in seasons.split(",")]
    if len(years) != len(set(years)):
        raise ValueError("identity 年份不得重复")
    return selected, years


def validate_global_ids(aliases, candidates):
    known = {}
    for groups in (aliases, candidates):
        for league, entries in groups.items():
            for key in entries:
                canonical = _canonical(groups, league, key)
                if canonical:
                    if key in known and known[key] != canonical:
                        raise ValueError("同一 APIF 球队 ID 跨联赛规范名冲突，须人工核验")
                    known[key] = canonical


def collect_identity(database, run_id, *, codes=None, seasons=None):
    require_probe_actions()
    selected, years = identity_scopes(codes, seasons)
    proof = require_confirmed_leagues(database, list(selected))
    state = database.parent / "state"
    target = state / "alias_candidates/apif.json"
    candidates = read_json(target) if target.exists() else {}
    aliases = read_json(ALIASES)
    validate_approvals(aliases, require_metadata=True)
    validate_global_ids(aliases, candidates)
    original_candidates, all_audit = deepcopy(candidates), []
    client = ApiFootball(
        store=RawStore(database.parent / "raw"),
        quota_path=state / "api_football_quota.json",
        identity_only=True,
    )
    report = dict(
        status="complete", requests=0, status_requests=0, auto_verified=0, errors=[], leagues={}
    )
    try:
        for code, config in selected.items():
            league = config["league"]
            games, provenance, intervals = {}, {}, []
            appearances = {year: Counter() for year in (2025, 2026)}
            inconsistent = False
            for year in years:
                record_scope(state / "audit/apif_identity_scopes.csv", code, league, year)
                try:
                    scope = dict(league_id=code, season=year)
                    path = client._request(client.request_url(**scope))
                    raw = path.read_bytes()
                    doc = client._document(raw, client._scope(**scope))
                    if doc["paging"] != {"current": 1, "total": 1}:
                        raise ValueError("identity 须完整单页；不追加分页或使用不完整证据")
                    parsed = client.parse(raw, **scope)
                    if client.rejected:
                        raise ValueError("identity 整季存在解析拒绝，不作为身份依据")
                    meta = client.store.load_metadata(path)
                    record_matches(
                        state / "audit/apif_identity_matches.csv",
                        [dict(games=parsed, scope=scope, complete=True, raw_sha256=meta["sha256"])],
                    )
                    # Stage and check the full scope before adding any of its games.
                    for game in parsed:
                        previous = games.get(game.provider_match_id)
                        if previous and (
                            previous.home_provider_id,
                            previous.away_provider_id,
                            previous.kickoff_utc,
                        ) != (game.home_provider_id, game.away_provider_id, game.kickoff_utc):
                            inconsistent = True
                            raise ValueError("identity 跨季比赛 ID 身份冲突")
                    for game in parsed:
                        games[game.provider_match_id] = game
                        provenance[game.provider_match_id] = dict(
                            raw_sha256=meta["sha256"],
                            sidecar=path.with_suffix(path.suffix + ".meta.json")
                            .relative_to(database.parent / "raw")
                            .as_posix(),
                            metadata_sha256=proof[code]["raw_sha256"],
                        )
                        appearances[year].update(
                            (str(game.home_provider_id), str(game.away_provider_id))
                        )
                    intervals.append(
                        (datetime(year, 7, 1, tzinfo=UTC), datetime(year + 1, 7, 1, tzinfo=UTC))
                    )
                except Exception as exc:
                    report["status"] = "partial"
                    report["errors"].append(f"{code}/{year}: {failure_reason(exc)}")
            before = deepcopy(candidates)
            try:
                if inconsistent:
                    raise ValueError("identity 联赛有矛盾证据，停止本联赛核验")
                official, fd_proof = read_counterparts(database, config, FD_LEAGUES, FD_ALIASES)
                relevant = [
                    row
                    for row in official
                    if any(
                        abs(_utc(row["kickoff_utc"]) - game.kickoff_utc) <= WINDOW
                        for game in games.values()
                    )
                ]
                if any(
                    not _covered(_utc(row["kickoff_utc"]), sorted(intervals)) for row in relevant
                ):
                    raise ValueError("identity 对照比赛三小时邻域未完整覆盖")
                for info in provenance.values():
                    info.update(fd_proof)
                entries = infer_aliases(
                    list(games.values()),
                    relevant,
                    aliases=aliases.get(league, {}),
                    provenance=provenance,
                )
                labels = {
                    str(key): name
                    for game in games.values()
                    for key, name in (
                        (game.home_provider_id, game.home_raw),
                        (game.away_provider_id, game.away_raw),
                    )
                }
                audit = []
                for key, name in labels.items():
                    entry = deepcopy(entries[key])
                    if entry["status"] == "auto_verified" and not _canonical(aliases, league, key):
                        prior = candidates.setdefault(league, {}).get(key)
                        if prior and _canonical(candidates, league, key):
                            if prior["canonical"] != entry["canonical"]:
                                raise ValueError("identity 与历史候选身份冲突")
                            old_evidence = prior.get("evidence", [])
                            entry["evidence"] = old_evidence + [
                                e for e in entry["evidence"] if e not in old_evidence
                            ]
                        candidates[league][key] = entry
                    references = sorted(
                        {
                            f"{row['match_id']}: {row['home']} / {row['away']}"
                            for row in relevant
                            if row["match_id"].startswith("jc:")
                            and any(
                                int(key) in (g.home_provider_id, g.away_provider_id)
                                and abs(_utc(row["kickoff_utc"]) - g.kickoff_utc) <= WINDOW
                                for g in games.values()
                            )
                        }
                    )
                    audit.append(
                        dict(
                            run_id=run_id,
                            league_id=code,
                            league=league,
                            team_id=key,
                            name=name,
                            country=config["country"],
                            canonical=entry.get("canonical") or "",
                            status=entry["status"],
                            appearances_2025=appearances[2025][key],
                            appearances_2026=appearances[2026][key],
                            jc_references=json.dumps(references, ensure_ascii=False),
                            reason=entry.get("reason", ""),
                            evidence=json.dumps(
                                entry.get("evidence", []), ensure_ascii=False, sort_keys=True
                            ),
                        )
                    )
                _validate_candidates(candidates, aliases)
                all_audit.extend(audit)
                count = sum(entries[key]["status"] == "auto_verified" for key in labels)
                report["leagues"][code] = dict(
                    league=league,
                    teams=len(labels),
                    auto_verified=count,
                    proposed=sum(entries[key]["status"] == "proposed" for key in labels),
                )
                report["auto_verified"] += count
            except Exception as exc:
                candidates = before
                report["status"] = "partial"
                report["errors"].append(f"{code}: {failure_reason(exc)}")
        try:
            validate_global_ids(aliases, candidates)
        except ValueError as exc:
            # Do not publish whichever league happened to be checked first.
            candidates = original_candidates
            report["status"], report["auto_verified"] = "partial", 0
            report["errors"].append(str(exc))
            for row in all_audit:
                row.update(status="proposed", canonical="", reason=str(exc))
            for item in report["leagues"].values():
                item.update(auto_verified=0, proposed=item["teams"])
        append_csv(state / "audit/apif_identity.csv", FIELDS, all_audit)
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(
            target, (json.dumps(candidates, ensure_ascii=False, indent=2) + "\n").encode()
        )
        report.update(client.quota.describe())
    finally:
        report["requests"] = client.request_count
        client.session.close()
    LOG.info(
        "身份核验请求 %d 次，自动核验 %d 支；数据库未导入",
        report["requests"],
        report["auto_verified"],
    )
    return report


def write_identity_summary(report):
    import html
    import os
    from pathlib import Path

    from zucai.cloud_feeds import write_apif_quota

    if not os.environ.get("GITHUB_STEP_SUMMARY"):
        return
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
        stream.write(
            f"### APIF 身份核验：{report['status']}\n\n只生成待审证据，不导入比赛或启用别名。\n"
        )
        write_apif_quota(stream, report)
        stream.write(
            f"\n请求 {report['requests']} 次，auto_verified {report['auto_verified']} 支。\n\n"
            "| 联赛 | 球队 | 自动核验 | 未核验 |\n| --- | ---: | ---: | ---: |\n"
        )
        for item in report["leagues"].values():
            stream.write(
                f"| {item['league']} | {item['teams']} | "
                f"{item['auto_verified']} | {item['proposed']} |\n"
            )
        for error in report["errors"]:
            stream.write("\n- " + html.escape(error) + "\n")
