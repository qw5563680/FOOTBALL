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


EXCEPTIONS = PATH.parent / "identity_reviews/t68-team-categories.json"


def apply_team_exceptions(apif, teams, registry):
    """An approved exception changes category/name vetoes only, never identity edges."""
    from datetime import UTC, datetime

    from zucai.identity_binding import _utc
    from zucai.identity_gate_evidence import proof
    from zucai.sources.apif_aliases import validate_approvals

    raw = EXCEPTIONS.read_bytes()
    document = json.loads(raw)
    if set(document) != {"version", "exceptions"} or document["version"] != 1:
        raise ValueError("Team category exception registry schema invalid")
    seen = set()
    for entry in document["exceptions"]:
        code, official, provider = [
            entry[k] for k in ("competition_id", "official_team_id", "provider_team_id")
        ]
        key = code, official
        if (
            key in seen
            or any(
                not isinstance(n, str) or not n.isdecimal() or int(n) < 1
                for n in (code, official, provider)
            )
            or code not in registry["providers"]["apif"]
            or not valid_identity_category(entry["category"])
            or entry["status"] not in {"proposed", "approved"}
            or not isinstance(entry.get("allowed_labels"), list)
            or any(not isinstance(s, str) or not s.strip() for s in entry["allowed_labels"])
            or not entry.get("evidence")
        ):
            raise ValueError("Team category exception identity/category/scope/evidence invalid")
        seen.add(key)
        observed = []
        for evidence in entry["evidence"]:
            proof(EXCEPTIONS.parent, evidence)
            observed.append(_utc(evidence["observed_utc"]))
        if entry["status"] == "approved":
            validate_approvals({"exception": {official: entry}}, require_metadata=True)
            if (
                entry.get("approved_by") != "qw5563680"
                or not entry.get("review_url")
                or not max(observed) <= _utc(entry["approved_utc"]) <= datetime.now(UTC)
            ):
                raise ValueError(
                    "Team category exception requires actual owner approval and review"
                )
        item = dict(
            entry,
            provenance=dict(
                path="data/identity_reviews/t68-team-categories.json",
                sha256=hashlib.sha256(raw).hexdigest(),
            ),
        )
        team = teams.get("jc-team:" + official)
        if team:
            team.setdefault("team_category_exceptions", []).append(item)
            if entry["status"] == "approved":
                team.setdefault("category_overrides", {})[code] = item
        for row in apif:
            if row["competition_id"] != code:
                continue
            for side in ("home", "away"):
                if row[side] == provider and entry["status"] == "approved":
                    row[side + "_category"] = entry["category"]
                    row[side + "_category_exception"] = item
