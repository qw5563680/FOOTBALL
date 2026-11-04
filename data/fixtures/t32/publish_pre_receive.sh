#!/usr/bin/env bash
# Synthetic local-only remote failure; invoked by test_publish_script.py.
set -eu
attempt=0
if [[ -f test-push-attempts ]]; then
  attempt=$(cat test-push-attempts)
fi
attempt=$((attempt + 1))
printf '%s\n' "$attempt" > test-push-attempts
if (( attempt <= TEST_PUSH_FAILURES )); then
  printf '%s\n' "$TEST_PUSH_MESSAGE" >&2
  exit 1
fi
