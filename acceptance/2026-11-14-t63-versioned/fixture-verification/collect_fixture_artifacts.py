"""Preserve already-generated synthetic cloud fixtures; never read formal archives."""

import argparse
import hashlib
import json
import shutil
from pathlib import Path

from zucai.forecast_archive import verify_forecasts

parser = argparse.ArgumentParser()
parser.add_argument("pytest_root", type=Path)
parser.add_argument("output", type=Path)
args = parser.parse_args()
cases = {
    "success": "test_run_restores_trains_and_f0/archive",
    "candidate_failed": "test_failed_source_candidate_f0/archive",
    "expanded_primary": "test_original_experimental_lea0/experimental-archive",
}
args.output.mkdir(parents=True, exist_ok=False)
summaries = {}
for name, relative in cases.items():
    source = args.pytest_root / relative
    before = verify_forecasts(source)
    target = args.output / name
    shutil.copytree(source, target)
    after = verify_forecasts(target)
    if before != after:
        raise ValueError(f"Copied fixture manifest changed: {name}")
    summaries[name] = {
        "synthetic": True,
        "source_test_directory": str(source),
        "verified": True,
        "run_ids": sorted(after["runs"]),
        "files": sum(path.is_file() for path in target.rglob("*")),
        "bytes": sum(path.stat().st_size for path in target.rglob("*") if path.is_file()),
    }
metadata = {
    "description": "Synthetic offline pytest artifacts, not real forecasts or performance evidence.",
    "inputs": ["data/fixtures/t16/train_history.csv", "data/fixtures/t17/upcoming.json"],
    "cases": summaries,
}
(args.output / "verification.json").write_text(
    json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
(args.output / "SHA256SUMS").write_text(
    "".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(args.output)}\n"
        for path in sorted(args.output.rglob("*"))
        if path.is_file()
    ),
    encoding="utf-8",
)
print(json.dumps(metadata, ensure_ascii=False, indent=2))
