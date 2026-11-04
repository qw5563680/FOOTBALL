#!/usr/bin/env bash
# Inject a local competing writer after pull and before push advertises remote refs.
set -eu
printf '%s\n' "$*" >> "$TEST_GIT_LOG"
if [[ "$1" == push && -n "${TEST_OTHER_WRITER:-}" && ! -e "$TEST_RACE_MARKER" ]]; then
  touch "$TEST_RACE_MARKER"
  "$TEST_REAL_GIT" -C "$TEST_OTHER_WRITER" push origin HEAD
fi
exec "$TEST_REAL_GIT" "$@"
