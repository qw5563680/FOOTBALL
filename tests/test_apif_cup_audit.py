import csv
import json
from copy import deepcopy

from test_cup_identity import identity_inputs


def test_apif_cup_audit_ignores_runner_provenance_but_keeps_substantive_evidence(tmp_path):
    from zucai.sources.cup_identity import _audit_aliases, resolve_cup_aliases

    db, _, capture, _ = identity_inputs(tmp_path, "apif")
    resolve_cup_aliases("apif", db, {}, [capture], tmp_path / "audit")
    path = tmp_path / "audit/apif_cup_aliases.csv"
    before = path.read_bytes()
    changed = deepcopy(capture)
    changed.update(sidecar="/another/runner/raw/file.meta.json", seen_utc="2026-11-04T00:00:00Z")
    resolve_cup_aliases("apif", db, {}, [changed], tmp_path / "audit")
    assert path.read_bytes() == before
    rows = list(csv.DictReader(path.open()))
    old = deepcopy(rows[0])
    evidence = json.loads(old["evidence"])
    evidence[0]["seen_utc"] = "different"
    old["evidence"] = json.dumps(evidence)
    with path.open("a", newline="") as stream:
        csv.DictWriter(stream, rows[0].keys()).writerow(old)
    _audit_aliases(path, [])
    assert path.read_bytes() == before
    changed["raw_sha256"] = "b" * 64
    resolve_cup_aliases("apif", db, {}, [changed], tmp_path / "audit")
    assert len(list(csv.DictReader(path.open()))) == 4
