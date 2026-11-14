"""SFC publication evidence from complete, remote-reachable data-branch history."""

import copy
import hashlib
import logging
import re
import subprocess
from pathlib import Path

from zucai.publication_history import (
    PublicationHistoryError,
    check_history,
    git_read,
    resolve_history,
)
from zucai.publication_history import precedes_calculation as _precedes_calculation
from zucai.report.render import _beijing, _timestamp

LOG = logging.getLogger(__name__)
PUBLISHED_REF = "refs/remotes/origin/data/football-feeds"
PUBLICATION_FIELDS = (
    "publication_schema",
    "generated_utc",
    "published_utc",
    "sale_end_utc",
    "sale_state",
    "publication_commit",
    "publication_evidence",
)


def deadline_status(deadline, published):
    if not deadline or not published:
        return "deadline_unconfirmed"
    return (
        "published_before_deadline"
        if _timestamp(published) < _timestamp(deadline)
        else "missed_deadline"
    )


def with_publication(report, evidence=None):
    result = copy.deepcopy(report)
    published = evidence["published_utc"] if evidence else None
    if published and _precedes_calculation(published, report["generated_utc"]):
        raise ValueError("SFC 发布时间早于概率计算完成时间")
    deadline = report["source"]["sale_end_utc"]
    result.update(
        publication_schema=1,
        published_utc=published,
        sale_end_utc=deadline,
        sale_state=deadline_status(deadline, published),
        publication_commit=evidence["commit"] if evidence else None,
        publication_evidence=copy.deepcopy(evidence),
    )
    return result


def deadline_message(report):
    def clock(value):
        return _beijing(value)[11:16] if value else "未知"

    generated, published = clock(report.get("generated_utc")), clock(report.get("published_utc"))
    deadline = clock(report.get("sale_end_utc"))
    state = report["sale_state"]
    if state == "missed_deadline":
        return f"本期错过停售（停售 {deadline}，算完 {generated}，存好 {published}）"
    if state == "published_before_deadline":
        return f"已确认赶上停售（停售 {deadline}，算完 {generated}，存好 {published}）"
    if report.get("published_utc"):
        return f"无法确认赶上停售（停售时间未知，算完 {generated}，存好 {published}）"
    return f"无法确认赶上停售（算完 {generated}，存好时间未知）"


def _git_evidence(archive, reports, *, published_ref=PUBLISHED_REF):
    """Batch lookup; an unpushed local commit or incomplete history proves nothing.

    The caller supplies immutable report paths and already hash-validated reports.
    Publication is the first file-addition commit reachable from a remote-tracking
    branch, with the exact candidate bytes. No wall clock or author date is used.
    """
    if not re.fullmatch(r"refs/remotes/[^/]+/data/football-feeds", published_ref):
        raise ValueError("SFC 发布时间必须来自远程 data/football-feeds 分支")
    archive = Path(archive).resolve()
    paths = {path + "/sfc.json": report for path, report in reports}
    if not paths:
        return {}

    def git(*args, input=None):
        return git_read(archive, *args, input=input)

    try:
        check_history(archive, published_ref)
        history = git(
            "log",
            "--reverse",
            "--topo-order",
            "--format=%H%x09%cI",
            "--diff-filter=A",
            "--name-only",
            published_ref,
            "--",
            "sfc",
        ).decode()
        additions = {}
        commit = timestamp = None
        for line in history.splitlines():
            if re.match(r"^[0-9a-f]{40,64}\t", line):
                commit, timestamp = line.split("\t", 1)
            elif line in paths and line not in additions and commit:
                additions[line] = (commit, timestamp)
        if not additions:
            return {}
        # One subprocess verifies all candidate blobs, including legacy reports.
        raw = git(
            "cat-file",
            "--batch",
            input="".join(f"{commit}:{path}\n" for path, (commit, _) in additions.items()).encode(),
        )
        result, offset = {}, 0
        for path, (commit, timestamp) in additions.items():
            newline = raw.index(b"\n", offset)
            header = raw[offset:newline].split()
            if len(header) != 3 or header[1] != b"blob":
                raise PublicationHistoryError("数据分支历史不可用：SFC Git blob 响应无效")
            size = int(header[2])
            offset = newline + 1
            contents = raw[offset : offset + size]
            offset += size + 1
            sha = hashlib.sha256(contents).hexdigest()
            if sha != hashlib.sha256((archive / path).read_bytes()).hexdigest():
                continue
            published = _timestamp(timestamp).isoformat()
            if _precedes_calculation(published, paths[path]["generated_utc"]):
                continue
            result[path.removesuffix("/sfc.json")] = {
                "candidate_path": path,
                "candidate_sha256": sha,
                "commit": commit,
                "published_utc": published,
                "ref": published_ref,
            }
        return result
    except (OSError, subprocess.SubprocessError, ValueError, UnicodeError) as error:
        if isinstance(error, PublicationHistoryError):
            raise
        raise PublicationHistoryError(
            "数据分支历史不可用：SFC Git 历史读取失败（" + type(error).__name__ + "）"
        ) from error


def publication_evidence(archive, reports, *, published_ref=PUBLISHED_REF, pending=None):
    reports = list(reports)
    candidates = {
        path + "/sfc.json": (
            hashlib.sha256((archive / path / "sfc.json").read_bytes()).hexdigest(),
            report["generated_utc"],
        )
        for path, report in reports
    }

    def derive(missing):
        found = _git_evidence(
            archive,
            [(path, r) for path, r in reports if path + "/sfc.json" in missing],
            published_ref=published_ref,
        )
        return {
            path + "/sfc.json": dict(entry, method="historical_first_containing_commit")
            for path, entry in found.items()
        }

    resolved = resolve_history(archive, "sfc", candidates, derive, pending=pending)
    # Existing SFC reports/receipts retain their original schema and exact presentation.
    return {
        path.removesuffix("/sfc.json"): {k: v for k, v in entry.items() if k != "method"}
        for path, entry in resolved.items()
    }


def resolve_publications(
    archive, reports, *, published_ref=PUBLISHED_REF, now=None, pending_history=None
):
    """Read-only views for digest, including legacy files with no saved receipt."""
    # read_reports has already verified each receipt against the exact original.
    # The original remains in immutable history alongside its confirmation version.
    receipts = {
        report["publication_evidence"]["candidate_path"].removesuffix("/sfc.json"): report[
            "publication_evidence"
        ]
        for _, report in reports
        if report.get("publication_evidence")
    }
    pending = [
        (path, report)
        for path, report in reports
        if not report.get("published_utc") and path not in receipts
    ]
    evidence = {
        **publication_evidence(
            archive, pending, published_ref=published_ref, pending=pending_history
        ),
        **receipts,
    }
    views = []
    for path, report in reports:
        view = (
            copy.deepcopy(report)
            if report.get("publication_evidence")
            else with_publication(report, evidence.get(path))
        )
        if now and view.get("published_utc") and _timestamp(view["published_utc"]) > now:
            view = with_publication(report)
        views.append((path, view))
    return views
