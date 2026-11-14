# T63 revised version-policy acceptance

This directory appends evidence for the PR136 owner scope revision. The earlier
acceptance/2026-11-14-t63-sources directory and all production archives are unchanged.

- legacy-compatibility-final/final/acceptance.json: final 39 fits and 105 predictions equal
  the b83d2cc baseline, 14 targets without a trusted cutoff retained.
- legacy-compatibility/: preserved earlier comparison and baseline, not overwritten
  by the final run after the future-APIF-import compatibility correction.
- fixture-verification/full-suite.log and XML: final complete offline test run.
- fixture-verification/full-suite.red.*: retained first run and its three failures;
  IMPLEMENTATION-NOTES.md explains the scope-dependent old-test adaptations.
- fixture-verification/source_version_artifacts/: complete SYNTHETIC archives,
  including failed candidates. Not live HTTP data, benefit evidence or publication.
- implementation-receipt.json: actual implementation revision and verification.
- file-sha256.json: hashes relative to this directory, excluding this hash file itself.

For actual scope and limitations see the T63 report in the implementation branch.
No database, formal credentials, new sports fetch, production replacement or PR merge.
