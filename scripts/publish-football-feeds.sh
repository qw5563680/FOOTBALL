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
if [[ -d scoreboard ]]; then
  if ! sed 's/\r$//' .gitattributes 2>/dev/null | grep -Fxq -- 'scoreboard/** -text'; then
    printf '\n%s\n' 'scoreboard/** -text' >> .gitattributes
  fi
fi
if [[ -d daily ]]; then
  if ! sed 's/\r$//' .gitattributes 2>/dev/null | grep -Fxq -- 'daily/** -text'; then
    printf '\n%s\n' 'daily/** -text' >> .gitattributes
  fi
fi
if [[ -d sfc ]]; then
  if ! sed 's/\r$//' .gitattributes 2>/dev/null | grep -Fxq -- 'sfc/** -text whitespace=cr-at-eol'; then
    printf '\n%s\n' 'sfc/** -text whitespace=cr-at-eol' >> .gitattributes
  fi
fi
git add -- state matches feed_manifest.json
for path in .gitattributes models forecasts forecast_manifest.json scoreboard daily sfc; do
  if [[ -e "$path" ]]; then
    git add -- "$path"
  fi
done
if ! git diff --cached --quiet; then
  git commit -m "Update football feeds from Actions run ${GITHUB_RUN_ID}/${GITHUB_RUN_ATTEMPT}"
fi
push_errors=$(mktemp)
trap 'rm -f "$push_errors"' EXIT
for attempt in 1 2 3; do
  # A failed rebase (including conflicts) exits immediately, without another push.
  git pull --rebase origin data/football-feeds
  if git push origin HEAD:refs/heads/data/football-feeds 2>"$push_errors"; then
    cat "$push_errors" >&2
    if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
      printf '\n推送 %s/3 成功\n' "$attempt" >> "$GITHUB_STEP_SUMMARY"
    fi
    break
  else
    push_status=$?
  fi
  cat "$push_errors" >&2
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    {
      printf '\n推送 %s/3 失败\n\n~~~~text\n' "$attempt"
      cat "$push_errors"
      printf '\n~~~~\n'
    } >> "$GITHUB_STEP_SUMMARY"
  fi
  retry_kind=none
  if grep -Eiq 'fetch first|non-fast-forward' "$push_errors"; then
    retry_kind=rebase
  elif grep -Eiq 'returned error: 5[0-9]{2}|HTTP[^[:alnum:]]+5[0-9]{2}|Internal Server Error|Bad Gateway|Service Unavailable|Gateway Timeout|timed? out|timeout|Could not resolve host|Failed to connect|Connection (reset|refused|closed)|remote end hung up unexpectedly|Recv failure|Send failure|Empty reply from server|Network is unreachable|TLS connection was non-properly terminated' "$push_errors"; then
    retry_kind=network
  fi
  if [[ "$retry_kind" == none || "$attempt" == 3 ]]; then
    if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
      printf '\n推送失败，产物在 artifact\n' >> "$GITHUB_STEP_SUMMARY"
    fi
    exit "$push_status"
  fi
  if [[ "$retry_kind" == network ]]; then
    sleep 30
  fi
done
if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  printf '\n### Data branch\n\n`data/football-feeds` commit: `%s`\n' "$(git rev-parse HEAD)" >> "$GITHUB_STEP_SUMMARY"
fi
