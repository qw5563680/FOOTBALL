#!/usr/bin/env bash
# Record the configured delay without making offline tests wait.
printf '%s\n' "$*" >> "$TEST_SLEEP_LOG"
