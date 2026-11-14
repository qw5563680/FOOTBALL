"""Escaped display of frozen values; no model or score calculations."""

import html
from datetime import UTC, datetime, timedelta, timezone
from urllib.parse import quote

BEIJING = timezone(timedelta(hours=8))


def _instant(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("摘要来源时间必须含时区")
    return result.astimezone(UTC)


def _day(value):
    return _instant(value).astimezone(BEIJING).date()


def _text(value):
    if value is None:
        return "未记录"
    text = html.escape(str(value)).replace("\n", " ").replace("\r", " ")
    for symbol in ("\\", "|", "[", "]", "`", "*", "_"):
        text = text.replace(symbol, "\\" + symbol)
    return text.replace("@", "&#64;")


def _number(value, places=4, *, percent=False):
    if value is None:
        return "未提供"
    return f"{value * 100 if percent else value:.{places}f}" + ("%" if percent else "")


def _probabilities(row):
    values = row.get("model_probabilities") or {}
    return " / ".join(_number(values.get(key), 2, percent=True) for key in "hda")


def _match(row):
    if row.get("home") and row.get("away"):
        return f"{_text(row['home'])} VS {_text(row['away'])}"
    return _text(row["match_id"])


def _link(path, repository):
    target = (
        f"https://github.com/{repository}/blob/data/football-feeds/{quote(path)}"
        if repository
        else "../" + quote(path)
    )
    return f"[{_text(path)}]({target})"
