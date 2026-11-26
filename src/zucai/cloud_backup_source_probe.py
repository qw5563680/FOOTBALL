"""Run the public schedule probe from its read-only Actions job."""

import argparse
import json
import os
from pathlib import Path

from zucai.cloud_probe import require_probe_actions


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    require_probe_actions()
    if os.environ.get("ZUCAI_OPERATION") != "probe":
        parser.error("备用赛程探测仅供 operation=probe 使用")
    expected = {"probe_kind": "backup-sources"}
    try:
        options = json.loads(os.environ.get("DISPATCH_OPTIONS", "{}"))
    except json.JSONDecodeError:
        parser.error("options 必须为 JSON 对象")
    if options != expected:
        parser.error(f"备用赛程探测的 options 只接受 {json.dumps(expected)}")

    from zucai.backup_source_probe import collect_backup_source_probe

    collect_backup_source_probe(output=args.out)


if __name__ == "__main__":
    main()
