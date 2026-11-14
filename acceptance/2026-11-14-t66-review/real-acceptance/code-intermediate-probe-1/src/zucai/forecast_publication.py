"""Post-push forecast receipts and read-only legacy publication evidence."""

import argparse
import hashlib
import logging
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from zucai.forecast_archive import MANIFEST, _write, verify_forecasts
from zucai.publication_history import (
    PublicationHistoryError,
    check_history,
    git_read,
    resolve_history,
)
from zucai.report.render import _timestamp
from zucai.sfc_publication import PUBLISHED_REF
from zucai.sources.base import _atomic_write

LOG = logging.getLogger(__name__)


def candidates(manifest):
    for run in (manifest or {}).get("runs", {}).values():
        for entry in run["forecasts"].values():
            yield entry, entry["path"] + "/forecast.json", ""
            for prefix in ("experimental_", "parlay_"):
                if entry.get(prefix + "path"):
                    yield entry, entry[prefix + "path"], prefix


def git_evidence(archive, paths, *, published_ref=PUBLISHED_REF, repository=None):
    """First additions reachable from the data remote, verified against exact bytes.

    A separate repository is permitted for exported, immutable offline snapshots;
    both snapshot and historical blob must match the caller's manifest SHA256.
    No fetch, checkout, index update, or archive write is performed here.
    """
    if not re.fullmatch(r"refs/remotes/[^/]+/data/football-feeds", published_ref):
        raise ValueError("发布时间必须来自远程 data/football-feeds 分支")
    root = Path(repository or archive).resolve()

    def git(*args, input=None):
        return git_read(root, *args, input=input)

    if not paths:
        return {}
    try:
        check_history(root, published_ref)
        history = git(
            "log",
            "--reverse",
            "--topo-order",
            "--format=%H%x09%cI",
            "--diff-filter=A",
            "--name-only",
            published_ref,
            "--",
            "forecasts",
        ).decode()
        additions = {}
        commit = stamp = None
        for line in history.splitlines():
            if re.match(r"^[0-9a-f]{40,64}\t", line):
                commit, stamp = line.split("\t", 1)
            elif line in paths and line not in additions and commit:
                additions[line] = (commit, stamp)
        raw = git(
            "cat-file",
            "--batch",
            input="".join(f"{commit}:{path}\n" for path, (commit, _) in additions.items()).encode(),
        )
        evidence, offset = {}, 0
        for path, (commit, stamp) in additions.items():
            newline = raw.index(b"\n", offset)
            header = raw[offset:newline].split()
            if len(header) != 3 or header[1] != b"blob":
                raise PublicationHistoryError("数据分支历史不可用：Git blob 响应无效")
            size = int(header[2])
            offset = newline + 1
            sha = hashlib.sha256(raw[offset : offset + size]).hexdigest()
            offset += size + 1
            if (
                sha != paths[path]
                or hashlib.sha256((archive / path).read_bytes()).hexdigest() != sha
            ):
                continue
            evidence[path] = dict(
                candidate_path=path,
                candidate_sha256=sha,
                commit=commit,
                published_utc=_timestamp(stamp).isoformat(),
                ref=published_ref,
                method="historical_first_containing_commit",
            )
        return evidence
    except (OSError, subprocess.SubprocessError, ValueError, UnicodeError) as exc:
        if isinstance(exc, PublicationHistoryError):
            raise
        raise PublicationHistoryError(
            "数据分支历史不可用：Git 历史或原件读取失败（" + type(exc).__name__ + "）"
        ) from exc


def publication_times(
    archive, manifest, *, published_ref=PUBLISHED_REF, repository=None, pending=None
):
    """New batches require receipts; old batches alone may use commit timestamps."""
    resolved, legacy = {}, {}
    entries = list(candidates(manifest))
    for entry, path, prefix in entries:
        receipt = entry.get(prefix + "publication_evidence")
        published = entry.get(prefix + "published_utc")
        if receipt:
            try:
                valid = (
                    receipt["method"] == "post_push_confirmation"
                    and re.fullmatch(r"[0-9a-f]{40,64}", receipt["commit"])
                    and re.fullmatch(r"refs/remotes/[^/]+/data/football-feeds", receipt["ref"])
                    and receipt["candidate_path"] == path
                    and receipt["candidate_sha256"] == manifest["files"][path]
                    and hashlib.sha256((Path(archive) / path).read_bytes()).hexdigest()
                    == manifest["files"][path]
                    and receipt["published_utc"] == published
                    and _timestamp(published).astimezone(UTC)
                    >= _timestamp(entry["generated_utc"]).astimezone(UTC)
                )
            except (KeyError, ValueError, TypeError, OverflowError):
                valid = False
            if valid:
                resolved[path] = receipt
        elif not entry.get("publication_required"):
            legacy[path] = (manifest["files"][path], entry["generated_utc"])
    resolved.update(
        resolve_history(
            archive,
            "forecast",
            legacy,
            lambda paths: git_evidence(
                archive, paths, published_ref=published_ref, repository=repository
            ),
            pending=pending,
        )
    )
    return resolved


def confirm_publication(archive, *, published_ref=PUBLISHED_REF, clock=None):
    """Called only after successful push and refresh of the remote-tracking ref.

    Commit time is never used as the new receipt time. The timestamp is sampled
    after checking the published bytes, so pre-push computation cannot qualify.
    """
    archive = Path(archive).resolve()
    manifest = verify_forecasts(archive)
    pending = [
        (entry, path, prefix)
        for entry, path, prefix in candidates(manifest)
        if entry.get("publication_required") and not entry.get(prefix + "publication_evidence")
    ]
    evidence = git_evidence(
        archive,
        {path: manifest["files"][path] for _, path, _ in pending},
        published_ref=published_ref,
    )
    # One conservative confirmation time for all files observed on the remote.
    now = (clock or (lambda: datetime.now(UTC)))().isoformat()
    changed = []
    for entry, path, prefix in pending:
        if path not in evidence or _timestamp(now) < _timestamp(entry["generated_utc"]):
            continue
        receipt = dict(
            evidence[path],
            published_utc=_timestamp(now).isoformat(),
            method="post_push_confirmation",
        )
        entry[prefix + "published_utc"] = receipt["published_utc"]
        entry[prefix + "publication_evidence"] = receipt
        changed.append(path)
    if changed:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory(prefix=".publication-", dir=archive) as temporary:
            staged = Path(temporary) / MANIFEST
            _write(staged, manifest)
            _atomic_write(archive / MANIFEST, staged.read_bytes())
    return changed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--published-ref", default=PUBLISHED_REF)
    args = parser.parse_args(argv)
    confirm_publication(args.archive, published_ref=args.published_ref)


if __name__ == "__main__":
    main()
