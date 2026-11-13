#!/usr/bin/env bash
# Run from the data-branch checkout. Conflicts leave the remote untouched.
set -euo pipefail

git config user.name 'github-actions[bot]'
git config user.email '41898282+github-actions[bot]@users.noreply.github.com'
# Frozen hashes cover exact bytes, including T06 CSV CRLF on Windows checkouts.
# A pinned candidate review must not migrate any unrelated archive attributes.
if [[ -z "${PUBLISH_EXPECTED_BASE:-}" && -e forecast_manifest.json ]]; then
  for rule in 'models/** -text' 'forecasts/** -text whitespace=cr-at-eol' 'forecast_manifest.json -text'; do
    if ! sed 's/\r$//' .gitattributes 2>/dev/null | grep -Fxq -- "$rule"; then
      printf '\n%s\n' "$rule" >> .gitattributes
    fi
  done
fi
if [[ -z "${PUBLISH_EXPECTED_BASE:-}" && -d scoreboard ]]; then
  if ! sed 's/\r$//' .gitattributes 2>/dev/null | grep -Fxq -- 'scoreboard/** -text'; then
    printf '\n%s\n' 'scoreboard/** -text' >> .gitattributes
  fi
fi
if [[ -z "${PUBLISH_EXPECTED_BASE:-}" && -d daily ]]; then
  if ! sed 's/\r$//' .gitattributes 2>/dev/null | grep -Fxq -- 'daily/** -text'; then
    printf '\n%s\n' 'daily/** -text' >> .gitattributes
  fi
fi
if [[ -z "${PUBLISH_EXPECTED_BASE:-}" && -d sfc ]]; then
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
# Save the committed collection before any network operation or rebase can fail.
# Incremental bundle contains Git objects only, never checkout credentials/config.
if [[ -n "${PUBLISH_RECOVERY_DIR:-}" ]]; then
  mkdir -p "$PUBLISH_RECOVERY_DIR"
  base=$(git rev-parse refs/remotes/origin/data/football-feeds)
  head=$(git rev-parse HEAD)
  if [[ "$head" != "$base" ]]; then
    git bundle create "$PUBLISH_RECOVERY_DIR/unpublished.bundle" HEAD "^$base"
    {
      printf 'run=%s/%s\nbase=%s\nhead=%s\n' "$GITHUB_RUN_ID" "$GITHUB_RUN_ATTEMPT" "$base" "$head"
      printf 'feed_manifest_sha256='
      sha256sum feed_manifest.json
    } > "$PUBLISH_RECOVERY_DIR/recovery.txt"
    (cd "$PUBLISH_RECOVERY_DIR" && sha256sum unpublished.bundle > SHA256SUMS)
  fi
fi
push_errors=$(mktemp)
trap 'rm -f "$push_errors"' EXIT
network_error() {
  grep -Eiq 'returned error: 5[0-9]{2}|HTTP[^[:alnum:]]+5[0-9]{2}|Internal Server Error|Bad Gateway|Service Unavailable|Gateway Timeout|timed? out|timeout|Could not resolve host|Failed to connect|Connection (reset|refused|closed)|remote end hung up unexpectedly|Recv failure|Send failure|Empty reply from server|Network is unreachable|TLS connection was non-properly terminated' "$push_errors"
}
failed_attempt() {
  cat "$push_errors" >&2
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    {
      printf '\n%s %s/3 失败\n\n~~~~text\n' "$1" "$attempt"
      cat "$push_errors"
      printf '\n~~~~\n'
    } >> "$GITHUB_STEP_SUMMARY"
  fi
}
failed_publication() {
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    printf '\n发布未确认成功；恢复资料由后续 artifact 步骤上传，请核对上传结果。\n' >> "$GITHUB_STEP_SUMMARY"
    if [[ -n "${PUBLISH_RECOVERY_DIR:-}" ]]; then
      printf '本次已提交数据的恢复包：%s；未确认发布，后续只能读取远端已有版本。\n' "$PUBLISH_RECOVERY_DIR" >> "$GITHUB_STEP_SUMMARY"
    fi
  fi
  exit "$1"
}
for attempt in 1 2 3; do
  # Separate fetch from rebase: only the network operation may retry.
  if git fetch origin data/football-feeds 2>"$push_errors"; then
    cat "$push_errors" >&2
  else
    fetch_status=$?
    failed_attempt 拉取
    if ! network_error || [[ "$attempt" == 3 ]]; then
      failed_publication "$fetch_status"
    fi
    sleep 30
    continue
  fi
  # A reviewed queue is approved only for its restored snapshot. Never rebase
  # that decision over data the reviewer has not inspected, even without conflicts.
  if [[ -n "${PUBLISH_EXPECTED_BASE:-}" ]]; then
    if [[ "$(git rev-parse FETCH_HEAD)" != "$PUBLISH_EXPECTED_BASE" ]]; then
      printf '核验基线已变化，须重新 inspect 和核验计划；本次不发布。\n' > "$push_errors"
      failed_attempt 核验基线
      failed_publication 1
    fi
  elif git rebase FETCH_HEAD >"$push_errors" 2>&1; then
    cat "$push_errors"
  else
    rebase_status=$?
    failed_attempt 变基
    failed_publication "$rebase_status"
  fi
  if git push origin HEAD:refs/heads/data/football-feeds 2>"$push_errors"; then
    cat "$push_errors" >&2
    if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
      printf '\n推送 %s/3 成功\n' "$attempt" >> "$GITHUB_STEP_SUMMARY"
    fi
    break
  else
    push_status=$?
  fi
  failed_attempt 推送
  retry_kind=none
  if grep -Eiq 'fetch first|non-fast-forward' "$push_errors"; then
    retry_kind=rebase
  elif network_error; then
    retry_kind=network
  fi
  if [[ "$retry_kind" == none || "$attempt" == 3 ]]; then
    failed_publication "$push_status"
  fi
  if [[ "$retry_kind" == network ]]; then
    sleep 30
  fi
done
if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  printf '\n### Data branch\n\n`data/football-feeds` commit: `%s`\n' "$(git rev-parse HEAD)" >> "$GITHUB_STEP_SUMMARY"
fi
