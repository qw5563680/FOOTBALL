#!/usr/bin/env bash
# Run from the data-branch checkout. Conflicts leave the remote untouched.
set -euo pipefail

git config user.name 'github-actions[bot]'
git config user.email '41898282+github-actions[bot]@users.noreply.github.com'
# Frozen hashes cover exact bytes, including T06 CSV CRLF on Windows checkouts.
if [[ -e forecast_manifest.json ]]; then
  for rule in 'models/** -text' 'forecasts/** -text whitespace=cr-at-eol' 'forecast_manifest.json -text'; do
    if ! sed 's/\r$//' .gitattributes 2>/dev/null | grep -Fxq -- "$rule"; then
      printf '\n%s\n' "$rule" >> .gitattributes
    fi
  done
fi
git add -- state matches feed_manifest.json
for path in .gitattributes models forecasts forecast_manifest.json; do
  if [[ -e "$path" ]]; then
    git add -- "$path"
  fi
done
if ! git diff --cached --quiet; then
  git commit -m "Update football feeds from Actions run ${GITHUB_RUN_ID}/${GITHUB_RUN_ATTEMPT}"
fi
git pull --rebase origin data/football-feeds
git push origin HEAD:refs/heads/data/football-feeds
if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  printf '\n### Data branch\n\n`data/football-feeds` commit: `%s`\n' "$(git rev-parse HEAD)" >> "$GITHUB_STEP_SUMMARY"
fi
