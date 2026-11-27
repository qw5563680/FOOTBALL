"""Read authenticated team-country observations from archived T99 probes."""

import json
import logging
import re
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

from zucai.cloud_team_country_probe import COLLECTION, CUP_CODES
from zucai.identity_archive_evidence import FrozenEvidence
from zucai.identity_binding import _utc
from zucai.sources.api_football import API
from zucai.sources.apif_stats import envelope

LOG = logging.getLogger(__name__)
PROBE_PATH = re.compile(
    r"state/catalogue/(?P<run>[A-Za-z0-9][A-Za-z0-9_-]{0,99})/apif-team-countries/"
    r"(?:index\.json|(?:3|848)\.json(?:\.meta\.json)?)"
)


def _verified_bytes(frozen, relative):
    path = frozen.root / relative
    if any(p.is_symlink() for p in (path, *path.parents) if p.is_relative_to(frozen.root)):
        raise ValueError("球队列表证据路径含符号链接")
    return frozen.verify(relative).read_bytes()


def _country_rows(frozen, directory, item, now):
    league, season = item["league_id"], item["season"]
    path = f"{directory}/{league}.json"
    metadata_path = path + ".meta.json"
    if (
        type(season) is not int
        or not 2021 <= season <= now.year
        or item["raw_path"] != path
        or item["metadata_path"] != metadata_path
        or item["http"] != 200
    ):
        raise ValueError("球队列表索引范围无效")
    raw = _verified_bytes(frozen, path)
    meta = json.loads(_verified_bytes(frozen, metadata_path))
    params = {"league": league, "season": season}
    captured = _utc(meta["fetched_at_utc"])
    sha256 = frozen.manifest["files"][path]
    # This independently appended probe can postdate the unchanged fixtures snapshot.
    if (
        meta["url"] != f"{API}/teams?{urlencode(params)}"
        or meta["status_code"] != 200
        or item["raw_sha256"] != sha256
        or meta["sha256"] != sha256
        or captured != _utc(item["observed_utc"])
        or captured > now
    ):
        raise ValueError("球队列表原件与探测回执不一致")
    doc = json.loads(raw)
    rows = envelope(doc, "teams", params)
    if (
        set(doc["parameters"]) != params.keys()
        or any(type(doc["paging"][key]) is not int for key in ("current", "total"))
        or not rows
        or len(rows) != item["results"]
    ):
        raise ValueError("球队列表范围、分页或条数不完整")
    for index, row in enumerate(rows):
        team = row.get("team")
        if (
            not isinstance(team, dict)
            or type(team.get("id")) is not int
            or team["id"] <= 0
            or type(team.get("national")) is not bool
        ):
            raise ValueError("球队列表缺少有效球队编号或类别")
        country = team.get("country")
        yield (
            str(team["id"]),
            {
                "country": country if isinstance(country, str) and not team["national"] else None,
                "path": path,
                "sha256": sha256,
                "observed_utc": meta["fetched_at_utc"],
                "pointer": f"/response/{index}/team/country",
            },
        )


def load_team_countries(archive, *, now=None):
    """Read all corroborating or conflicting observations keyed by API-Football team ID."""
    now = now or datetime.now(UTC)
    if now.utcoffset() is None:
        raise ValueError("球队国家核对需要明确时区")
    frozen = FrozenEvidence(Path(archive))
    directories = {
        path.rsplit("/", 1)[0]: match["run"]
        for path in frozen.manifest["files"]
        if (match := PROBE_PATH.fullmatch(path))
    }
    proofs = defaultdict(list)
    for directory, run_id in sorted(directories.items()):
        index_path = f"{directory}/index.json"
        try:
            report = json.loads(_verified_bytes(frozen, index_path))
            items = report["probes"]
            if (
                report["run_id"] != run_id
                or report["collection"] != COLLECTION
                or report["evidence_index"] != index_path
                or len(items) != len(CUP_CODES)
                or {item["league_id"] for item in items} != set(CUP_CODES)
                or any(item["outcome"] not in {"complete", "error"} for item in items)
                or report["status"]
                != ("complete" if all(i["outcome"] == "complete" for i in items) else "partial")
            ):
                raise ValueError("球队列表探测索引无效")
            for item in items:
                if item["outcome"] == "complete":
                    for provider_id, proof in _country_rows(frozen, directory, item, now):
                        proofs[provider_id].append(proof)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            # A missing complete page might contain a conflicting country for any team.
            LOG.warning("球队国家证据不可核实，保留待核：%s（%s）", index_path, exc)
            return {}
    return dict(proofs)
