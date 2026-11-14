"""Replay previously saved official catalogue pages without network or database access."""

import argparse
import logging
from pathlib import Path

from zucai.cloud_jc_catalogue import replay_jc_catalogue

LOG = logging.getLogger(__name__)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True, help="Existing JC run directory")
    parser.add_argument("--out-dir", type=Path, required=True, help="Separate output root")
    parser.add_argument("--run-id", required=True, help="New immutable replay run identifier")
    args = parser.parse_args(argv)
    report = replay_jc_catalogue(args.source_dir, args.out_dir, args.run_id)
    LOG.info(
        "JC catalogue replay %s: %s pages, %s; no network requests",
        report["run_id"],
        report["replayed_pages"],
        report["status"],
    )
    return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
