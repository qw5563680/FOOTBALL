"""Frozen-manifest authentication for the identity reader (no collection writes)."""

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

from zucai import jc_catalogue_kickoffs as kickoffs
from zucai.identity_binding import _utc


class FrozenEvidence:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.manifest = json.loads((self.root / "feed_manifest.json").read_bytes())
        self.observed_utc = self.manifest["created_utc"]
        _utc(self.observed_utc)
        self.checked = set()

    def verify(self, relative):
        path = self.root / relative
        if not path.resolve().is_relative_to(self.root):
            raise ValueError("Frozen manifest path escapes archive")
        if relative not in self.checked:
            expected = self.manifest["files"].get(relative)
            if not expected or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError(f"Frozen manifest evidence mismatch: {relative}")
            self.checked.add(relative)
        return path

    def original(self, relative):
        path = self.verify(relative)
        metadata = json.loads(self.verify(relative + ".meta.json").read_bytes())
        if _utc(metadata["fetched_at_utc"]) > _utc(self.observed_utc):
            raise ValueError("Frozen original capture is later than its manifest")
        return path

    def catalogue(self):
        prefix = f"state/catalogue/{kickoffs.CATALOGUE_SOURCE_RUN}/"
        self.verify(prefix + "jc.json")
        for path in sorted((self.root / prefix / "jc").rglob("*.json")):
            relative = path.relative_to(self.root).as_posix()
            if not relative.endswith(".meta.json"):
                self.original(relative)
        return kickoffs._catalogue(self.root)

    def clocks(self, rows, catalogue):
        """Archived snapshots prove their own clock only; current identities are separate."""
        snapshots = sorted((self.root / "state/catalogue").glob("*/jc-db-manifests/*.json"))
        for path in snapshots:
            self.verify(path.relative_to(self.root).as_posix())
            folder = path.parent.parent
            for name in ("matches.jsonl", "teams.jsonl", "aliases.json"):
                self.verify((folder / ("jc-db-" + name)).relative_to(self.root).as_posix())
            snapshot = json.loads(path.read_bytes())
            limit = min(
                _utc(self.observed_utc), _utc(snapshot.get("created_utc", self.observed_utc))
            )
            columns = snapshot["columns"]["matches"]
            for line in (folder / "jc-db-matches.jsonl").read_bytes().splitlines():
                record = dict(zip(columns, json.loads(line), strict=True))
                checked = record.get("result_checked_utc")
                if checked and _utc(checked) > limit:
                    raise ValueError("Frozen snapshot verification is later than its manifest")
        if snapshots:
            db, issues = kickoffs._database_evidence(self.root, rows, catalogue)
        else:
            db, issues = {}, []
        output, paths = defaultdict(list), defaultdict(list)
        snapshot_rows = {}
        for key, proofs in db.items():
            for proof in proofs:
                relative = proof["matches_path"]
                if relative not in snapshot_rows:
                    columns = json.loads(self.verify(proof["manifest_path"]).read_bytes())[
                        "columns"
                    ]["matches"]
                    snapshot_rows[relative] = {
                        values[0]: dict(zip(columns, values, strict=True))
                        for line in self.verify(relative).read_bytes().splitlines()
                        if (values := json.loads(line))
                    }
                output[key].append(
                    dict(
                        proof,
                        verified_utc=self.observed_utc,
                        clock_only=True,
                        match_snapshot=snapshot_rows[relative][f"jc:{key}"],
                    )
                )
        for relative in sorted(self.manifest["files"]):
            if not relative.endswith(".json") or relative.endswith(".meta.json"):
                continue
            head = re.search(
                r"/jc-heads/(?:conflicts/)?([1-9][0-9]*)(?:-[0-9a-f]{64})?\.json$", relative
            )
            legacy = re.search(r"(?:^|/)raw/.*/match-([1-9][0-9]*)_", relative)
            schedule = "/jc-schedules/" in relative
            if not (head or legacy or schedule):
                continue
            path = self.original(relative)
            if head or legacy:
                paths[int((head or legacy)[1])].append(path)
            else:
                value = kickoffs.official_value(path.read_bytes())
                for group in value.get("matchInfoList", []):
                    for item in group.get("subMatchList", []):
                        if type(item.get("matchId")) is int:
                            paths[item["matchId"]].append(path)
        for row in rows:
            for path in paths[row["match_id"]]:
                try:
                    output[row["match_id"]].append(kickoffs._raw_evidence(path, row, self.root))
                except kickoffs.TrustedJcContradiction as exc:
                    output[row["match_id"]].append(exc.evidence)
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    issues.append(dict(match_id=row["match_id"], reason=str(exc)))
        return output, issues
