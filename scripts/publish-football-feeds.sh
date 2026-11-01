#!/usr/bin/env bash
# Run from the data-branch checkout. Conflicts leave the remote untouched.
set -euo pipefail

git config user.name 'github-actions[bot]'
git config user.email '41898282+github-actions[bot]@users.noreply.github.com'
git add -- state matches feed_manifest.json
if ! git diff --cached --quiet; then
  git commit -m "Update football feeds from Actions run ${GITHUB_RUN_ID}/${GITHUB_RUN_ATTEMPT}"
fi
git pull --rebase origin data/football-feeds
git push origin HEAD:refs/heads/data/football-feeds
if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  printf '\n### Data branch\n\n`data/football-feeds` commit: `%s`\n' "$(git rev-parse HEAD)" >> "$GITHUB_STEP_SUMMARY"
fi
