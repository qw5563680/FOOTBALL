"""Run independent league evaluations against one already frozen database.

No downloads and no database writes. Full JSON stays in --out (default work/).
Example: python examples/t15_model_selection.py --db work/t15/static.db \
    --as-of 2026-11-01T21:40:00Z --leagues 英超,西甲,德甲,意甲,法甲,荷甲,英冠,葡超
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from zucai.predict.parameters import write_parameters


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--leagues", required=True, help="Comma-separated canonical league names")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--out", type=Path, default=Path("work/t15/evaluations"))
    parser.add_argument("--params-file", type=Path, default=Path("data/model/params.json"))
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    args.out.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(args.db.read_bytes()).hexdigest()
    leagues = args.leagues.split(",")
    if not all(leagues) or len(set(leagues)) != len(leagues):
        parser.error("league names must be nonempty and unique")

    def run(league):
        # Canonical league names cannot be interpreted as paths or shell code.
        key = hashlib.sha256(league.encode()).hexdigest()[:12]
        report, params, log = (
            args.out / f"{key}{suffix}" for suffix in (".json", ".params.json", ".log")
        )
        if any(path.exists() for path in (report, params, log)):
            raise ValueError(f"{league}: output exists; use a new --out directory")
        env = dict(os.environ, OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1")
        command = [
            sys.executable,
            "-m",
            "zucai.cli",
            "tune",
            "--db",
            str(args.db.resolve()),
            "--league",
            league,
            "--source",
            "jc,fd",
            "--as-of",
            args.as_of,
            "--grid",
            "90,180,270,365,540,730",
            "--params-file",
            str(params),
        ]
        with report.open("w") as output, log.open("w") as error:
            result = subprocess.run(command, stdout=output, stderr=error, env=env, check=False)
        if result.returncode:
            raise RuntimeError(f"{league}: tune exited {result.returncode}; see {log}")
        data = json.loads(report.read_text())
        if data["database_sha256"] != digest:
            raise RuntimeError("database changed during evaluation")
        return league, data["parameters"], str(report)

    index = []
    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = [executor.submit(run, league) for league in leagues]
        for future in as_completed(futures):
            league, entry, path = future.result()
            # Only the parent writes the shared params file, sequentially.
            write_parameters(league, entry, args.params_file)
            index.append(
                dict(
                    league=league,
                    report=path,
                    half_life_days=entry["half_life_days"],
                    p2_ready=entry["p2_ready"],
                )
            )
            (args.out / "index.json").write_text(
                json.dumps(index, ensure_ascii=False, indent=2) + "\n"
            )
            print(json.dumps(index[-1], ensure_ascii=False), flush=True)
    if hashlib.sha256(args.db.read_bytes()).hexdigest() != digest:
        raise RuntimeError("database changed")


if __name__ == "__main__":
    main()
