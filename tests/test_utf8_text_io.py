"""Guard: text files under src/zucai are read with an explicit UTF-8 encoding.

The Windows self-hosted runner defaults to GBK; run 41877829928 failed restoring the
feeds archive because the rows manifest (UTF-8 with Chinese league names) was read with
``Path.read_text()`` and no encoding.
"""

import re
from pathlib import Path

SRC = Path(__file__).parents[1] / "src/zucai"


def test_no_read_text_without_explicit_encoding():
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\.read_text\(\s*\)", line):
                offenders.append(f"{path.relative_to(SRC.parent)}:{number}: {line.strip()}")
    assert offenders == [], "\n".join(offenders)
