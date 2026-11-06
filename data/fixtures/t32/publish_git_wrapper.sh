#!/usr/bin/env bash
# Inject a local competing writer after pull and before push advertises remote refs.
set -eu
printf '%s\n' "$*" >> "$TEST_GIT_LOG"
if [[ "$1" == fetch || "$1" == pull ]]; then
  count_file="$TEST_GIT_LOG.fetch-count"
  count=0
  [[ ! -e "$count_file" ]] || count=$(cat "$count_file")
  count=$((count + 1))
  printf '%s\n' "$count" > "$count_file"
  if (( count <= ${TEST_FETCH_FAILURES:-0} )); then
    printf '%s\n' "$TEST_FETCH_MESSAGE" >&2
    exit 128
  fi
fi
if [[ "$1" == push && -n "${TEST_OTHER_WRITER:-}" && ! -e "$TEST_RACE_MARKER" ]]; then
  touch "$TEST_RACE_MARKER"
  "$TEST_REAL_GIT" -C "$TEST_OTHER_WRITER" push origin HEAD
fi
if [[ "$1" == push && -n "${TEST_PUSH_ACK_LOST:-}" && ! -e "$TEST_RACE_MARKER" ]]; then
  touch "$TEST_RACE_MARKER"
  "$TEST_REAL_GIT" "$@"
  printf 'fatal: Recv failure: Connection was reset\n' >&2
  exit 128
fi
exec "$TEST_REAL_GIT" "$@"
