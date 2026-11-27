"""Render a separate daily section from independently frozen web research."""

import html
import logging
import re
import unicodedata

from zucai.digest_display import _day, _instant, _number, _text
from zucai.web_research_archive import has_runs, load

LOG = logging.getLogger(__name__)
OUTCOMES = {"h": "主胜", "d": "平", "a": "客胜"}
UNAVAILABLE = {
    "failed": "未生成（接口失败）",
    "partial": "未完成",
    "unparsed": "未生成（结构未通过校验）",
    "identity_mismatch": "未生成（比赛身份不符）",
}
UNSAFE_TEXT = re.compile(
    r"[a-z][a-z0-9+.-]*://|\bwww\.|@|\b(?:javascript|data|mailto):|"
    r"\b(?:BET|WATCH|PASS|stake|parlay|entry price|Bearer)\b|\bsk-[a-z0-9_-]{8,}|"
    r"建议购买|推荐|下注|投注|串关|入场价|仓位",
    re.IGNORECASE,
)


def _plain(value, limit):
    """Keep external text within one table cell and suppress action-oriented content."""
    if not isinstance(value, str):
        return None
    value = "".join(
        " " if char.isspace() else char
        for char in value
        if char.isspace() or not unicodedata.category(char).startswith("C")
    )
    value = " ".join(value.split())
    if not value or UNSAFE_TEXT.search(unicodedata.normalize("NFKC", html.unescape(value))):
        return None
    return _text(value if len(value) <= limit else value[: limit - 1] + "…")


def _match(row):
    identity = row["identity"]
    home, away = (_plain(identity.get(key), 36) for key in ("home", "away"))
    number = _plain(identity.get("jc_num"), 12)
    label = f"{home} VS {away}" if home and away else _plain(row["match_id"], 36)
    return " ".join(part for part in (number, label) if part) or "场次未记录"


def _outcome(outcome, probability, missing):
    label = OUTCOMES.get(outcome)
    if label is None:
        return missing
    if type(probability) in (float, int) and 0 <= probability <= 1:
        return f"{label} {_number(probability, 2, percent=True)}"
    return label


def _basis(facts):
    texts = []
    for fact in facts:
        if fact.get("urls") and fact.get("published_utc") and (text := _plain(fact["text"], 100)):
            texts.append(text)
            if len(texts) == 2:
                break
    return " / ".join(texts) or "无可展示依据"


def _render(row):
    identity = _match(row)
    checks = row.get("checks") or {}
    warnings = checks.get("warning_count", "未记录")
    if type(warnings) is not int or warnings < 0:
        warnings = "未记录"
    if row["status"] != "ok":
        return f"| {identity} | — | {UNAVAILABLE[row['status']]} | — | {warnings} |"
    structured = row["structured"]
    market = structured.get("market") or {}
    market_text = _outcome(market.get("outcome"), market.get("probability"), "未记录")
    judgment = structured["judgment"]
    value = (structured.get("probabilities") or {}).get(judgment)
    research = _outcome(judgment, value, "无可靠倾向")
    return (
        f"| {identity} | {market_text} | {research} | {_basis(structured['basis'])} | {warnings} |"
    )


def _visible(rows, now, kickoffs):
    selected = {}
    today = _day(now)
    for row in rows:
        kickoff = _instant(row["kickoff_utc"])
        completed = _instant(row["completed_utc"])
        frozen = _instant(row["frozen_utc"])
        corrected = kickoffs.get(row["match_id"])
        if (
            completed > frozen
            or frozen > now
            or frozen >= kickoff
            or _day(kickoff) < today
            or (corrected is not None and _instant(corrected) != kickoff)
        ):
            continue
        key = (completed, frozen, row["run_id"])
        existing = selected.get(row["match_id"])
        if existing is None or key > existing[0]:
            selected[row["match_id"]] = key, row
    return sorted(
        (row for _, row in selected.values()),
        key=lambda row: (_instant(row["kickoff_utc"]), row["match_id"]),
    )


def section(archive, now, *, kickoffs=None):
    """Return daily Markdown lines and exact archive hashes for the render receipt."""
    try:
        if not has_runs(archive):
            return [], {}
        now = _instant(now)
        rows = _visible(load(archive, now), now, kickoffs or {})
        if not rows:
            return [], {}
        lines = [
            "## 联网研究线（实验）",
            "",
            "参考市场信息，展示截点内已冻结的赛前研究及核对告警。",
            "",
            "| 竞彩场次 | 市场最高概率项 | 研究判断 | 主要依据 | 核对告警 |",
            "| --- | --- | --- | --- | --- |",
            *(_render(row) for row in rows),
            "",
        ]
        return lines, {row["archive_path"]: row["archive_sha256"] for row in rows}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        # This optional archive may contain provider text. Log a fixed diagnostic only.
        LOG.warning("联网研究线归档无法读取，日报其他栏目照常生成。")
        return [], {}
