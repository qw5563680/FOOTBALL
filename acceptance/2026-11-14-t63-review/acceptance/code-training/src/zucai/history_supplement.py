"""Offline, reviewed historical references; never writes the operational database."""

import argparse
import csv
import hashlib
import io
import json
import logging
import re
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PURPOSE = "historical-reference-only"
FILES = {"supplement.jsonl", "audit.json", "plan.json", "registry.json"}
COMPETITIONS = {"UEFA Euro": "欧洲杯", "UEFA Nations League": "欧国联"}
# This is a reviewed, fixed batch, not an unrestricted international-results importer.
NEW_FIXTURES = {
    "uefa:2036206": ("2024-07-05", "Portugal", "France", "UEFA Euro"),
    "uefa:2036209": ("2024-07-09", "Spain", "France", "UEFA Euro"),
    "uefa:2036211": ("2024-07-14", "Spain", "England", "UEFA Euro"),
}
NEW_IDS = set(NEW_FIXTURES)


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _json(value):
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _time(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _require(result.tzinfo is not None and result.utcoffset() == timedelta(0), "UTC time required")
    return result


def _checked_bytes(path, expected):
    content = Path(path).read_bytes()
    _require(_sha(content) == expected, f"SHA256 mismatch: {path}")
    return content


def _rows(data):
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))


def _key(row):
    return tuple(row[k] for k in ("date", "home_team", "away_team", "tournament"))


def _reference_row(plan, match):
    raw = match["source_row"]
    return {
        "match_id": match["reference_id"],
        "home": raw["home_team"],
        "away": raw["away_team"],
        "home_team_id": "apif:" + match["home_apif_team_id"],
        "away_team_id": "apif:" + match["away_apif_team_id"],
        "league": match["league"],
        "season": match["season"],
        "kickoff_utc": _time(match["kickoff_utc"]).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "home_goals_90": match["home_goals_90"],
        "away_goals_90": match["away_goals_90"],
        "played_extra_time": match["played_extra_time"],
        "neutral": match["neutral"],
        "venue": match["venue"],
        "venue_country": match["venue_country"],
        "historical_available_utc": None,
        "purpose": PURPOSE,
        "source": plan["source"],
        "raw_record": raw,
        "verification": match,
    }


def _validate_match(plan, match, registry):
    raw = match["source_row"]
    _require(re.fullmatch(r"uefa:\d+", match["reference_id"]), "UEFA reference ID required")
    _require(raw["tournament"] in COMPETITIONS, "Unreviewed competition")
    _require(match["league"] == COMPETITIONS[raw["tournament"]], "Competition mismatch")
    _require(raw["home_team"] != raw["away_team"], "Same team on both sides")
    for side in ("home", "away"):
        team_id = match[f"{side}_apif_team_id"]
        _require(team_id in registry, "Unknown team ID")
        _require(registry[team_id]["name"] == raw[f"{side}_team"], "Team name/ID mismatch")
        score = match[f"{side}_goals_90"]
        _require(type(score) is int and score >= 0, "Invalid regulation score")
        _require(re.fullmatch(r"\d+", raw[f"{side}_score"]), "Invalid source score")
        full_score = int(raw[f"{side}_score"])
        _require(full_score >= score, "Regulation score exceeds original final score")
        if not match["played_extra_time"]:
            _require(full_score == score, "Non-extra-time score mismatch")
    _require(type(match["played_extra_time"]) is bool, "Explicit extra-time status required")
    _require(match["score_period"] == "90_minutes_plus_stoppage", "Regulation proof required")
    _require(type(match["neutral"]) is bool, "Explicit neutral flag required")
    _require(raw["neutral"] in ("TRUE", "FALSE"), "Invalid source neutral flag")
    _require(match["neutral"] == (raw["neutral"] == "TRUE"), "Neutral flag mismatch")
    _require(match["venue_country"] == raw["country"] and match["venue"], "Venue mismatch")
    kickoff = _time(match["kickoff_utc"])
    _require(kickoff.date().isoformat() == raw["date"], "Date mismatch in reviewed UEFA sample")
    _require(kickoff < _time(plan["target_cutoff_utc"]), "Match not before frozen cutoff")
    now = datetime.now(UTC)
    _require(
        kickoff < _time(plan["source"]["fetched_at_utc"]) <= now, "Invalid source capture time"
    )
    _require(kickoff < _time(match["checked_at_utc"]) <= now, "Invalid verification time")
    for field in ("score_source_url", "venue_source_url"):
        _require(match[field].startswith("https://www.uefa.com/"), "UEFA proof URL required")
    uefa_id = match["reference_id"].split(":")[1]
    _require(
        f"/match/{uefa_id}--" in match["venue_source_url"]
        or match["venue_source_url"].endswith(f"/{uefa_id}_FR.pdf"),
        "UEFA proof identifies a different fixture",
    )
    _require(match["period_note"], "Explicit period verification required")
    if match["project_match_id"] is None:
        _require(match["reference_id"] in NEW_IDS, "Outside the three reviewed additions")
        _require(_key(raw) == NEW_FIXTURES[match["reference_id"]], "UEFA fixture identity mismatch")
        proof = f"https://www.uefa.com/newsfiles/EURO/2024/{uefa_id}_FR.pdf"
        _require(
            match["score_source_url"] == match["venue_source_url"] == proof,
            "Incorrect official report for the supplemental fixture",
        )
        clock = match["kickoff_evidence"]
        _require(
            clock["source_url"].startswith(
                ("https://www.fff.fr/", "https://www.englandfootball.com/")
            ),
            "Independent federation kickoff evidence required",
        )
        local = datetime.fromisoformat(clock["local_datetime"])
        _require(local.tzinfo is None, "Local clock must use its separate IANA zone")
        _require(
            local.replace(tzinfo=ZoneInfo(clock["timezone"])) == kickoff, "Kickoff clock mismatch"
        )


def _validate_plan(plan, registry):
    _require(plan["version"] == 1 and plan["purpose"] == PURPOSE, "Unsupported history plan")
    _require(plan["batch"] == "T51-international-results-19", "Unreviewed batch")
    source = plan["source"]
    _require(re.fullmatch(r"[0-9a-f]{40}", source["commit"]), "Pinned source commit required")
    expected_url = (
        "https://raw.githubusercontent.com/martj42/international_results/"
        + source["commit"]
        + "/results.csv"
    )
    _require(
        source["source_url"] == expected_url and source["http_status"] == 200, "Source mismatch"
    )
    baseline = plan["baseline"]
    _require(re.fullmatch(r"[0-9a-f]{40}", baseline["commit"]), "Pinned archive commit required")
    for name, sha in baseline["files"].items():
        _require(re.fullmatch(r"matches/\d{4}-\d{2}\.csv", name), "Invalid archive path")
        _require(re.fullmatch(r"[0-9a-f]{64}", sha), "Invalid archive digest")
    matches = plan["matches"]
    _require(len(matches) == 19, "Reviewed batch requires 19 distinct matches")
    _require(len({m["reference_id"] for m in matches}) == 19, "Duplicate reference identity")
    _require(len({_key(m["source_row"]) for m in matches}) == 19, "Duplicate source identity")
    _require(
        {m["reference_id"] for m in matches if m["project_match_id"] is None} == NEW_IDS,
        "Reviewed additions changed",
    )
    retained = [m["project_match_id"] for m in matches if m["project_match_id"] is not None]
    _require(
        len(set(retained)) == 16 and all(re.fullmatch(r"apif:\d+", r) for r in retained),
        "Invalid existing fixture identities",
    )
    for match in matches:
        _validate_match(plan, match, registry)
        month = "matches/" + match["source_row"]["date"][:7] + ".csv"
        _require(month in baseline["files"], f"Missing reviewed month: {month}")


def _audit_entry(match):
    raw = match["source_row"]
    return {
        "reference_id": match["reference_id"],
        "project_match_id": match["project_match_id"],
        "decision": "retain_existing" if match["project_match_id"] else "add_separate_reference",
        "source_score": [int(raw["home_score"]), int(raw["away_score"])],
        "verified_score_90": [match["home_goals_90"], match["away_goals_90"]],
    }


def _counts(audit):
    added = sum(r["decision"] == "add_separate_reference" for r in audit)
    return {
        "added": added,
        "retained": len(audit) - added,
        "source_score_differences": sum(r["source_score"] != r["verified_score_90"] for r in audit),
    }


def _prepare(plan, source_path, archive_path, registry_path):
    registry_data = _checked_bytes(registry_path, plan["registry_sha256"])
    registry = json.loads(registry_data)["teams"]
    _validate_plan(plan, registry)
    raw_rows = _rows(_checked_bytes(source_path, plan["source"]["sha256"]))
    existing = []
    for name, sha in plan["baseline"]["files"].items():
        path = archive_path / name
        _require(path.resolve().is_relative_to(archive_path.resolve()), "Archive path escapes root")
        rows = _rows(_checked_bytes(path, sha))
        _require(
            all(r["kickoff_utc"].startswith(Path(name).stem) for r in rows), "Wrong archive month"
        )
        existing.extend(rows)
    additions, audit = [], []
    for match in plan["matches"]:
        raw = match["source_row"]
        selected = [r for r in raw_rows if _key(r) == _key(raw)]
        _require(selected == [raw], f"Missing, changed or ambiguous source row: {_key(raw)}")
        home_names = {raw["home_team"], registry[match["home_apif_team_id"]]["canonical"]}
        away_names = {raw["away_team"], registry[match["away_apif_team_id"]]["canonical"]}
        candidates = [
            r
            for r in existing
            if r["kickoff_utc"][:10] == raw["date"]
            and (
                (r["home"] in home_names and r["away"] in away_names)
                or (r["away"] in home_names and r["home"] in away_names)
            )
        ]
        expected_id = match["project_match_id"]
        if expected_id is None:
            _require(
                not candidates, f"Already present or conflicting existing fixture: {_key(raw)}"
            )
            additions.append(_reference_row(plan, match))
        else:
            _require(
                len(candidates) == 1, f"Existing identity is missing or ambiguous: {expected_id}"
            )
            row = candidates[0]
            _require(row["match_id"] == expected_id, "Existing provider identity mismatch")
            _require(
                row["home"] in home_names and row["away"] in away_names, "Reversed existing teams"
            )
            _require(
                row["league"] == match["league"] and row["status"] == "finished",
                "Existing scope mismatch",
            )
            _require(
                _time(row["kickoff_utc"]) == _time(match["kickoff_utc"]),
                "Existing kickoff mismatch",
            )
            _require(
                [int(row["home_goals"]), int(row["away_goals"])]
                == [match["home_goals_90"], match["away_goals_90"]],
                "Existing reliable result conflict",
            )
        audit.append(_audit_entry(match))
    additions.sort(key=lambda r: r["match_id"])
    return {
        "supplement.jsonl": b"".join(_json(row) for row in additions),
        "audit.json": _json(audit),
        "plan.json": _json(plan),
        "registry.json": registry_data,
    }, _counts(audit)


def read_supplement(output_path):
    """Read a copied/exported reference bundle after validating every listed file."""
    root = Path(output_path)
    try:
        _require(not root.is_symlink(), "Symlinked output is not accepted")
        _require(
            all(not (root / n).is_symlink() for n in FILES | {"manifest.json"}), "Symlinked file"
        )
        manifest = json.loads((root / "manifest.json").read_bytes())
        _require(manifest["version"] == 1 and manifest["purpose"] == PURPOSE, "Unsupported bundle")
        _require(set(manifest["files"]) == FILES, "Incomplete bundle manifest")
        data = {n: _checked_bytes(root / n, sha) for n, sha in manifest["files"].items()}
        rows = [json.loads(line) for line in data["supplement.jsonl"].splitlines()]
        plan = json.loads(data["plan.json"])
        _require(_sha(data["registry.json"]) == plan["registry_sha256"], "Registry digest mismatch")
        _validate_plan(plan, json.loads(data["registry.json"])["teams"])
        audit = json.loads(data["audit.json"])
        _require(audit == [_audit_entry(m) for m in plan["matches"]], "Audit differs from plan")
        _require(manifest["counts"] == _counts(audit), "Bundle counts differ from audit")
        expected = sorted(
            (_reference_row(plan, m) for m in plan["matches"] if m["project_match_id"] is None),
            key=lambda r: r["match_id"],
        )
        _require(rows == expected and len(rows) == 3, "Reference records differ from reviewed plan")
        _require({r["match_id"] for r in rows} == NEW_IDS, "Unreviewed reference identity")
        _require(manifest["counts"]["added"] == len(rows), "Bundle count mismatch")
        return rows
    except (KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid historical reference bundle: {exc}") from exc


def build_supplement(*, plan_path, source_path, archive_path, registry_path, output_path):
    """Validate the complete reviewed batch before writing an independent immutable directory."""
    archive, output = Path(archive_path).resolve(), Path(output_path).resolve()
    _require(
        not output.is_relative_to(archive) and not archive.is_relative_to(output),
        "Output must be separate from the formal archive",
    )
    _require(not Path(output_path).is_symlink(), "Symlinked output is not accepted")
    try:
        plan = json.loads(Path(plan_path).read_bytes())
        files, counts = _prepare(plan, source_path, archive, registry_path)
    except (KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid history input: {exc}") from exc
    if output.exists():
        read_supplement(output)
        _require(
            all((output / name).read_bytes() == data for name, data in files.items()),
            "Refusing to replace existing reference bundle",
        )
        return {**counts, "added": 0}
    manifest = {
        "version": 1,
        "purpose": PURPOSE,
        "counts": counts,
        "files": {name: _sha(data) for name, data in files.items()},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".history-", dir=output.parent))
    try:
        for name, data in {**files, "manifest.json": _json(manifest)}.items():
            (stage / name).write_bytes(data)
        read_supplement(stage)
        stage.rename(output)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("plan", "source", "archive", "registry", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = build_supplement(**{key + "_path": value for key, value in vars(args).items()})
    logging.info(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
