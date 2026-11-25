#!/usr/bin/env bash
# Run only after the production data and its publication evidence are pushed.
set -euo pipefail

archive=$(cd "$1" && pwd -P)
compute_budget=$3
publisher="$(cd "$(dirname "$0")" && pwd -P)/publish-football-feeds.sh"
run_id="$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT"
official=$(git -C "$archive" rev-parse HEAD)
ref=refs/remotes/origin/data/football-feeds
refspec="refs/heads/data/football-feeds:$ref"
scratch=
attachment_pushed=false
result="实验未生成（experiment_failed）"
started=$(date -u +%s)
deadline=$((SECONDS + 36 * 60))
if [[ -n "${PREDICT_OPTIONAL_DEADLINE:-}" ]]; then
  job_deadline=$((SECONDS + PREDICT_OPTIONAL_DEADLINE - started))
  if (( job_deadline < deadline )); then deadline=$job_deadline; fi
fi

within_budget() {
  local limit=$1 remaining=$((deadline - SECONDS))
  shift
  (( remaining > 0 )) || return 124
  if (( remaining > limit )); then remaining=$limit; fi
  timeout --signal=TERM --kill-after=5s "${remaining}s" "$@"
}

remove_worktree() {
  if [[ -n "$scratch" ]]; then
    timeout --kill-after=5s 30s git -C "$archive" worktree remove --force "$scratch" ||
      rmdir "$scratch" || return
    scratch=
  fi
}

finish() {
  remove_worktree || printf '实验工作树清理失败。\n' >&2
  printf '\n实验阶段用时：%s 秒。%s\n' "$(($(date -u +%s) - started))" "$result" \
    >> "$GITHUB_STEP_SUMMARY"
}
trap finish EXIT
printf '\n实验阶段开始：%s（北京时间 %s）。计算预算上限 %s 秒。\n' \
  "$(date -u -d "@$started" +%FT%TZ)" \
  "$(TZ=Asia/Shanghai date -d "@$started" '+%F %T')" "$compute_budget" >> "$GITHUB_STEP_SUMMARY"

if ! state=$(cd "$2" && pwd -P); then
  result+="；正式冻结输入不可读。"
  exit 0
fi

refresh_data_ref() {
  within_budget 60 git -C "$1" fetch origin "$refspec"
}

if ! refresh_data_ref "$archive" || [[ $(git -C "$archive" rev-parse "$ref") != "$official" ]]; then
  result="实验未启动；远端正式基线未确认。"
  exit 0
fi

new_worktree() {
  scratch=$(mktemp -d "$(dirname "$state")/t88-experiments.XXXXXX") || return
  within_budget 60 git -C "$archive" worktree add --detach "$scratch" "$official"
}

publish_with_receipts() {
  local published
  (cd "$scratch" && PUBLISH_EXPECTED_BASE="$official" within_budget 120 bash "$publisher") || return
  attachment_pushed=true
  published=$(git -C "$scratch" rev-parse HEAD) || return
  refresh_data_ref "$scratch" || return
  [[ $(git -C "$scratch" rev-parse "$ref") == "$published" ]] || return
  within_budget 60 python -m zucai.forecast_publication --archive "$scratch" --published-ref "$ref" ||
    return
  (cd "$scratch" && PUBLISH_EXPECTED_BASE="$published" within_budget 120 bash "$publisher")
}

run_experiments() {
  local remaining=$((deadline - SECONDS - 30)) budget=$compute_budget
  # Leave room for the TERM-to-KILL grace period.
  if (( remaining < budget )); then budget=$remaining; fi
  (( budget > 0 )) || return 124
  # Reserve the duration of a Release request (120s) and a little cleanup time.
  ZUCAI_GITHUB_DEADLINE="$(($(date -u +%s) + budget - 125))" \
    timeout --signal=TERM --kill-after=30s "${budget}s" \
      python -m zucai.cloud_predict experiments --archive "$scratch" --run-id "$run_id" \
      --production-state "$state"
}

if new_worktree && run_experiments; then
  if publish_with_receipts; then
    result="实验阶段归档及发布回执已确认。"
    exit 0
  fi
  if [[ "$attachment_pushed" == true ]]; then
    result="实验归档已推送，发布回执未确认（experiment_publication_failed）。"
    exit 0
  fi
  reason=experiment_publication_failed
else
  case $? in
    124|137) reason=experiment_timeout ;;
    130|143) reason=experiment_cancelled ;;
    *) reason=experiment_failed ;;
  esac
fi

result="实验未生成（$reason）"
if ! remove_worktree || ! refresh_data_ref "$archive" ||
   [[ $(git -C "$archive" rev-parse "$ref") != "$official" ]]; then
  # An ambiguous push or concurrent update may already have published evidence.
  result+="；远端未确认仍在正式基线，保留远端现有归档。"
  exit 0
fi

if new_worktree && within_budget 180 \
  python -m zucai.cloud_predict experiment-unavailable --archive "$scratch" --run-id "$run_id" \
  --production-state "$state" --reason "$reason" && publish_with_receipts; then
  result+="；缺席原因及发布回执已确认。"
else
  result+="；缺席原因未确认发布。"
fi
