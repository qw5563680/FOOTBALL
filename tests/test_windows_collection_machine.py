"""Everything the Windows collection machine runs must start without POSIX-only modules."""

import re
import subprocess
import sys
import textwrap

import pytest

from test_stats_workflow import WORKFLOW, jobs

POSIX_ONLY = ("fcntl", "pwd", "grp", "resource", "termios")
# Modules the Windows jobs reach only inside functions, so a plain import would not show them.
LAZY = (
    "zucai.cloud_probe",
    "zucai.cloud_catalogue",
    "zucai.sources.sfc",
    "zucai.sources.okooo_sfc",
)


def windows_entrypoints():
    modules = set()
    for job in jobs().values():
        runner = re.search(r"^    runs-on: (.+)$", job, re.M).group(1)
        if "self-hosted" in runner or "FOOTBALL_FEEDS_RUNNER" in runner:
            modules.update(re.findall(r"-m (zucai[\w.]*)", job))
    return sorted(modules)


def test_windows_jobs_are_found():
    assert WORKFLOW.exists()
    assert {
        "zucai.cloud_feeds",
        "zucai.okooo_probe",
        "zucai.cloud_backup_source_probe",
    } <= set(windows_entrypoints())


@pytest.mark.parametrize("module", [*windows_entrypoints(), *LAZY])
def test_module_starts_without_posix_only_modules(module):
    code = textwrap.dedent(
        f"""
        import importlib, sys
        for name in {POSIX_ONLY!r}:
            sys.modules[name] = None  # importing it now fails, as it does on Windows
        importlib.import_module({module!r})
        """
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-600:]


def test_quota_refuses_to_spend_without_the_posix_lock(tmp_path, monkeypatch):
    from zucai.sources import apif_quota

    monkeypatch.setattr(apif_quota, "fcntl", None)
    quota = apif_quota.DailyQuota(tmp_path / "quota.json", reserve=0)
    with pytest.raises(RuntimeError, match="只支持 Linux"), quota.locked():
        pass
    assert not (tmp_path / "quota.json").exists()
