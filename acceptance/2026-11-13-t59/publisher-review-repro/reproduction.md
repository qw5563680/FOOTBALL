# T59 publisher scope finding — original pre-fix reproduction

Read-only repository review, 2026-11-13. This evidence was captured before the implementer changed the publisher in response to the finding. Only temporary local Git repositories were used; no remote network or production archive was accessed.

## Finding

P2: `scripts/publish-football-feeds.sh:15–18` unconditionally appended `scoreboard/** -text` to `.gitattributes`, including when `PUBLISH_EXPECTED_BASE` selected candidate-review publication. Lines 31–34 then staged that file. Equivalent migrations existed at lines 8–28.

A supported older archive lacking one of those rules could therefore publish a change beyond the permitted queue, new audit files and feed manifest. The current fixed archive already contained every rule, so its acceptance run did not expose this case.

## Reproduction command

Run from `/workspace/work/football-t59` against the original pre-fix publisher:

```bash
PYTHONPATH=tests /workspace/work/t58-fresh311-venv/bin/python - <<'PY'
import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from test_publish_script import publication, execute, git

with TemporaryDirectory(prefix='t59-guarded-publish-scope-') as temporary:
    envset = publication.__wrapped__(Path(temporary))
    remote, seed, run, summary, env = envset
    git(run, 'config', 'user.name', 'Offline Review')
    git(run, 'config', 'user.email', 'offline@example.invalid')
    (run / 'scoreboard').mkdir()
    (run / 'scoreboard/ledger.jsonl').write_text('{"frozen":true}\n')
    (run / '.gitattributes').write_text('state/** -text\n')
    queue = run / 'state/alias_candidates/jc.json'
    queue.parent.mkdir()
    queue.write_text('{"status":"pending"}\n')
    git(run, 'add', '.')
    git(run, 'commit', '-m', 'seed older archive without scoreboard attribute')
    subprocess.run([env['TEST_REAL_GIT'], '-C', str(run), 'push', 'origin', 'HEAD'], env=env, check=True, capture_output=True)
    expected_base = git(run, 'rev-parse', 'HEAD')
    env['PUBLISH_EXPECTED_BASE'] = expected_base
    queue.write_text('{"status":"cleared"}\n')
    intended = git(run, 'diff', '--name-only').splitlines()
    result = execute(envset)
    actual = git(remote, 'diff-tree', '--no-commit-id', '--name-only', '-r', 'data/football-feeds').splitlines()
    print(json.dumps({'returncode': result.returncode, 'intended_changes': intended, 'committed_changes': actual, 'published_attributes': git(remote, 'show', 'data/football-feeds:.gitattributes')}, indent=2))
PY
```

## Observed pre-fix output

```json
{
  "returncode": 0,
  "intended_changes": [
    "state/alias_candidates/jc.json"
  ],
  "committed_changes": [
    ".gitattributes",
    "state/alias_candidates/jc.json"
  ],
  "published_attributes": "state/** -text\n\nscoreboard/** -text"
}
```

## Other verification

`/workspace/work/t58-fresh311-venv/bin/python -m pytest -q tests/test_t59_review_publish.py tests/test_publish_script.py` passed all 24 tests before the fix. The existing coverage correctly rejected normal concurrent remote advancement without rebase, but did not exercise an archive missing an attribute migration rule.
