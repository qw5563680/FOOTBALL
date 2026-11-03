import csv
import json
from pathlib import Path

from zucai.sources.apif_command import _audit

ROOT = Path(__file__).parents[1] / "data/fixtures/t10"


def audit_row():
    game = json.loads((ROOT / "synthetic_ingest.json").read_bytes())["fixture"]
    return dict(
        provider_match_id=game["provider_match_id"],
        home_provider_id=game["home_provider_id"],
        away_provider_id=game["away_provider_id"],
        league=game["league"],
        kickoff_utc=game["kickoff_utc"],
        reason="jc 候选身份不全",
        candidates=["jc:1"],
    )


def test_same_match_hash_reason_is_not_repeated_and_other_evidence_is_retained(tmp_path):
    path = tmp_path / "apif_suspected_duplicates.csv"
    row = audit_row()
    _audit(path, [row, row], "a" * 64)
    first = path.read_bytes()
    _audit(path, [row], "a" * 64)
    assert path.read_bytes() == first
    assert len(list(csv.DictReader(path.open()))) == 1
    _audit(path, [row], "b" * 64)
    _audit(path, [dict(row, reason="不同拒绝原因")], "a" * 64)
    assert len(list(csv.DictReader(path.open()))) == 3


def test_old_duplicate_rows_are_compacted_even_if_no_new_rejections(tmp_path):
    path = tmp_path / "apif_rejected_rows.csv"
    _audit(path, [audit_row()], "a" * 64)
    # Seed old duplicate bytes directly; fdorg's writer now deduplicates new rows too.
    lines = path.read_bytes().splitlines(keepends=True)
    history = b"".join([*lines, lines[-1]])
    path.write_bytes(history)
    fdorg = tmp_path / "fdorg_rejected_rows.csv"
    fdorg.write_bytes(history)
    _audit(path, [], "b" * 64)
    assert len(list(csv.DictReader(path.open()))) == 1
    assert fdorg.read_bytes() == history


def test_rows_without_provider_id_keep_distinct_row_numbers(tmp_path):
    path = tmp_path / "apif_rejected_rows.csv"
    rows = [dict(provider_match_id="", row_number=n, reason="缺少 ID") for n in (1, 2)]
    _audit(path, rows, "a" * 64)
    _audit(path, rows, "a" * 64)
    assert len(list(csv.DictReader(path.open()))) == 2
