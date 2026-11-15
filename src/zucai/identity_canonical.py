"""Offline, explicitly approved creation of JC numeric canonical identities."""

import argparse
import csv
import io
import json
import re
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from sqlalchemy import select, text

from zucai.db import get_engine, session_scope
from zucai.identity_binding import _category_veto, _utc
from zucai.models import Match, Team
from zucai.sources.apif_aliases import validate_approvals
from zucai.sources.base import _atomic_write
from zucai.sources.teams import normalize_team

RECEIPTS = "state/audit/jc_canonical_identities.csv"
FIELDS = ("official_team_id", "project_team_id", "canonical", "created_utc", "decision_json")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _evidence(team):
    return sorted(
        [
            dict(
                label=r["label"],
                match_id=r["match_id"],
                side=r["side"],
                originals=[
                    {k: p[k] for k in ("path", "sha256", "pointer") if k in p}
                    for p in r["provenance"]
                    if p.get("path") and p.get("sha256")
                ],
            )
            for r in team["official_name_evidence"]
        ],
        key=_json,
    )


def _entry(team):
    evidence = _evidence(team)
    category = team.get("category")
    blockers = []
    if team["identity_status"] != "creation_proposed":
        blockers.append("existing_or_conflicting_official_identity")
    if team.get("category_status") != "approved" or not category:
        blockers.append("category_not_approved_or_ambiguous")
    if any(_category_veto(name, category) for name in team["official_names"]):
        blockers.append("individual_team_category_requires_review")
    if not evidence or any(not row["originals"] for row in evidence):
        blockers.append("numeric_original_evidence_missing")
    return dict(
        status="proposed",
        action="create_canonical_identity",
        official_team_id=team["official_team_id"],
        canonical=normalize_team(team["name"]),
        official_names=team["official_names"],
        category=category,
        evidence=evidence,
        evidence_sha256=sha256(_json(evidence).encode()).hexdigest(),
        blockers=blockers,
    )


def propose(archive, *, codes=None):
    from zucai.identity_binding_archive import load_archive
    from zucai.league_registry import load_registry

    allowed = {
        c for c, e in load_registry()["providers"]["apif"].items() if e["status"] == "verified"
    }
    codes = sorted(allowed if codes is None else codes)
    if not codes or not set(codes) <= allowed:
        raise ValueError("Creation proposals require verified catalogue routes")
    facts = load_archive(archive, leagues=codes)
    rows = [
        _entry(team)
        for key, team in sorted(facts["teams"].items())
        if key.startswith("jc-team:") and team["identity_status"] == "creation_proposed"
    ]
    return dict(
        version=1,
        archive_sha256=facts["summary"]["manifest"]["sha256"],
        decisions=rows,
        codes=codes,
        production_writes=0,
    )


def _approval(decision):
    if (
        decision.get("status") != "approved"
        or decision.get("action") != "create_canonical_identity"
    ):
        raise ValueError("Creation requires explicit approved decision")
    validate_approvals({"creation": {"1": decision}}, require_metadata=True)
    if (
        decision.get("approved_by") != "qw5563680"
        or not isinstance(decision.get("review_url"), str)
        or not re.fullmatch(
            r"https://github\.com/qw5563680/FOOTBALL/pull/[1-9][0-9]*(?:#[A-Za-z0-9_-]+)?",
            decision["review_url"],
        )
        or _utc(decision["approved_utc"]) > datetime.now(UTC)
        or not re.fullmatch(r"[1-9][0-9]*", str(decision.get("official_team_id", "")))
    ):
        raise ValueError("Creation requires actual owner approval and review")


def _read_receipts(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        if reader.fieldnames != list(FIELDS):
            raise ValueError("Creation audit schema invalid")
        rows = list(reader)
    if any(None in row or None in row.values() for row in rows):
        raise ValueError("Creation audit row invalid")
    if len({r["official_team_id"] for r in rows}) != len(rows) or len(
        {r["project_team_id"] for r in rows}
    ) != len(rows):
        raise ValueError("Creation audit identity not unique")
    return rows


def _verify_creation_originals(root, decision, manifest):
    from zucai.identity_gate_evidence import proof

    evidence = decision["evidence"]
    if not evidence or sha256(_json(evidence).encode()).hexdigest() != decision["evidence_sha256"]:
        raise ValueError("Creation evidence digest invalid")
    for observation in evidence:
        matched = False
        side = observation["side"]
        if side not in {"home", "away"}:
            raise ValueError("Creation evidence side invalid")
        for reference in observation["originals"]:
            if manifest["files"].get(reference["path"]) != reference["sha256"]:
                raise ValueError("Creation original missing from archive manifest")
            raw = proof(root, reference)
            pointer = reference.get("pointer", "")
            if not pointer.startswith("/value/"):
                continue
            value = json.loads(raw)
            try:
                for part in pointer.lstrip("/").split("/"):
                    value = value[int(part)] if isinstance(value, list) else value[part]
            except (KeyError, IndexError, ValueError, TypeError) as exc:
                raise ValueError("Creation original pointer invalid") from exc
            official = value.get(side + "TeamId", value.get("sporttery" + side.title() + "TeamId"))
            match_id = value.get("matchId", value.get("sportteryMatchId"))
            if (
                str(official) == decision["official_team_id"]
                and "jc:" + str(match_id) == observation["match_id"]
            ):
                matched = True
        if not matched:
            raise ValueError("Creation original does not prove numeric match/team/side")


def apply_created_identities(root, teams, tables, manifest):
    """Restore the numeric association only with its authenticated approval receipt."""
    path = Path(root) / RECEIPTS
    if not path.exists():
        return
    from zucai.identity_gate_evidence import proof

    document = json.loads((Path(root) / "feed_manifest.json").read_bytes())
    digest = document["files"].get(RECEIPTS)
    if not digest:
        raise ValueError("Unlisted creation audit")
    proof(Path(root), dict(path=RECEIPTS, sha256=digest))
    projects = {str(t["id"]): t for t in tables["teams"]}
    owners = {}
    project_labels = {}
    for tid, project in projects.items():
        aliases = project["aliases"]
        if isinstance(aliases, str):
            aliases = json.loads(aliases)
        labels = {normalize_team(label).casefold() for label in (project["name"], *aliases)}
        project_labels[tid] = labels
        for label in labels:
            owners.setdefault(label, set()).add(tid)
    claimed = set()
    for receipt in _read_receipts(path):
        decision = json.loads(receipt["decision_json"])
        _approval(decision)
        tid = receipt["project_team_id"]
        official = receipt["official_team_id"]
        target = projects.get(tid)
        if target is None:
            raise ValueError("Created canonical team missing")
        aliases = target["aliases"]
        if isinstance(aliases, str):
            aliases = json.loads(aliases)
        if (
            decision["official_team_id"] != official
            or receipt["canonical"] != decision["canonical"]
            or target["name"] != decision["canonical"]
            or f"jc:team:{official}" not in aliases
            or not _utc(decision["approved_utc"])
            <= _utc(receipt["created_utc"])
            <= _utc(document["created_utc"])
        ):
            raise ValueError("Created identity receipt/snapshot inconsistent")
        marker = "jc:team:" + official
        markers = {label for label in project_labels[tid] if label.startswith("jc:team:")}
        if owners.get(marker) != {tid} or markers != {marker}:
            raise ValueError("Created official numeric marker has conflicting current owners")
        if tid in claimed:
            raise ValueError("Created project identity claimed twice")
        claimed.add(tid)
        _verify_creation_originals(Path(root), decision, document)
        team = teams.get("jc-team:" + official)
        if team is None:
            continue  # Reader may select other competitions.
        if team["ambiguous"] or team.get("project_team_id") not in (None, tid):
            raise ValueError("Created identity contradicts current numbered evidence")
        if any(
            t.get("project_team_id") == tid and key != "jc-team:" + official
            for key, t in teams.items()
        ):
            raise ValueError("Created project has another official owner")
        team.update(
            project_team_id=tid, project_name=target["name"], identity_status="existing_numbered"
        )
        team["project_identity_evidence"].append(
            dict(
                kind="approved_numeric_creation",
                manifest=manifest,
                path=RECEIPTS,
                sha256=digest,
                decision=decision,
                created_utc=receipt["created_utc"],
            )
        )


def apply_approvals(database, archive, approvals):
    from zucai.identity_binding_archive import _tables, load_archive

    database, archive = Path(database), Path(archive)
    if not database.is_file():
        raise ValueError("Creation requires an existing restored database")
    document = json.loads(Path(approvals).read_bytes())
    tables, _ = _tables(archive)
    if document.get("version") != 1:
        raise ValueError("Creation decision document version invalid")
    # archive_sha256 is descriptive only: approvals must survive unrelated
    # daily archive rewrites, so every decision is re-verified against the
    # current archive below instead of binding the whole archive digest.
    decisions = document.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        raise ValueError("Explicit creation decisions required")
    from zucai.league_registry import load_registry

    allowed = {
        c for c, e in load_registry()["providers"]["apif"].items() if e["status"] == "verified"
    }
    codes = document.get("codes")
    if not isinstance(codes, list) or not codes or not set(codes) <= allowed:
        raise ValueError("Creation decision route scope invalid")
    facts = load_archive(archive, leagues=codes)
    seen = set()
    for decision in decisions:
        _approval(decision)
        official = decision["official_team_id"]
        if official in seen:
            raise ValueError("Duplicate creation decision")
        seen.add(official)
        team = facts["teams"].get("jc-team:" + official)
        if team is None:
            raise ValueError("Official numeric evidence missing")
        expected = _entry(team)
        if expected["blockers"] or any(
            decision.get(k) != expected[k] for k in ("canonical", "category", "official_names")
        ):
            raise ValueError("Creation evidence/category/name invalid or identity already exists")
        # The approved evidence must be self-consistent and a subset of the
        # current evidence: matches added after approval must not invalidate it.
        approved_evidence = decision.get("evidence")
        if not approved_evidence or sha256(
            _json(approved_evidence).encode()
        ).hexdigest() != decision.get("evidence_sha256"):
            raise ValueError("Creation evidence digest invalid")
        current_evidence = {_json(row) for row in expected["evidence"]}
        if any(_json(row) not in current_evidence for row in approved_evidence):
            raise ValueError("Approved evidence is not a subset of current evidence")
    # The restored DB must carry the originals that its exported receipt will cite.
    # Merely pointing at an external archive would produce a non-portable approval.
    from zucai.cloud_feeds import STATE_FILE

    source_manifest = json.loads((archive / "feed_manifest.json").read_bytes())
    for decision in decisions:
        for observation in decision["evidence"]:
            for reference in observation["originals"]:
                for name in (reference["path"], reference["path"] + ".meta.json"):
                    if not re.fullmatch(STATE_FILE, name):
                        raise ValueError("Creation original is outside feeds export policy")
        try:
            _verify_creation_originals(database.parent, decision, source_manifest)
        except (KeyError, OSError) as exc:
            raise ValueError("Creation originals must be restored beside the database") from exc
        # The approval must not precede the capture of any original it cites.
        for observation in decision["evidence"]:
            for reference in observation["originals"]:
                meta_path = database.parent / (reference["path"] + ".meta.json")
                try:
                    meta = json.loads(meta_path.read_bytes())
                except (OSError, ValueError) as exc:
                    raise ValueError(
                        "Creation original metadata missing beside the database"
                    ) from exc
                fetched = meta.get("fetched_at_utc")
                if not fetched or _utc(decision["approved_utc"]) < _utc(fetched):
                    raise ValueError("Approval cannot precede original capture")
    audit = database.parent / RECEIPTS
    before = audit.read_bytes() if audit.exists() else None
    receipts = _read_receipts(audit)
    receipt_by_id = {r["official_team_id"]: r for r in receipts}
    created = []
    engine = get_engine(database)
    try:
        with session_scope(engine) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            known = list(session.scalars(select(Team)))
            expected_ids = {r["id"] for r in tables["teams"]} | {
                int(r["project_team_id"]) for r in receipts
            }
            if {t.id for t in known} != expected_ids:
                raise ValueError("Database team inventory changed since frozen proposal")
            for row in tables["teams"]:
                target = session.get(Team, row["id"])
                aliases = (
                    json.loads(row["aliases"])
                    if isinstance(row["aliases"], str)
                    else row["aliases"]
                )
                if (target.name, target.aliases) != (row["name"], aliases):
                    raise ValueError("Database team identity changed since frozen proposal")
            stored = list(session.scalars(select(Match)))
            if {m.match_id for m in stored} != {r["match_id"] for r in tables["matches"]}:
                raise ValueError("Database fixture inventory changed since proposal")
            for row in tables["matches"]:
                match = session.get(Match, row["match_id"])
                if any(
                    getattr(match, k) != row[k]
                    for k in ("source", "league", "home_team_id", "away_team_id")
                ) or _utc(match.kickoff_utc) != _utc(row["kickoff_utc"]):
                    raise ValueError("Database fixture identity changed since proposal")
            for decision in decisions:
                official = decision["official_team_id"]
                marker = "jc:team:" + official
                existing = receipt_by_id.get(official)
                if existing:
                    target = session.get(Team, int(existing["project_team_id"]))
                    if (
                        json.loads(existing["decision_json"]) != decision
                        or target is None
                        or target.name != decision["canonical"]
                        or marker not in target.aliases
                    ):
                        raise ValueError("Existing creation receipt differs from approved decision")
                    continue
                labels = {normalize_team(label).casefold() for label in decision["official_names"]}
                labels.update((decision["canonical"].casefold(), marker))
                if any(
                    labels & {normalize_team(s).casefold() for s in (t.name, *t.aliases)}
                    for t in known
                ):
                    raise ValueError(
                        "Canonical name/alias/numeric ID already has an owner; "
                        "review existing identity"
                    )
                target = Team(name=decision["canonical"], aliases=[marker])
                session.add(target)
                session.flush()
                known.append(target)
                receipt = dict(
                    official_team_id=official,
                    project_team_id=str(target.id),
                    canonical=target.name,
                    created_utc=datetime.now(UTC).isoformat(),
                    decision_json=_json(decision),
                )
                receipts.append(receipt)
                created.append(receipt)
            if created:
                stream = io.StringIO(newline="")
                writer = csv.DictWriter(stream, FIELDS)
                writer.writeheader()
                writer.writerows(receipts)
                audit.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write(audit, stream.getvalue().encode())
    except Exception:
        if before is None:
            audit.unlink(missing_ok=True)
        else:
            _atomic_write(audit, before)
        raise
    finally:
        engine.dispose()
    return dict(
        created=len(created),
        already_applied=len(decisions) - len(created),
        records=created,
        matches_changed=0,
        formal_bindings_changed=0,
        daily_changed=False,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["plan", "apply"])
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--codes")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--decisions", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.operation == "plan":
        result = propose(args.archive, codes=args.codes.split(",") if args.codes else None)
    else:
        if args.db is None or args.decisions is None:
            parser.error("apply requires --db and --decisions")
        result = apply_approvals(args.db, args.archive, args.decisions)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(args.out, (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode())


if __name__ == "__main__":
    main()
