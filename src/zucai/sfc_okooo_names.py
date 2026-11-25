"""Owner-approved Okooo names, scoped to the provider league and printed label."""

import json
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from zucai.sfc_sequence import _instant
from zucai.sources.football_data import CONFIG_ROOT

PATH = CONFIG_ROOT / "team_aliases/sfc_okooo.json"
PROVIDER = "okooo.com"
REVIEW = r"https://github\.com/qw5563680/FOOTBALL/pull/[1-9][0-9]*"


def is_okooo(url):
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname == "www.okooo.com"
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        return False


def _text(value):
    return isinstance(value, str) and bool(value) and value == value.strip()


def _evidence_link(value):
    if not _text(value):
        return False
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        return False


def load(path=None):
    document = json.loads(Path(path or PATH).read_bytes())
    if (
        not isinstance(document, dict)
        or document.get("version") != 1
        or not isinstance(document.get("leagues"), dict)
    ):
        raise ValueError("澳客简称表结构无效")
    entries = {}
    for league, names in document["leagues"].items():
        if not _text(league) or not isinstance(names, dict):
            raise ValueError("澳客简称赛事条目无效")
        for label, entry in names.items():
            if not _text(label) or not isinstance(entry, dict):
                raise ValueError("澳客简称条目无效")
            if entry.get("status") != "approved":
                continue
            if (
                entry.get("approved_by") != "qw5563680"
                or not _text(entry.get("canonical"))
                or ("official" in entry and not _text(entry["official"]))
                or not _evidence_link(entry.get("evidence"))
                or not re.fullmatch(REVIEW, str(entry.get("approval_pr", "")))
            ):
                raise ValueError("澳客简称缺少规范队名、证据或所有者批准")
            _instant(entry.get("approved_utc"))
            entries[league, label] = entry
    return entries


@lru_cache(maxsize=1)
def _table():
    return load()


def approved_name(league, label, at):
    """Return only evidence already approved at the relevant observation."""
    entry = _table().get((league, label))
    return entry if entry and _instant(entry["approved_utc"]) <= _instant(at) else None


def name_evidence(entry, label):
    return {
        "label": label,
        **{
            key: entry[key]
            for key in (
                "canonical",
                "official",
                "evidence",
                "approved_by",
                "approved_utc",
                "approval_pr",
            )
            if key in entry
        },
    }
