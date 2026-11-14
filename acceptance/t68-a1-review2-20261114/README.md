# T68 A1 review2 offline evidence

PR: https://github.com/qw5563680/FOOTBALL/pull/140

Source/data versions, limitations and exact checks are inside the ZIP. No production publication, real sports requests or new identity approvals. Internal review is not Claude approval.

{
  "task": "T68 A1 review2",
  "previous_source_commit": "4427f23368324d852a4287d572463006d01cd85b",
  "formal_review": "https://github.com/qw5563680/FOOTBALL/pull/140#pullrequestreview-5904484794",
  "status": "Offline implementation tested; reviewer live pilot pending; B not implemented",
  "observed_utc": "2026-11-14T13:30:03.693777+00:00",
  "observed_beijing": "2026-11-14T21:30:03.693777+08:00",
  "source_base_commit": "273008329b15fabe8b5dd0cdc1ed78f139dcc270",
  "data_commit": "e370db1d5cabe1cb33ddbfff3029c6d802bb8992",
  "source_manifest": "a1-review2-tested-files.json",
  "full_suite": {
    "tests": 4110,
    "failures": 0,
    "errors": 0,
    "skipped": 0
  },
  "full_suite_seconds": 253.236,
  "selected": 1262,
  "reusable": 146,
  "pending": 1116,
  "http_calls": 0,
  "database_unchanged_sha256": "719f06b8cf800ccccc70f4eb158ecb092aa15d8da3ee8e29f98a4fca43abfb3a",
  "protected_files": {
    "data/team_aliases/api_football.json": "5c47ff54f44e5b5dd55a414c8118c32899f9a7ecc72ec2f69154d5e7785bb7bc",
    "data/api_football/nations.json": "9cbe8e7bd11253411580985b80caf277c47e498eba2256579d20d4bca00079d0",
    "data/league_map.json": "f7b9e6f9bdb604781c4ad7f6e771c82967fe4f4ca31b5141009663e15a774481",
    "data/model/params.json": "fbe2e82f171ac52605c0d309544f89505a36e524d517334e32348c29438cb8f8"
  },
  "fixed_inputs_unchanged": 2305,
  "limitation": "Partial source materialization for plan; results-only regression fixture is not a full production restoration",
  "previous_evidence": "7acbbc4e8bd287fb3da6b5503d5209a7e99e97ca/acceptance/t68-a1-20261114"
}
