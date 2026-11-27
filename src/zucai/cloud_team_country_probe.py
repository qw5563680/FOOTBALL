"""Archive two cup team lists without interpreting unverified team-country fields."""

import html
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

from zucai.cloud_apis import failure_reason
from zucai.cloud_probe import require_probe_actions
from zucai.sources.api_common import LocalValidationError
from zucai.sources.api_football import API, ApiFootball
from zucai.sources.apif_stats import envelope
from zucai.sources.base import RawStore, _atomic_write

CUP_CODES = (3, 848)
COLLECTION = "apif_team_countries"


def probe_evidence_path(root, run_id):
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", run_id):
        raise ValueError("球队列表 probe 的运行编号无效")
    return root / "state" / "catalogue" / run_id / "apif-team-countries"


def _archive_response(client, root, folder, item, source):
    raw = folder / f"{item['league_id']}.json"
    sidecar = raw.with_suffix(".json.meta.json")
    metadata = client.store.load_metadata(source)
    _atomic_write(raw, client.store.load(source))
    _atomic_write(sidecar, client.store.load(source.with_suffix(".json.meta.json")))
    item.update(
        raw_path=raw.relative_to(root).as_posix(),
        metadata_path=sidecar.relative_to(root).as_posix(),
        raw_sha256=metadata["sha256"],
        http=metadata["status_code"],
        observed_utc=metadata["fetched_at_utc"],
    )


def _team_list(raw, params):
    try:
        rows = envelope(raw, "teams", params)
    except ValueError as exc:
        raise LocalValidationError(str(exc)) from None
    if not rows:
        raise LocalValidationError("球队列表为空")
    return rows


def collect_team_country_probe(database, run_id, *, now=None):
    """One current-season teams request for each cup. Archive transport evidence only."""
    require_probe_actions()
    if os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
        raise ValueError("球队列表 probe 仅允许 github-hosted Actions")
    root = Path(database).parent
    folder = probe_evidence_path(root, run_id)
    now = now or datetime.now(UTC)
    if now.utcoffset() is None:
        raise ValueError("球队列表 probe 的时间须含时区")
    utc = now.astimezone(UTC)
    season = utc.year - (utc.month < 7)
    folder.mkdir(parents=True, exist_ok=False)
    index = folder / "index.json"
    report = dict(
        collection=COLLECTION,
        run_id=run_id,
        status="complete",
        requests=0,
        status_requests=0,
        probes=[],
        errors=[],
        evidence_index=index.relative_to(root).as_posix(),
    )
    client = ApiFootball(
        store=RawStore(root / "raw"), quota_path=root / "state/api_football_quota.json"
    )
    with client.session:
        for league in CUP_CODES:
            params = {"league": league, "season": season}
            url = f"{API}/teams?{urlencode(params)}"
            item = dict(
                league_id=league,
                season=season,
                outcome="error",
                raw_path=None,
                metadata_path=None,
                raw_sha256=None,
                http=None,
                observed_utc=None,
                results=None,
                reason=None,
            )
            client.pages = []
            try:
                path = client._request(url, name="team-country")
                rows = _team_list(client.store.load(path), params)
                item.update(outcome="complete", results=len(rows))
            except (ValueError, OSError) as exc:
                item["reason"] = failure_reason(exc)
                report["status"] = "partial"
                report["errors"].append(f"{league}/{season}: {item['reason']}")
            finally:
                if client.pages:
                    _archive_response(client, root, folder, item, client.pages[-1])
            report["probes"].append(item)
        report["requests"] = client.request_count
    _atomic_write(index, json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
    return report


def write_summary(report):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as stream:
        stream.write("### 欧战球队列表探测\n\n")
        stream.write(f"实际请求 {report['requests']} 次。只归档原件。\n\n")
        stream.write(
            "| 赛事 | 赛季 | HTTP | 完整性 | 条数 | 原件 SHA-256 | 原件路径 | 失败原因 |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
        )
        fields = (
            "league_id",
            "season",
            "http",
            "outcome",
            "results",
            "raw_sha256",
            "raw_path",
            "reason",
        )
        for item in report["probes"]:
            cells = [
                html.escape(str(value)).replace("|", "&#124;").replace("\n", " ").replace("\r", " ")
                if (value := item[field]) is not None
                else "—"
                for field in fields
            ]
            stream.write("| " + " | ".join(cells) + " |\n")
        stream.write(f"\n证据索引 `{report['evidence_index']}`。\n")
