"""Prove the final datetime-input compatibility edit preserves the fitted corpus."""

import difflib
import hashlib
import json
import sqlite3
from contextlib import ExitStack, closing
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from zucai.sources.apif_stats import instant

ROOT = Path(__file__).parent
LIVE = Path("/workspace/work/football-t63")
BASE = Path("/workspace/work/t66-review-20261114/real-acceptance/fixed/work/data/zucai.db")
WORK = ROOT / "work/data/zucai.db"
RELATIVE = "src/zucai/source_result_quarantine.py"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    old = (ROOT / "code-training" / RELATIVE).read_text()
    new = (LIVE / RELATIVE).read_text()
    assert old.count("datetime.fromisoformat(") == 2
    expected = old.replace(
        "from datetime import datetime, timedelta", "from datetime import timedelta"
    )
    expected = expected.replace(
        "from zucai.predict.training_sources import actual_result_leagues\n",
        "from zucai.predict.training_sources import actual_result_leagues\n"
        "from zucai.sources.apif_stats import instant\n",
    ).replace("datetime.fromisoformat(", "instant(")
    assert expected == new, "Source change exceeds the two timestamp normalizations and imports"
    (ROOT / "final-normalization-source.diff").write_text(
        "".join(
            difflib.unified_diff(
                old.splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile="actual-19-fit/source_result_quarantine.py",
                tofile="final-live/source_result_quarantine.py",
            )
        )
    )
    training = json.loads((ROOT / "training-imported-modules.json").read_text())
    changed = {}
    for module, row in training.items():
        relative = "src/" + row["path"].split("/src/", 1)[1]
        current = sha(LIVE / relative)
        if current != row["sha256"]:
            changed[module] = dict(
                path=relative, trained_sha256=row["sha256"], final_sha256=current
            )
    assert set(changed) == {"zucai.source_result_quarantine"}
    calls = []

    def blocked(*args, **kwargs):
        calls.append(repr(args[:2]))
        raise AssertionError("Network blocked for normalization equivalence proof")

    databases = {}
    timestamp_maps = []
    with ExitStack() as stack:
        for name in (
            "requests.sessions.Session.request",
            "socket.create_connection",
            "socket.socket.connect",
        ):
            stack.enter_context(patch(name, side_effect=blocked))
        for label, database in (("original_t66", BASE), ("latest_restored_work", WORK)):
            before = sha(database)
            digest = hashlib.sha256()
            count = 0
            values = {}
            with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as db:
                db.execute("PRAGMA query_only=ON")
                for key, value in db.execute(
                    "SELECT match_id,kickoff_utc FROM matches ORDER BY match_id"
                ):
                    assert isinstance(value, str)
                    previous = datetime.fromisoformat(value)
                    current = instant(value)
                    assert previous == current and previous.isoformat() == current.isoformat(), key
                    assert previous.utcoffset() == current.utcoffset(), key
                    assert instant(previous) == previous, key
                    values[key] = value
                    digest.update(
                        (
                            json.dumps([key, value, previous.isoformat(), current.isoformat()])
                            + "\n"
                        ).encode()
                    )
                    count += 1
            assert sha(database) == before
            timestamp_maps.append(values)
            databases[label] = dict(
                path=str(database),
                database_sha256=before,
                match_rows=count,
                exact_value_and_iso_equality=True,
                all_datetime_inputs_also_supported=True,
                per_row_normalization_digest=digest.hexdigest(),
                database_unchanged=True,
            )
    assert timestamp_maps[0] == timestamp_maps[1]
    assert not calls
    report = dict(
        actual_fit_imported_module_count=len(training),
        changed_fit_modules=changed,
        source_diff_is_only_two_normalizer_calls_and_imports=True,
        databases=databases,
        all_match_kickoffs_equal_across_databases=True,
        original_19_fit_models_retained=True,
        retraining_required_for_this_edit=False,
        rationale=(
            "All real timestamp values and their pairwise differences are unchanged; "
            "only support for already-datetime session values is added."
        ),
        http_calls=calls,
    )
    (ROOT / "final-normalization-compatibility.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                "actual_fit_modules": len(training),
                "changed_modules": list(changed),
                "rows_per_database": {k: v["match_rows"] for k, v in databases.items()},
                "all_timestamp_values_equal": True,
                "http_calls": calls,
            }
        )
    )


if __name__ == "__main__":
    main()
