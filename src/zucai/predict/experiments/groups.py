"""Prespecified subgroup intervals computed only from saved, unchanged predictions."""

import hashlib
import io
import json
import subprocess
from pathlib import Path

import zstandard

from zucai.predict.experiments.evaluate import load_protocol
from zucai.predict.experiments.metrics import paired


def subgroup_comparisons(rows, protocol):
    result = {}
    options = {key: protocol["bootstrap"][key] for key in ("replicates", "seed")}
    for field in ("cold_start", "statistics_missing"):
        result[field] = {}
        for flag in (True, False):
            subset = [row for row in rows if row.get(field) == flag]
            comparisons = {}
            for suffix in ("raw", "calibrated"):
                for a, b in (
                    ("candidate", "baseline"),
                    ("ablation", "baseline"),
                    ("candidate", "ablation"),
                ):
                    left, right = f"{a}_{suffix}", f"{b}_{suffix}"
                    comparisons[f"{left}-{right}"] = {
                        scope: paired(subset, left, right, common_experiment=common, **options)
                        for scope, common in (("all", False), ("common_experiment", True))
                    }
            result[field][str(flag).lower()] = dict(targets=len(subset), paired=comparisons)
    return result


def _sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_group_report(archive, *, generator_commit=None):
    archive = Path(archive)
    output = archive / "group_comparisons.json"
    if output.exists():
        raise FileExistsError("分组区间产物已存在，禁止覆盖")
    protocol = load_protocol(archive / "protocol.json")
    manifest = json.loads((archive / "input_manifest.json").read_bytes())
    summary = json.loads((archive / "summary.json").read_bytes())
    scopes, pooled, hashes = {}, {}, {}
    for scope, modes in summary["scopes"].items():
        if modes.get("status") == "not_covered":
            scopes[scope] = modes
            continue
        scopes[scope] = {}
        provider = scope.split("-", 1)[0]
        for mode in protocol["availability_modes"]:
            file = archive / f"{scope}-{mode}-predictions.jsonl.zst"
            hashes[file.name] = _sha(file)
            rows = []
            with (
                file.open("rb") as source,
                zstandard.ZstdDecompressor().stream_reader(source) as stream,
            ):
                for line in io.TextIOWrapper(stream, encoding="utf-8"):
                    row = json.loads(line)
                    row["variants"] = {
                        name: None
                        if value is None
                        else {
                            "probabilities": value["probabilities"],
                            "score_brier": value.get("score_brier"),
                        }
                        for name, value in row["variants"].items()
                    }
                    rows.append(
                        {
                            key: row[key]
                            for key in (
                                "actual",
                                "kickoff_utc",
                                "experimental",
                                "cold_start",
                                "statistics_missing",
                                "variants",
                            )
                        }
                    )
            scopes[scope][mode] = subgroup_comparisons(rows, protocol)
            pooled.setdefault(f"{provider}-{mode}", []).extend(rows)
    generator_commit = (
        generator_commit
        or subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[4], text=True
        ).strip()
    )
    report = dict(
        version="t33-subgroup-intervals-v1",
        prediction_code_commit=manifest["code_commit"],
        generator_commit=generator_commit,
        generator_sha256=_sha(Path(__file__)),
        predictions_sha256=hashes,
        original_summary_sha256=_sha(archive / "summary.json"),
        protocol_sha256=manifest["protocol_sha256"],
        source_commit=manifest["source_commit"],
        refitted=False,
        reselected=False,
        scopes=scopes,
        pooled={scope: subgroup_comparisons(rows, protocol) for scope, rows in pooled.items()},
    )
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    return output
