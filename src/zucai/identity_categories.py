"""Reviewable competition category proposals; never an approval mechanism."""

import hashlib
import json
from pathlib import Path

from zucai.sources.apif_aliases import valid_identity_category

PATH = Path(__file__).resolve().parents[2] / "data/identity_categories.json"


def load_categories(registry, path=None):
    path = Path(path or PATH)
    raw = path.read_bytes()
    document = json.loads(raw)
    output = {}
    for code, entry in document["competitions"].items():
        route = registry["providers"]["apif"].get(code)
        if not route or entry["league"] != route["config"]["league"]:
            raise ValueError("Category competition route mismatch")
        if not valid_identity_category(entry["category"]) or not entry.get("evidence"):
            raise ValueError("Category requires explicit category and reviewable evidence")
        if entry["status"] not in {"proposed", "approved"}:
            raise ValueError("Category status invalid")
        if entry["status"] == "approved" and not all(
            entry.get(key) for key in ("review_url", "owner_approval")
        ):
            raise ValueError("Approved category requires review and owner approval evidence")
        output[code] = dict(
            entry,
            provenance=dict(
                path="data/identity_categories.json",
                sha256=hashlib.sha256(raw).hexdigest(),
                pointer=f"/competitions/{code}",
            ),
        )
    return output
