"""Frozen catalogue coverage and a non-operational historical baseline."""

from collections import Counter, defaultdict
from datetime import date, timedelta
from math import isclose

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import aliased

from zucai.coverage_baseline import MODEL_NAME, MODEL_VERSION, historical_baseline
from zucai.digest_context import (
    archived_sfc_issues,
    audit_rows,
    latest_catalog_rows,
    skip_is_evidence,
)
from zucai.forecast_archive import _path, _read
from zucai.models import Match, Result, SfcIssue, Team
from zucai.report.sfc import _unmapped_reason
from zucai.result_source_identity import _binding, _owners
from zucai.scoreboard.score import instant
from zucai.sfc_sequence import period_windows, sequence
from zucai.sources.sfc import _league_key, refresh_snapshot
from zucai.sources.sporttery import BEIJING


def _time(value):
    try:
        return instant(value)
    except (TypeError, ValueError):
        return None


def _sale_days(now):
    today = instant(now).astimezone(BEIJING).date()
    return today.isoformat(), (today + timedelta(days=1)).isoformat()


def with_backup_catalog(catalog, active, gaps, as_of):
    """Supplement observed sales-day slots without replacing official catalogue evidence."""
    if not active and not gaps:
        return catalog
    rows = list(catalog or ())
    as_of = instant(as_of)
    known = {
        (row["business_date"], row["jc_num"]): row
        for row in latest_catalog_rows(_seen(rows, as_of))
    }
    by_id = {row["match_id"]: slot for slot, row in known.items() if row.get("match_id")}
    sources = [(key, evidence, "jc_backup", evidence) for key, evidence in active.items()] + [
        (gap.get("match_id"), gap, "jc_backup_gap", {"reason": gap.get("reason")}) for gap in gaps
    ]
    for key, evidence, marker, proof in sources:
        seen = _time(evidence.get("observed_utc"))
        number = evidence.get("jc_num")
        try:
            day = date.fromisoformat(evidence.get("sales_date")).isoformat()
        except (TypeError, ValueError):
            continue
        if (
            not key
            or not number
            or not seen
            or seen > as_of
            or (marker == "jc_backup_gap" and not proof["reason"])
        ):
            continue
        slot = (day, number)
        known_slot = by_id.get(key)
        previous = known.get(slot)
        if (previous is not None and previous.get("match_id") != key) or (
            known_slot is not None and known_slot != slot
        ):
            continue
        if previous is not None:
            if instant(previous["captured_utc"]) >= seen or any(
                name in previous for name in ("jc_backup", "jc_backup_gap")
            ):
                continue
            row = {**previous, marker: proof}
            rows[rows.index(previous)] = row
        else:
            row = {
                "match_id": key,
                "business_date": day,
                "jc_num": number,
                "captured_utc": seen.isoformat(),
                **{
                    field: evidence.get(field)
                    for field in ("home", "away", "league", "kickoff_utc")
                },
                marker: proof,
            }
            rows.append(row)
        if marker == "jc_backup_gap" and evidence.get("code") == "kickoff_placeholder":
            row["kickoff_basis"] = "placeholder"
        by_id[key], known[slot] = slot, row
    return rows if rows else catalog


def _open_windows(issues, schedule, now):
    windows = period_windows(issues, schedule or {}, now)
    state = sequence(windows.values(), now)
    return {key: windows[key]["sale_end_utc"] for key in state["in_line"] + state["ahead"]}


def _schedule_row(issue):
    check = issue.official_verification
    return dict(
        issue_no=issue.issue_no,
        sale_end_utc=issue.sale_end_utc,
        official_verification=check,
    )


def _identity(session, match):
    if match is not None:
        return dict(
            source=match.source,
            league=match.league,
            kickoff_utc=match.kickoff_utc,
            home=session.get(Team, match.home_team_id).name,
            away=session.get(Team, match.away_team_id).name,
        )


def _formal(candidates, identity, kickoff, generated, scope):
    if identity is None or kickoff is None:
        return None
    return max(
        (
            row
            for row in candidates
            if row.get("status") == "predicted"
            and row.get("mode") == "prematch"
            and row.get("model_name") == "dixon-coles"
            and (scope != "jc" or not row.get("sfc_only"))
            and all(row.get(key) == identity[key] for key in ("source", "league", "home", "away"))
            and row.get("published_utc")
            and instant(row["as_of_utc"])
            <= instant(row["generated_utc"])
            <= instant(row["published_utc"])
            <= generated
            and instant(row["published_utc"]) < min(kickoff, instant(row["kickoff_utc"]))
        ),
        key=lambda row: instant(row["generated_utc"]),
        default=None,
    )


def _placeholder_candidate(session, row):
    """Disclose a possible missing identity without using it as a match binding."""
    if row.get("kickoff_basis") == "placeholder" or not row.get("kickoff_utc"):
        return None
    home, away = aliased(Team), aliased(Team)
    for named, placeholder, label in (
        (home, away, "home"),
        (away, home, "away"),
    ):
        candidates = session.execute(
            select(Match.match_id, placeholder.name)
            .join(home, home.id == Match.home_team_id)
            .join(away, away.id == Match.away_team_id)
            .where(
                Match.league == row["league"],
                Match.kickoff_utc == row["kickoff_utc"],
                named.name == row.get(label),
            )
        ).all()
        if len(candidates) == 1:
            key, name = candidates[0]
            if name.startswith(("apif:team:", "fdorg:team:")):
                return dict(match_id=key, placeholder=name)
    return None


def _skip_action(reason):
    return {
        "match_status_not_selling": "采集维护人核对发布状态",
        "sell_status_not_1": "采集维护人核对销售状态",
        "no_fixed_odds": "采集维护人核对官方售卖范围",
        "no_selling_fixed_pool": "采集维护人核对正在销售的玩法",
        "no_valid_fixed_quotes": "采集维护人核对原始报价字段和赛程入库",
    }[reason]


def _api_history(session, cutoff, configured_leagues):
    """Find already archived API results, including those with temporary identities."""
    sources = defaultdict(set)
    for league, provider in session.execute(
        select(Match.league, Match.source)
        .join(Result, Result.match_id == Match.match_id)
        .where(
            Match.league.not_in(configured_leagues),
            Match.source.in_(("apif", "fdorg")),
            Match.status == "finished",
            Result.available_utc > Match.kickoff_utc,
            Result.available_utc < cutoff,
            or_(
                Result.available_source.in_(("captured", "published")),
                and_(
                    Result.available_source == "assumed",
                    Match.result_checked_utc >= Result.available_utc,
                    Match.result_checked_utc < cutoff,
                ),
            ),
        )
        .distinct()
    ):
        sources[league].add(provider)
    teams = (
        {
            row["id"]: row
            for row in session.execute(select(Team.id, Team.name, Team.aliases)).mappings()
        }
        if sources
        else {}
    )
    return sources, _owners(teams)


def _has_provider_binding(team, league, sources, owners):
    for provider in sources:
        try:
            binding = _binding({"name": team.name, "aliases": team.aliases}, provider, league)
        except ValueError:
            continue
        if owners.get(binding.casefold()) == {team.id}:
            return True
    return False


def _unconfigured_reason(session, league, identified_match, api):
    by_league, owners = api
    sources = by_league.get(league)
    if not sources:
        return (
            "本次归档没有该赛事的接口历史赛果",
            "采集维护人核对该赛事的接口赛果与历史归档",
        )
    if not identified_match or not all(
        _has_provider_binding(session.get(Team, team_id), league, sources, owners)
        for team_id in (identified_match.home_team_id, identified_match.away_team_id)
    ):
        return (
            "球队尚未核实与接口历史的身份绑定",
            "资料审核人核对球队身份，交所有者批准后补齐绑定",
        )
    return "该赛事尚未配置模型或模型池", "所有者决定是否安排建模"


def _missing_reason(session, row, match, identity, skipped, exclusion, configured, scope, api):
    league = row.get("league")
    reason = None
    if scope == "sfc":
        if row.get("mapping") == "missing_page":
            reason = "尚未取得完整的14场赛程表"
            action = "采集维护人补齐逐场赛程并核对官方公告"
        elif not identity:
            reason = _unmapped_reason(row, row.get("reason") or row.get("mapping"))
            action = "资料审核人核对比赛和队名，需新增简称时交所有者批准"
            if league in configured and any(
                side
                for side in (row.get("mapping_evidence") or {}).get("own_fixture_team_gaps", {})
            ):
                action = "资料审核人核对并补齐球队正式身份与历史，交所有者批准"
    if identity and any(
        identity[k].startswith(("apif:team:", "fdorg:team:")) for k in ("home", "away")
    ):
        return (
            "球队仅有占位编号，尚未建立正式球队身份",
            "资料审核人核实球队名称并提交身份批准",
        )
    if league and league not in configured:
        missing, next_action = _unconfigured_reason(
            session, league, match if identity else None, api
        )
        if reason:
            return f"{missing}；{reason}", f"{next_action}；{action}"
        return missing, next_action
    if reason:
        return reason, action
    if match is None:
        if scope == "jc" and skip_is_evidence(skipped, row):
            return f"竞彩同步未收录（{skipped['reason']}）", _skip_action(skipped["reason"])
        return "目录场次在本次数据快照中无已核实比赛", "采集维护人核对比赛入库及球队对应"
    if exclusion:
        return exclusion, "模型维护人核对冻结批次的排除依据"
    return "本班没有对应的有效正式预测", "模型维护人核对模型路由与批次时序"


def _row(session, source, scope, candidates, skipped, configured, generated, history, api):
    match_id = source.get("match_id") or None
    gap = source.get("jc_backup_gap")
    match = session.get(Match, match_id) if match_id and not gap else None
    identity = _identity(session, match)
    if scope == "sfc" and (
        source.get("mapping") != "matched" or source.get("matched_identity") != identity
    ):
        identity = None
    kickoff = _time(source.get("kickoff_utc"))
    if source.get("kickoff_basis") == "placeholder":
        kickoff = None
    if scope == "jc" and identity:
        db_kickoff = instant(identity["kickoff_utc"])
        kickoff = min(kickoff, db_kickoff) if kickoff else db_kickoff
    rows = candidates.get(match_id, ())
    formal = _formal(rows, identity, kickoff, generated, scope)
    league = identity["league"] if identity else source.get("league") or None
    if league and scope == "sfc":
        league = _league_key(league)
    row = {
        "match_id": match_id,
        "fixture_id": source.get("fixture_id"),
        "seq": source.get("seq"),
        "jc_num": source.get("jc_num"),
        "league": league,
        "home": source.get("home") or None,
        "away": source.get("away") or None,
        "kickoff_utc": kickoff.isoformat() if kickoff else None,
        "identity": identity,
        "formal": formal is not None,
        "formal_probabilities": formal["model_probabilities"] if formal else None,
        "reason": None,
        "action": None,
        "baseline": None,
    }
    if backup := source.get("jc_backup"):
        row["jc_backup"] = backup
    if gap:
        row.update(
            jc_backup_gap=gap,
            reason=gap["reason"],
            action="采集维护人核对备用赛程证据，需新增对应关系时交所有者批准",
            baseline_unavailable="备用赛程尚未核实",
        )
        return row
    if formal:
        row["formal_forecast_path"] = formal.get("forecast_path")
        row["formal_kickoff_utc"] = formal["kickoff_utc"]
        return row
    exclusion = next(
        (p["reason"] for p in rows if p.get("status") == "excluded" and p.get("reason")), None
    )
    row["reason"], row["action"] = _missing_reason(
        session,
        {**source, "league": league},
        match,
        identity,
        skipped,
        exclusion,
        configured,
        scope,
        api,
    )
    if scope == "sfc" and not identity and row.get("league"):
        if candidate := _placeholder_candidate(session, source):
            row["reason"] += "；同联赛同开球候选有球队仍为占位编号"
            row["action"] = "资料审核人核实球队名称及候选比赛，提交身份批准"
            row["placeholder_candidate"] = candidate
    if kickoff is None:
        row["baseline_unavailable"] = "缺少可核实的开球时间"
    elif (
        generated >= kickoff
        or source.get("source_ended")
        or (match is not None and match.status != "scheduled")
    ):
        row["baseline_unavailable"] = "场次已开球或当前状态不允许生成赛前概率"
    else:
        row["baseline"] = history.for_league(league)
        if row["baseline"] is None:
            row["baseline_unavailable"] = "截点前没有可核实的历史赛果"
    return row


def _baseline_key(scope, group, row):
    key = row["jc_num"] if scope == "jc" else row["seq"]
    if not (row.get("match_id") or row.get("fixture_id")):
        return None
    return (
        scope,
        group,
        key,
        *(row.get(k) for k in ("match_id", "fixture_id", "league", "home", "away", "kickoff_utc")),
    )


def published_baselines(archive, manifest, publications, now):
    """Prior published baselines for the same fixtures, to retain after kickoff."""
    known = {}
    sale_days = _sale_days(now)
    for run in (manifest or {}).get("runs", {}).values():
        path = run.get("coverage_path")
        published = _time(publications.get(path, {}).get("published_utc"))
        if published is None or published > now:
            continue
        document = read_coverage(archive, run)
        for scope in ("jc", "sfc"):
            for group, rows in document[scope].items():
                if scope == "jc" and group not in sale_days:
                    continue
                if scope == "sfc" and instant(document["sfc_sale_end_utc"][group]) <= now:
                    continue
                for row in rows:
                    if not row["baseline"] or row.get("baseline_source"):
                        continue
                    key = _baseline_key(scope, group, row)
                    if (
                        key is not None
                        and published < instant(row["kickoff_utc"])
                        and (key not in known or known[key][0] < published)
                    ):
                        known[key] = (
                            published,
                            row["baseline"],
                            {
                                "path": path,
                                "generated_utc": document["generated_utc"],
                            },
                        )
    return known


def _gaps(jc, sfc):
    return [
        dict(scope=scope, group=group, **row)
        for scope, groups in (("jc", jc), ("sfc", sfc))
        for group, rows in groups.items()
        for row in rows
        if not row["formal"]
    ]


def _seen(rows, as_of):
    return [r for r in rows or () if r.get("captured_utc") and instant(r["captured_utc"]) <= as_of]


def build_coverage(
    session,
    *,
    run_id,
    as_of,
    generated,
    catalog,
    skipped,
    schedule,
    batches,
    configured_leagues,
    previous_rows=(),
    previous_baselines=None,
):
    """Compute one view from the same DB snapshot/cutoff as the official forecast."""
    as_of, generated = instant(as_of), instant(generated)
    if generated < as_of:
        raise ValueError("覆盖清单的生成时间早于数据截点")
    candidates = defaultdict(list)
    for batch in batches:
        day = batch.get("input", {}).get("kickoff_date_beijing")
        path = f"forecasts/{day}/{run_id}/forecast.json" if day else None
        for row in batch["predictions"]:
            candidates[row["match_id"]].append(
                {
                    **row,
                    **{k: batch[k] for k in ("mode", "generated_utc", "as_of_utc")},
                    "published_utc": batch["generated_utc"],
                    "forecast_path": path,
                }
            )
    for row in previous_rows:
        candidates[row["match_id"]].append(row)
    history = historical_baseline(session, as_of)
    api = _api_history(session, as_of, configured_leagues)
    skipped = {
        (r["business_date"], r["jc_num"], r["match_id"], instant(r["captured_utc"])): r
        for r in _seen(skipped, as_of)
    }
    jc = {day: [] for day in _sale_days(as_of)} if catalog is not None else {}
    for row in sorted(
        latest_catalog_rows(r for r in _seen(catalog, as_of) if r.get("jc_num")),
        key=lambda r: (r["business_date"], r["jc_num"]),
    ):
        day = row["business_date"]
        if day not in jc:
            continue
        skip = skipped.get((day, row["jc_num"], row["match_id"], instant(row["captured_utc"])))
        jc[day].append(
            _row(session, row, "jc", candidates, skip, configured_leagues, generated, history, api)
        )

    issues = {
        issue.issue_no: issue
        for issue in session.scalars(select(SfcIssue).where(SfcIssue.observed_utc <= as_of))
    }
    windows = _open_windows((_schedule_row(i) for i in issues.values()), schedule, as_of)
    sfc = {}
    for number in sorted(windows):
        issue = issues.get(number)
        rows = (
            {r["seq"]: r for r in refresh_snapshot(session, issue)}
            if issue and issue.snapshot
            else {}
        )
        sfc[number] = [
            _row(
                session,
                rows.get(seq, {"seq": seq, "mapping": "missing_page"}),
                "sfc",
                candidates,
                None,
                configured_leagues,
                generated,
                history,
                api,
            )
            for seq in range(1, 15)
        ]
    for scope, groups in (("jc", jc), ("sfc", sfc)):
        for group, rows in groups.items():
            for row in rows:
                if row["formal"] or row["baseline"] or row.get("jc_backup_gap"):
                    continue
                prior = (previous_baselines or {}).get(_baseline_key(scope, group, row))
                if prior and prior[0] <= as_of and prior[0] < instant(row["kickoff_utc"]):
                    row["baseline"], row["baseline_source"] = prior[1:]
                    row.pop("baseline_unavailable", None)
    missing_leagues = Counter(
        row["league"]
        for rows in jc.values()
        for row in rows
        if row["league"] and row["league"] not in configured_leagues
    )
    return dict(
        schema_version=1,
        run_id=run_id,
        as_of_utc=as_of.isoformat(),
        generated_utc=generated.isoformat(),
        jc=jc,
        sfc=sfc,
        sfc_sale_end_utc=windows,
        gaps=_gaps(jc, sfc),
        jc_leagues_without_model=dict(sorted(missing_leagues.items())),
    )


def read_coverage(archive, run):
    name = run.get("coverage_path")
    if not name:
        return None
    document = _read(_path(archive, name))
    if (
        document["schema_version"] != 1
        or document["run_id"] != run["run_id"]
        or not instant(document["as_of_utc"])
        <= instant(document["generated_utc"])
        <= instant(run["generated_utc"])
    ):
        raise ValueError("覆盖清单与预测 run 不符")
    for rows in (*document["jc"].values(), *document["sfc"].values()):
        for row in rows:
            baseline = row["baseline"]
            if not baseline:
                continue
            values = baseline["probabilities"]
            source = row.get("baseline_source")
            generated = instant(source["generated_utc"] if source else document["generated_utc"])
            if (
                row["formal"]
                or baseline["model_name"] != MODEL_NAME
                or baseline["model_version"] != MODEL_VERSION
                or set(values) != set("hda")
                or any(type(p) not in (int, float) or not 0 <= p <= 1 for p in values.values())
                or not isclose(sum(values.values()), 1, abs_tol=1e-12)
                or generated >= instant(row["kickoff_utc"])
                or (source and generated > instant(document["as_of_utc"]))
            ):
                raise ValueError("覆盖清单包含无效基准概率")
    if document["gaps"] != _gaps(document["jc"], document["sfc"]):
        raise ValueError("覆盖清单的缺口明细与场次不符")
    return document


def coverage_summary(document):
    today, tomorrow = _sale_days(document["as_of_utc"])
    groups = [(f"14 场 {number} 期", rows) for number, rows in document["sfc"].items()] + [
        (f"竞彩{label}", document["jc"][day])
        for day, label in ((today, "今日"), (tomorrow, "明日"))
        if day in document["jc"]
    ]
    counts = []
    for label, rows in groups:
        formal = sum(row["formal"] for row in rows)
        baseline = sum(bool(row["baseline"]) for row in rows)
        missing = len(rows) - formal - baseline
        counts.append(
            f"{label}：正式预测 {formal}/{len(rows)}，基准 {baseline}/{len(rows)}"
            + (f"，未生成 {missing}/{len(rows)}" if missing else "")
        )
    return "覆盖情况｜" + ("；".join(counts) if counts else "暂无可核对的赛程名单")


def latest_coverage(archive, manifest, now, *, feed=None, publications=None, forecast_rows=()):
    """Report only an observed, published view and never use a newer run in a past digest."""
    now = instant(now)
    if publications is None:
        from zucai.forecast_publication import publication_times

        publications = publication_times(archive, manifest)
    documents = []
    for run in (manifest or {}).get("runs", {}).values():
        name = run.get("coverage_path")
        published = _time(publications.get(name, {}).get("published_utc"))
        if published is not None and published <= now:
            doc = read_coverage(archive, run)
            documents.append((instant(doc["generated_utc"]), published, name, doc))
    if not documents:
        return None
    _, published, name, document = max(documents, key=lambda item: item[:3])
    allowed_days = _sale_days(now)
    document["coverage_as_of_utc"] = document["as_of_utc"]
    document["as_of_utc"] = now.isoformat()
    document["jc"] = {k: v for k, v in document["jc"].items() if k in allowed_days}
    ends = document["sfc_sale_end_utc"]
    current_slots = {}
    if feed is not None:
        schedule = feed.get("collection", {}).get("sfc", {}).get("official_schedule")
        current_issues = [row for _, row in archived_sfc_issues(archive, feed, now)]
        ends = _open_windows(current_issues, schedule, now)
        for row in latest_catalog_rows(
            audit_rows(archive, feed, "jc_catalog", now, all_shards=True)
        ):
            current_slots["jc", row["business_date"], row["jc_num"]] = row
        for issue in current_issues:
            for row in issue.get("snapshot") or ():
                current_slots["sfc", issue["issue_no"], row["seq"]] = row
    document["sfc"] = {
        k: v for k, v in document["sfc"].items() if k in ends and now < instant(ends[k])
    }
    candidates = defaultdict(list)
    for row in forecast_rows:
        candidates[row["match_id"]].append(row)
    for scope in ("jc", "sfc"):
        for group, rows in document[scope].items():
            for row in rows:
                number = "jc_num" if scope == "jc" else "seq"
                current = current_slots.get((scope, group, row[number]))
                kickoff = _time(row["kickoff_utc"])
                if current:
                    identity_key = "match_id" if scope == "jc" else "fixture_id"
                    corrected = (
                        _time(current.get("kickoff_utc"))
                        if current.get("kickoff_basis") != "placeholder"
                        else None
                    )
                    if (current.get(identity_key) or None) != row.get(identity_key):
                        row.update(
                            **{
                                k: current.get(k) or None
                                for k in ("match_id", "home", "away", "league")
                            },
                            fixture_id=current.get("fixture_id"),
                            identity=None,
                            kickoff_utc=corrected.isoformat() if corrected else None,
                            formal=False,
                            formal_probabilities=None,
                            baseline=None,
                            reason="赛程清单在本班冻结后换场，新场次尚无已核实概率",
                            action="运行维护人核对新场次和下一班冻结结果",
                        )
                        row.pop("baseline_unavailable", None)
                        continue
                    if kickoff and corrected:
                        kickoff = min(kickoff, corrected)
                if row["formal"]:
                    path = row.get("formal_forecast_path")
                    sent = _time(publications.get(path, {}).get("published_utc"))
                    original = _time(row.get("formal_kickoff_utc")) or kickoff
                    if not (sent and kickoff and sent <= now and sent < min(kickoff, original)):
                        alternative = _formal(
                            candidates.get(row["match_id"], ()),
                            row["identity"],
                            kickoff,
                            now,
                            scope,
                        )
                        if alternative:
                            row.update(
                                formal_probabilities=alternative["model_probabilities"],
                                formal_forecast_path=alternative["forecast_path"],
                            )
                        else:
                            row.update(
                                formal=False,
                                formal_probabilities=None,
                                reason="正式预测缺少有效的开球前发布证据",
                                action="运行维护人核对开球及数据分支发布时间",
                            )
                if row["baseline"]:
                    source = row.get("baseline_source")
                    baseline_pub = (
                        _time(publications.get(source["path"], {}).get("published_utc"))
                        if source
                        else published
                    )
                    if not (
                        baseline_pub and kickoff and baseline_pub <= now and baseline_pub < kickoff
                    ):
                        row["baseline"] = None
                        row["baseline_unavailable"] = "基准缺少有效的开球前发布证据"
    document["coverage_path"] = name
    document["gaps"] = _gaps(document["jc"], document["sfc"])
    return document
