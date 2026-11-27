"""Grade independent web research against documented regular-time results."""

import hashlib
import json
import logging
from collections import Counter, defaultdict
from math import log
from pathlib import Path
from statistics import mean
from tempfile import TemporaryDirectory

from zucai.forecast_archive import _path
from zucai.report.render import OUTCOMES, _md, _triplet
from zucai.scoreboard.render import number
from zucai.scoreboard.score import instant, outcome, result_reason
from zucai.sources.sporttery import BEIJING
from zucai.web_research_archive import load

LOG = logging.getLogger(__name__)
SCORE_DATA = "scoreboard/web_research.json"
SCORE_REPORT = "scoreboard/web-research.md"
LABELS = {"research": "联网研究", "team": "球队模型", "market": "竞彩赔率基准"}


def _prematch(row, as_of):
    published, completed, frozen, kickoff = (
        instant(row[key])
        for key in ("forecast_published_utc", "completed_utc", "frozen_utc", "kickoff_utc")
    )
    if not published <= completed <= frozen < kickoff:
        return "freeze_not_prematch"
    if frozen > as_of:
        return "freeze_not_yet_available"
    if not row["match_id"].startswith("jc:"):
        return "not_jc"
    return None


def _result_reason(row, result, as_of):
    forecast = {
        **{key: row["identity"][key] for key in ("league", "home", "away")},
        "source": "jc",
        "generated_utc": row["completed_utc"],
        "kickoff_utc": row["kickoff_utc"],
    }
    reason = result_reason(forecast, result, as_of)
    if not reason and instant(result["available_utc"]) <= instant(row["kickoff_utc"]):
        return "result_not_after_kickoff"
    return reason


def _active_order(row):
    return (
        row["status"] == "ok",
        instant(row["completed_utc"]),
        instant(row["frozen_utc"]),
        row["run_id"],
    )


def _match(row, result):
    actual = outcome(result)
    structured = row["structured"] if row["status"] == "ok" else {}
    judgment = structured.get("judgment")
    if judgment is not None and judgment not in OUTCOMES:
        raise ValueError("无效联网研究判断")
    probabilities = {
        "research": structured.get("probabilities"),
        "team": row["model_probabilities"],
        "market": row["market_probabilities"],
    }
    for label, values in probabilities.items():
        if values is not None:
            _triplet(values, LABELS[label], probabilities=True)
    return {
        **{
            key: row[key]
            for key in (
                "match_id",
                "run_id",
                "archive_path",
                "archive_sha256",
                "forecast_run_id",
                "forecast_path",
                "forecast_sha256",
                "forecast_published_utc",
                "kickoff_utc",
                "completed_utc",
                "frozen_utc",
                "status",
            )
        },
        **{
            key: result[key]
            for key in ("available_utc", "available_source", "home_goals", "away_goals", "league")
        },
        "actual": actual,
        "judgment": judgment,
        "hit": judgment == actual if judgment is not None else None,
        **{f"{key}_probabilities": values for key, values in probabilities.items()},
    }


def _probability(rows, column):
    values = [(row[f"{column}_probabilities"], row["actual"]) for row in rows]
    if not values:
        return {"n": 0, "log_loss": None, "brier": None}
    realized = [p[actual] for p, actual in values]
    return {
        "n": len(values),
        "log_loss": "Infinity" if 0 in realized else mean(-log(p) for p in realized),
        "brier": mean(
            sum((p[key] - int(actual == key)) ** 2 for key in OUTCOMES) for p, actual in values
        ),
    }


def _direction_accuracy(rows, column):
    if not rows:
        return None
    if column == "research":
        return mean(int(row["hit"]) for row in rows)
    hits = []
    for row in rows:
        probabilities = row[f"{column}_probabilities"]
        best = max(probabilities.values())
        ties = sum(p == best for p in probabilities.values())
        hits.append(int(probabilities[row["actual"]] == best) / ties)
    return mean(hits)


def _summarize(rows):
    directions = [row for row in rows if row["hit"] is not None]
    probabilities = [row for row in rows if row["research_probabilities"] is not None]
    paired_direction = [
        r for r in directions if r["team_probabilities"] and r["market_probabilities"]
    ]
    paired_probability = [
        r for r in probabilities if r["team_probabilities"] and r["market_probabilities"]
    ]
    return {
        "settled_n": len(rows),
        "direction_n": len(directions),
        "direction_coverage": len(directions) / len(rows) if rows else None,
        "direction_hit_n": sum(row["hit"] for row in directions),
        "direction_hit_rate": _direction_accuracy(directions, "research"),
        "no_tendency_n": sum(row["status"] == "ok" and row["judgment"] is None for row in rows),
        "statuses": dict(Counter(row["status"] for row in rows)),
        "probability": _probability(probabilities, "research"),
        "common": {
            "direction_match_ids": [row["match_id"] for row in paired_direction],
            "probability_match_ids": [row["match_id"] for row in paired_probability],
            "direction_hit_rate": {
                key: _direction_accuracy(paired_direction, key) for key in LABELS
            },
            "probability": {key: _probability(paired_probability, key) for key in LABELS},
        },
    }


def score(attempts, results, as_of):
    """Pick one pre-kickoff attempt per settled fixture, preferring valid research."""
    at = instant(as_of)
    selected = defaultdict(list)
    excluded = Counter()
    attempted_ids = set()
    for row in attempts:
        if why := _prematch(row, at):
            excluded[why] += 1
            continue
        key = row["match_id"]
        attempted_ids.add(key)
        if why := _result_reason(row, results.get(key), at):
            excluded[why] += 1
            continue
        selected[key].append(row)
    matches = [
        _match(max(rows, key=_active_order), results[key]) for key, rows in sorted(selected.items())
    ]
    leagues = defaultdict(list)
    for row in matches:
        leagues[row["league"]].append(row)
    return {
        "schema_version": 1,
        "as_of_utc": at.isoformat(),
        "status": "experimental",
        "bet_allowed": False,
        "attempted_n": len(attempted_ids),
        "overall": _summarize(matches),
        "leagues": {key: _summarize(rows) for key, rows in sorted(leagues.items())},
        "excluded": dict(sorted(excluded.items())),
        "matches": matches,
    }


def _loss_label(value):
    return "∞" if value == "Infinity" else number(value)


def render(summary):
    overall = summary["overall"]
    common = overall["common"]
    lines = [
        "# 联网研究线（实验）记分板",
        "",
        "参考市场信息 · BET 禁用",
        f"截至北京时间 {instant(summary['as_of_utc']).astimezone(BEIJING).isoformat()}。",
        "",
        f"尝试 {summary['attempted_n']} 场，已核实常规时间赛果 {overall['settled_n']} 场。",
        f"明确判断 {overall['direction_n']} 场，"
        f"覆盖率 {number(overall['direction_coverage'], True)}，"
        f"命中 {overall['direction_hit_n']} 场，"
        f"命中率 {number(overall['direction_hit_rate'], True)}。"
        f"无可靠倾向 {overall['no_tendency_n']} 场。",
        f"有三项研究概率 {overall['probability']['n']} 场，"
        f"Log Loss {_loss_label(overall['probability']['log_loss'])}，"
        f"三类 Brier {number(overall['probability']['brier'])}。",
        "",
        "## 同场配对（全部联赛合计）",
        "",
        "三方在同一批场次上比较。研究命中按明确判断计，球队模型与赔率最高项并列时按并列项数分摊。",
        "实际结果的预测概率为 0 时，Log Loss 记为 ∞。",
        "",
        "| 口径 | 有明确判断的配对场数 | 命中率 | 有全概率的配对场数 | Log Loss | 三类 Brier |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for key, label in LABELS.items():
        probabilities = common["probability"][key]
        lines.append(
            f"| {label} | {len(common['direction_match_ids'])} | "
            f"{number(common['direction_hit_rate'][key], True)} | {probabilities['n']} | "
            f"{_loss_label(probabilities['log_loss'])} | {number(probabilities['brier'])} |"
        )
    lines += [
        "",
        "## 各联赛描述",
        "",
        "| 联赛 | 已核实赛果 | 明确判断 | 覆盖率 | 命中率 | 三项概率 | Log Loss | 三类 Brier |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for league, values in summary["leagues"].items():
        probabilities = values["probability"]
        lines.append(
            f"| {_md(league)} | {values['settled_n']} | {values['direction_n']} | "
            f"{number(values['direction_coverage'], True)} | "
            f"{number(values['direction_hit_rate'], True)} | {probabilities['n']} | "
            f"{_loss_label(probabilities['log_loss'])} | {number(probabilities['brier'])} |"
        )
    return "\n".join([*lines, "", "[同场编号、冻结路径与逐场证据](web_research.json)", ""])


def _write_reports(archive, summary):
    markdown = render(summary).encode()
    summary["report_sha256"] = hashlib.sha256(markdown).hexdigest()
    data = (
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode()
    files = {_path(archive, SCORE_REPORT): markdown, _path(archive, SCORE_DATA): data}
    before = {path: path.read_bytes() if path.exists() else None for path in files}
    replaced = []
    with TemporaryDirectory(prefix=".web-research-score-", dir=archive) as temporary:
        staged = Path(temporary)
        for path, raw in files.items():
            (staged / path.name).write_bytes(raw)
        try:
            for path in files:
                (staged / path.name).replace(path)
                replaced.append(path)
        except OSError:
            for path in replaced:
                if before[path] is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(before[path])
            raise


def update(archive, results, as_of):
    """Publish only T70 scores. An unavailable archive leaves previous evidence intact."""
    try:
        attempts = load(archive, as_of)
        if attempts:
            _write_reports(archive, score(attempts, results, as_of))
    except Exception:
        LOG.warning("联网研究独立计分不可用，保留已有评分证据")
