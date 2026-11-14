"""Explicit domestic training policy, independent of the ingestion registry."""

import json
from pathlib import Path

APIF_HISTORY_PATH = Path(__file__).resolve().parents[3] / "data/model/apif_history_training.json"


def apif_history_policy():
    """Read the committed opt-in list; malformed policy never broadens access."""
    try:
        document = json.loads(APIF_HISTORY_PATH.read_bytes())
    except (OSError, ValueError) as exc:
        raise ValueError("APIF 历史训练来源配置无法读取") from exc
    if (
        not isinstance(document, dict)
        or type(document.get("schema_version")) is not int
        or document["schema_version"] != 1
        or not isinstance(document.get("leagues"), dict)
    ):
        raise ValueError("APIF 历史训练来源配置格式无效")
    policy = {}
    for league, entry in document["leagues"].items():
        sources = entry.get("sources") if isinstance(entry, dict) else None
        if (
            not isinstance(league, str)
            or not league.strip()
            or league in {"欧战", "欧国联"}
            or not isinstance(sources, list)
            or any(not isinstance(source, str) for source in sources)
            or len(sources) != len(set(sources))
            or (
                set(sources) != {"apif", "jc"}
                and not (
                    entry.get("availability_policy") == "actual_capture"
                    and {"apif", "fd", "jc"} <= set(sources) <= {"apif", "fd", "fdorg", "jc"}
                )
            )
            or entry.get("availability_policy") not in (None, "actual_capture")
        ):
            raise ValueError(f"APIF 历史训练来源配置 {league} 须显式声明 apif / jc")
        policy[league] = entry
    return policy


def apif_history_sources():
    return {league: set(entry["sources"]) for league, entry in apif_history_policy().items()}


def actual_result_leagues():
    """T63 opt-in: added API rows require real observations, not historical delay."""
    return {
        league
        for league, entry in apif_history_policy().items()
        if entry.get("availability_policy") == "actual_capture"
    }
