"""Weekly digest diagnostics using frozen, provably published forecasts and known results."""

from statistics import fmean

from zucai.digest_context import observed_results, week_start
from zucai.digest_display import _instant, _number
from zucai.report.sfc import disclosed_rows
from zucai.scoreboard.score import outcome, publication_reason, result_reason
from zucai.upset import TIERS, assess


def _order(row):
    return (
        _instant(row["generated_utc"]),
        _instant(row["published_utc"]),
        row.get("forecast_path", ""),
        row["match_id"],
    )


def sfc_prematch_rows(reports, now):
    """Latest eligible frozen SFC row per match, including the original report evidence."""
    start = week_start(now)
    latest = {}
    for path, report in reports:
        if report["mode"] != "prematch" or report["sale_state"] != "published_before_deadline":
            continue
        generated, published = map(_instant, (report["generated_utc"], report["published_utc"]))
        if published > now:
            continue
        check = (report.get("source") or {}).get("official_verification") or {}
        if any(
            difference.get("status") == "conflict"
            and difference.get("field") in {"seq", "home", "away"}
            for difference in check.get("differences", ())
        ):
            continue
        for row in disclosed_rows(report):
            match_id, identity = row.get("match_id"), row.get("matched_identity")
            if not match_id or not identity:
                continue
            kickoff = _instant(identity["kickoff_utc"])
            if (
                not start <= kickoff <= now
                or kickoff != _instant(row["kickoff_utc"])
                or max(generated, published) >= kickoff
            ):
                continue
            frozen = dict(
                row,
                generated_utc=report["generated_utc"],
                published_utc=report["published_utc"],
                forecast_path=(report.get("publication_evidence") or {}).get(
                    "candidate_path", path
                ),
            )
            if match_id not in latest or _order(frozen) > _order(latest[match_id]):
                latest[match_id] = frozen
    return latest


def sfc_known_outcomes(frozen, matches, now):
    results = observed_results({key: matches[key] for key in frozen if key in matches})
    for key, row in frozen.items():
        result = results.get(key)
        identity = dict(row["matched_identity"], generated_utc=row["generated_utc"])
        if not result_reason(identity, result, now) and _instant(result["available_utc"]) > max(
            _instant(row["published_utc"]), _instant(identity["kickoff_utc"])
        ):
            yield row, outcome(result)


def _counterpart_keys(pairs, matches):
    ids = {row["match_id"] for row, _, _ in pairs}
    keys = {}

    def canonical(key):
        while key in keys:
            key = keys[key]
        return key

    for key in sorted(ids):
        match = matches.get(key, {})
        other = match.get("counterpart_id")
        linked = matches.get(other)
        if other not in ids or not linked or other == key:
            continue
        identity = ("league", "home_team_id", "away_team_id")
        if all(
            match.get(field) is not None and match[field] == linked.get(field) for field in identity
        ) and (_instant(match["kickoff_utc"]) == _instant(linked["kickoff_utc"])):
            first, second = sorted((canonical(key), canonical(other)))
            if first != second:
                keys[second] = first
    return {key: canonical(key) for key in ids}


def table(jc_pairs, sfc_pairs, matches, now):
    candidates = [
        (row, actual, False)
        for row, actual in jc_pairs
        if not publication_reason(row, now)
        and _instant(row["generated_utc"]) < _instant(row["kickoff_utc"])
    ] + [(row, actual, True) for row, actual in sfc_pairs]
    keys = _counterpart_keys(candidates, matches)
    latest = {}
    for row, actual, sfc in sorted(candidates, key=lambda pair: _order(pair[0])):
        latest[keys[row["match_id"]]] = row, actual, sfc

    groups = {tier: [] for tier in TIERS}
    for row, actual, sfc in latest.values():
        info = assess(row, sfc=sfc)
        if info["tier"] is not None:
            groups[info["tier"]].append((info["probability"], actual != info["favorite"]))
    if not any(groups.values()):
        return []
    lines = [
        "",
        "**爆冷预测成绩**",
        "",
        "| 档位 | 场次数 | 实际爆冷场次 | 实际爆冷率 | 赛前平均爆冷概率 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for tier, rows in groups.items():
        n = len(rows)
        upsets = sum(occurred for _, occurred in rows)
        rate = _number(upsets / n, 2, percent=True) if n else "—"
        average = _number(fmean(p for p, _ in rows), 2, percent=True) if n else "—"
        lines.append(f"| {tier} | {n} | {upsets} | {rate} | {average} |")
    return lines
