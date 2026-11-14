"""Explicit cross-day pair decisions and actual application receipts, never links."""

import argparse
import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from zucai.db import get_engine, session_scope
from zucai.models import Match, Result, Team
from zucai.predict.training_sources import actual_result_leagues, apif_history_sources
from zucai.result_source_identity import load_result_identities
from zucai.result_source_links import _canonical_team, load_result_links
from zucai.sources.apif_aliases import validate_approvals
from zucai.sources.base import _atomic_write

REGISTRY_PATH = Path(__file__).resolve().parents[2] / "data/identity_reviews/t68-cross-day.json"
RECEIPT_FILE = "state/t68_cross_day_receipts.json"
ENTRY_FIELDS = {
    "decision_id",
    "decision",
    "match_ids",
    "snapshot",
    "approved_utc",
    "approved_by",
    "approval_ref",
    "evidence",
}


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _sha(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _time(value):
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.utcoffset() is None:
            raise ValueError
        return stamp.astimezone(UTC)
    except (ValueError, TypeError):
        raise ValueError("跨日决定时间须为带时区的时间") from None


def _read(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("跨日决定证据文件缺失或为符号链接")
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise ValueError("跨日决定文件无法读取") from exc


def _fields(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def decision_snapshot(session, match_ids):
    """Inspect exact current source rows, clocks, identities and related claims."""
    if not isinstance(match_ids, list) or len(match_ids) != 2 or len(set(match_ids)) != 2:
        raise ValueError("跨日决定须指定两个不同比赛 ID")
    rows = [session.get(Match, key) for key in sorted(match_ids)]
    results = [session.get(Result, key) for key in sorted(match_ids)]
    if any(row is None for row in (*rows, *results)):
        raise ValueError("跨日决定双方须有原始比赛与赛果")
    team_ids = {key for row in rows for key in (row.home_team_id, row.away_team_id)}
    teams = [session.get(Team, key) for key in sorted(team_ids)]
    if any(team is None for team in teams):
        raise ValueError("跨日决定球队缺失")
    links = load_result_links(session)
    pointers = list(
        session.execute(
            select(Match.match_id, Match.counterpart_id).where(Match.counterpart_id.is_not(None))
        )
    )
    # Include the complete touching graph, also through non-result and unselected sources.
    reached, related = set(match_ids), []
    remaining = [tuple(edge) for edge in (*links, *pointers)]
    while remaining:
        attached = [edge for edge in remaining if set(edge[:2]) & reached]
        if not attached:
            break
        related.extend(attached)
        reached.update(key for edge in attached for key in edge[:2])
        remaining = [edge for edge in remaining if edge not in attached]
    identities = load_result_identities(session)
    return {
        "matches": [_fields(row) for row in rows],
        "results": [_fields(row) for row in results],
        "teams": [_fields(team) for team in teams],
        "identity_verified_utc": {key: identities.get(key) for key in sorted(match_ids)},
        "related_claims": [list(edge) for edge in sorted(set(related))],
    }


def _scope(snapshot, pair):
    if not isinstance(snapshot, dict) or set(snapshot) != {
        "matches",
        "results",
        "teams",
        "identity_verified_utc",
        "related_claims",
    }:
        raise ValueError("跨日决定完整快照无效")
    try:
        left, right = snapshot["matches"]
        if sorted(row["match_id"] for row in (left, right)) != pair:
            raise ValueError
        league = left["league"]
        sources = {left["source"], right["source"]}
        delta = abs(_time(left["kickoff_utc"]) - _time(right["kickoff_utc"]))
        if (
            league != right["league"]
            or league not in actual_result_leagues()
            or len(sources) != 2
            or not sources & {"apif", "fdorg"}
            or not sources <= apif_history_sources()[league]
            or (left["home_team_id"], left["away_team_id"])
            != (right["home_team_id"], right["away_team_id"])
            or left["home_team_id"] == left["away_team_id"]
            or not timedelta(hours=3) < delta <= timedelta(hours=36)
            or any(
                row["status"] != "finished" or not row["match_id"].startswith(row["source"] + ":")
                for row in (left, right)
            )
            or any(not _canonical_team(team["name"]) for team in snapshot["teams"])
        ):
            raise ValueError
        results = {row["match_id"]: row for row in snapshot["results"]}
        known = []
        for row in (left, right):
            result = results[row["match_id"]]
            stamp = _time(result["available_utc"])
            if result["available_source"] not in (None, "captured", "assumed") or stamp <= _time(
                row["kickoff_utc"]
            ):
                raise ValueError
            known.append(stamp)
            if row["source"] in {"apif", "fdorg"}:
                known.append(_time(snapshot["identity_verified_utc"][row["match_id"]]))
                if result["available_source"] == "assumed":
                    known.append(_time(row["result_checked_utc"]))
        return max(known)
    except (KeyError, TypeError, ValueError):
        raise ValueError("跨日决定仅限规范双ID、同向同联赛、3至36小时的两份可知赛果") from None


def _entry(entry):
    if not isinstance(entry, dict) or set(entry) != ENTRY_FIELDS:
        raise ValueError("跨日批准条目字段无效")
    pair = entry["match_ids"]
    if (
        not isinstance(pair, list)
        or len(pair) != 2
        or not all(isinstance(key, str) and key for key in pair)
        or pair != sorted(set(pair))
        or entry["decision"] not in {"same_match", "different_match"}
        or any(
            not isinstance(entry[key], str) or not entry[key].strip()
            for key in ("decision_id", "approved_by", "approval_ref")
        )
    ):
        raise ValueError("跨日批准条目身份或结论无效")
    validate_approvals(
        {
            "cross_day": {
                entry["decision_id"]: dict(
                    status="approved",
                    approved_by=entry["approved_by"],
                    approved_utc=entry["approved_utc"],
                    approval_pr=entry["approval_ref"],
                )
            }
        },
        require_metadata=True,
    )
    if entry["approved_by"] != "qw5563680":
        raise ValueError("跨日决定需要所有者 qw5563680 的批准记录")
    approved = _time(entry["approved_utc"])
    if _scope(entry["snapshot"], pair) > approved:
        raise ValueError("跨日批准早于双方实际可知证据")
    evidence = entry["evidence"]
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("跨日批准须有实际核验原件")
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "observed_utc"}:
            raise ValueError("跨日原件字段无效")
        if (
            not isinstance(item["path"], str)
            or not item["path"]
            or not isinstance(item["sha256"], str)
            or len(item["sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in item["sha256"])
        ):
            raise ValueError("跨日原件路径或哈希无效")
        if _time(item["observed_utc"]) > approved:
            raise ValueError("跨日批准早于原件观察")
    return entry


def read_registry(path=None):
    document = _read(Path(path) if path is not None else REGISTRY_PATH)
    if (
        not isinstance(document, dict)
        or set(document) != {"schema_version", "decisions"}
        or type(document["schema_version"]) is not int
        or document["schema_version"] != 1
        or not isinstance(document["decisions"], list)
    ):
        raise ValueError("跨日批准清单格式无效")
    pairs, ids = set(), set()
    for entry in document["decisions"]:
        _entry(entry)
        pair = tuple(entry["match_ids"])
        if pair in pairs or entry["decision_id"] in ids:
            raise ValueError("跨日批准清单存在重复或冲突决定")
        pairs.add(pair)
        ids.add(entry["decision_id"])
    return document["decisions"]


def read_receipts(path):
    if not path.exists() and not path.is_symlink():
        return {"schema_version": 1, "receipts": []}
    document = _read(path)
    if (
        not isinstance(document, dict)
        or set(document) != {"schema_version", "receipts"}
        or type(document["schema_version"]) is not int
        or document["schema_version"] != 1
        or not isinstance(document["receipts"], list)
    ):
        raise ValueError("跨日应用回执格式无效")
    seen = set()
    for receipt in document["receipts"]:
        if not isinstance(receipt, dict) or set(receipt) != {
            "entry",
            "entry_sha256",
            "applied_utc",
            "evidence_base64",
            "receipt_sha256",
        }:
            raise ValueError("跨日应用回执字段无效")
        entry = _entry(receipt["entry"])
        if receipt["entry_sha256"] != _sha(entry) or receipt["receipt_sha256"] != _sha(
            {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        ):
            raise ValueError("跨日应用回执哈希不符")
        if receipt["entry_sha256"] in seen:
            raise ValueError("跨日应用回执重复")
        seen.add(receipt["entry_sha256"])
        if not _time(entry["approved_utc"]) <= _time(receipt["applied_utc"]) <= datetime.now(UTC):
            raise ValueError("跨日应用回执时标无效")
        encoded = receipt["evidence_base64"]
        if not isinstance(encoded, list) or len(encoded) != len(entry["evidence"]):
            raise ValueError("跨日应用回执原件缺失")
        for value, item in zip(encoded, entry["evidence"], strict=True):
            try:
                raw = base64.b64decode(value, validate=True)
            except (ValueError, TypeError):
                raise ValueError("跨日应用回执原件编码无效") from None
            if hashlib.sha256(raw).hexdigest() != item["sha256"]:
                raise ValueError("跨日应用回执原件哈希不符")
    return document


def load_decisions(session):
    """Configuration alone grants nothing; current matching receipts are mandatory."""
    database = session.connection().engine.url.database
    if not database or database == ":memory:":
        return []
    entries = {_sha(entry): entry for entry in read_registry()}
    document = read_receipts(Path(database).parent / RECEIPT_FILE)
    valid = []
    for receipt in document["receipts"]:
        entry = entries.get(receipt["entry_sha256"])
        if entry is None:
            continue
        try:
            current = decision_snapshot(session, entry["match_ids"])
        except ValueError:
            continue
        if current != entry["snapshot"]:
            continue
        valid.append(
            {
                "match_ids": entry["match_ids"],
                "decision": entry["decision"],
                "decision_id": entry["decision_id"],
                "effective_utc": receipt["applied_utc"],
            }
        )
    return valid


def apply_decisions(database, registry=None):
    """Write only the optional receipt; no match/result/identity/relationship mutation."""
    database = Path(database).resolve()
    if not database.is_file():
        raise ValueError("跨日决定数据库不存在")
    registry = Path(registry) if registry is not None else REGISTRY_PATH
    entries = read_registry(registry)
    path = database.parent / RECEIPT_FILE
    document = read_receipts(path)
    existing = {row["entry_sha256"] for row in document["receipts"]}
    engine = get_engine(database)
    try:
        with session_scope(engine) as session:
            for entry in entries:
                applied = datetime.now(UTC)
                if _time(entry["approved_utc"]) > applied:
                    raise ValueError("未来批准不能应用")
                if decision_snapshot(session, entry["match_ids"]) != entry["snapshot"]:
                    raise ValueError("跨日决定快照已变更")
                if _sha(entry) in existing:
                    continue
                evidence = []
                for item in entry["evidence"]:
                    source = Path(item["path"])
                    source = source if source.is_absolute() else registry.parent / source
                    if (
                        source.is_symlink()
                        or not source.is_file()
                        or source.stat().st_size > 1024 * 1024
                    ):
                        raise ValueError("跨日核验原件缺失、过大或为符号链接")
                    raw = source.read_bytes()
                    if hashlib.sha256(raw).hexdigest() != item["sha256"]:
                        raise ValueError("跨日核验原件哈希不符")
                    evidence.append(base64.b64encode(raw).decode("ascii"))
                receipt = {
                    "entry": entry,
                    "entry_sha256": _sha(entry),
                    "applied_utc": datetime.now(UTC).isoformat(),
                    "evidence_base64": evidence,
                }
                receipt["receipt_sha256"] = _sha(receipt)
                document["receipts"].append(receipt)
    finally:
        engine.dispose()
    if len(document["receipts"]) != len(existing):
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, _bytes(document) + b"\n")
    return document


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    apply = commands.add_parser("apply")
    apply.add_argument("--db", type=Path, required=True)
    apply.add_argument("--registry", type=Path, default=REGISTRY_PATH)
    args = parser.parse_args(argv)
    apply_decisions(args.db, args.registry)


if __name__ == "__main__":
    main()
