"""T68 开启检查：竞彩从不销售的预备队可以经所有者逐队批准后不绑定。

登记表只是声明；开启检查每次都重新核对：条目经所有者批准且批准不晚于检查截点，
类别是预备队，接口里这支球队出现过的每一个名称都带预备队标记，并且它不是任何
竞彩官方球队的绑定对象。任何一条不成立，这支球队仍按「本赛季球队未绑定」阻断。
它只放开「本赛季球队必须全部绑定」这一条，不建立身份，也不让这些比赛进入训练。
"""

import hashlib
import json
from pathlib import Path

from zucai.identity_binding import _category_veto, _utc
from zucai.identity_categories import PATH as CATEGORIES_PATH
from zucai.sources.apif_aliases import valid_identity_category, validate_approvals

REGISTRY = CATEGORIES_PATH.parent / "identity_reviews/t68-unsold-teams.json"


def load_unsold(path=None):
    """Registered entries by (competition code, provider team id); schema errors raise."""
    from zucai.identity_gate_evidence import proof

    path = Path(path or REGISTRY)
    if not path.exists():
        return {}
    raw = path.read_bytes()
    document = json.loads(raw)
    if set(document) != {"version", "teams"} or document["version"] != 1:
        raise ValueError("Unsold team registry schema invalid")
    output = {}
    for entry in document["teams"]:
        code, provider = entry["competition_id"], entry["provider_team_id"]
        if (
            (code, provider) in output
            or any(
                not isinstance(n, str) or not n.isdecimal() or int(n) < 1 for n in (code, provider)
            )
            or entry["status"] not in {"proposed", "approved"}
            or not valid_identity_category(entry["category"])
            or not entry["category"].endswith("/reserve")
            or not entry.get("evidence")
        ):
            raise ValueError("Unsold team entry identity/category/evidence invalid")
        observed = []
        for evidence in entry["evidence"]:
            proof(path.parent, evidence)
            observed.append(_utc(evidence["observed_utc"]))
        if entry["status"] == "approved":
            validate_approvals({"unsold": {provider: entry}}, require_metadata=True)
            if (
                entry.get("approved_by") != "qw5563680"
                or not entry.get("review_url")
                or max(observed) > _utc(entry["approved_utc"])
            ):
                raise ValueError("Unsold team entry requires actual owner approval and review")
        output[code, provider] = dict(
            entry,
            provenance=dict(
                path="data/identity_reviews/" + path.name,
                sha256=hashlib.sha256(raw).hexdigest(),
            ),
        )
    return output


def unsold_roster(facts, code, unbound, cutoff, *, registry=None):
    """Split unbound current-roster provider ids into verified unsold reserve sides and the rest.

    Returns ``(accepted, refused)``: accepted maps provider id to what was verified,
    refused maps provider id to the reason it still blocks.
    """
    registry = load_unsold() if registry is None else registry
    league = facts["configs"][code]["league"]
    bound_ids = {
        str(provider)
        for provider, entry in facts["formal"].get(league, {}).items()
        if entry.get("status") in {"approved", "auto_verified"}
    }
    labels, fixtures, first_team = {}, {}, set()
    for row in facts["apif"]:
        if row["competition_id"] != code:
            continue
        for side in ("home", "away"):
            labels.setdefault(str(row[side]), set()).add(row.get(f"{side}_name") or "")
            fixtures[str(row[side])] = fixtures.get(str(row[side]), 0) + 1
            if row.get(f"{side}_category"):
                first_team.add(row[f"{side}_category"])
    accepted, refused = {}, {}
    for provider in sorted(unbound, key=int):
        entry = registry.get((code, str(provider)))
        names = sorted(labels.get(str(provider), set()))
        if entry is None:
            reason = "not_registered"
        elif entry["status"] != "approved":
            reason = "not_approved"
        elif _utc(entry["approved_utc"]) > _utc(cutoff):
            reason = "approved_after_cutoff"
        elif str(provider) in bound_ids:
            reason = "provider_id_is_bound"
        elif len(first_team) != 1 or next(iter(first_team)).endswith("/reserve"):
            reason = "competition_category_not_first_team"
        elif not names or any(
            not name or not _category_veto(name, next(iter(first_team))) for name in names
        ):
            reason = "label_without_reserve_marker"
        else:
            accepted[str(provider)] = dict(
                provider_team_id=str(provider),
                labels=names,
                category=entry["category"],
                fixtures=fixtures.get(str(provider), 0),
                approved_utc=entry["approved_utc"],
                review_url=entry["review_url"],
                provenance=entry["provenance"],
            )
            continue
        refused[str(provider)] = dict(provider_team_id=str(provider), labels=names, reason=reason)
    return accepted, refused
