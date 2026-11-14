# T68 A1 CI-environment evidence

No production code changes; historical test failure retained.

{
  "task": "T68 A1 CI environment regression",
  "previous_source": "342cd556b5711d096bdc72aee48056ff88171ebb",
  "formal_review": "https://github.com/qw5563680/FOOTBALL/pull/140#pullrequestreview-5904560410",
  "full_suite": {
    "tests": 4110,
    "failures": 0,
    "errors": 0,
    "skipped": 0
  },
  "seconds": 254.602,
  "observed_utc": "2026-11-14T13:48:09.026379+00:00",
  "environment": "Full suite explicitly sets GITHUB_STEP_SUMMARY; regression test itself sets a tmp path and complete collector result",
  "production_code_changes": 0,
  "live_sports_requests": 0,
  "prior_functional_evidence": "0869f663d263932be73cdcc08deedf4325af57a0"
}
