"""SFC publication evidence from complete, remote-reachable data-branch history."""

import copy
import hashlib
import logging
import re
import subprocess
from pathlib import Path

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


def _precedes_calculation(published, generated):
    published, generated = _timestamp(published), _timestamp(generated)
    # Git %cI drops subsecond precision. Accept only that exact truncation;
    # never move the recorded commit timestamp or relax the sale deadline.
    same_git_second = published.microsecond == 0 and published == generated.replace(microsecond=0)
    return published < generated and not same_git_second


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


def publication_evidence(archive, reports, *, published_ref=PUBLISHED_REF):
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
        return subprocess.run(
            ["git", "-C", str(archive), *args],
            input=input,
            capture_output=True,
            check=True,
            timeout=30,
        ).stdout

    try:
        if Path(git("rev-parse", "--show-toplevel").decode().strip()).resolve() != archive:
            return {}
        if git("rev-parse", "--is-shallow-repository").strip() != b"false":
            LOG.warning("SFC 发布记录缺少完整 Git 历史，存好时间未知")
            return {}
        git("show-ref", "--verify", published_ref)
        git("config", "--get", "remote." + published_ref.split("/")[2] + ".url")
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
                return {}
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
        LOG.warning("SFC 存好时间无法从数据分支确认：%s", type(error).__name__)
        return {}


def resolve_publications(archive, reports, *, published_ref=PUBLISHED_REF, now=None):
    """Read-only views for digest, including legacy files with no saved receipt."""
    pending = [(path, report) for path, report in reports if not report.get("published_utc")]
    evidence = publication_evidence(archive, pending, published_ref=published_ref)
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
