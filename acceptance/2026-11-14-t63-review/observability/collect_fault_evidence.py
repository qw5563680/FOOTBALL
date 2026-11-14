"""Summarize durable offline pytest outputs; never reads a service or modifies an archive."""
import json
from pathlib import Path

root = Path(__file__).resolve().parent
results = []
for path in sorted((root / "fault-injection").glob("*/archive/forecast_manifest.json")):
    case = path.parent.parent
    if case.is_symlink():
        continue
    manifest = json.loads(path.read_bytes())
    run = next(iter(manifest["runs"].values()))
    primary = {
        name: sha
        for name, sha in manifest["files"].items()
        if name.endswith(("/forecast.json", "/report.md", "/report.csv", "/manifest.json"))
        or name == "models/latest.json"
        or (name.endswith(".pkl") and "source-expanded" not in name)
    }
    results.append(
        dict(
            case=case.name,
            status=run["source_version_status"],
            primary_file_sha256=primary,
            primary_bytes_unchanged="asserted in passing fault-injection test",
            forecast_manifest=str(path),
            summary=str(case / "summary.md"),
            daily=[str(p) for p in (case / "daily").rglob("*.md")],
            candidate_files=[
                name
                for name in manifest["files"]
                if "source-expanded" in name or name.endswith("source_versions.json")
            ],
        )
    )
(root / "fault-injection-results.json").write_text(
    json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
print(f"Saved {len(results)} offline fault-injection cases.")
