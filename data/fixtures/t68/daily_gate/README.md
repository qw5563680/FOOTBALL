# T68 daily-gate offline fixtures

`facts.json` is synthetic validator input with explicit project IDs, regular-time scores and capture metadata.
Its placeholder hashes are not retained-source evidence.

`fd.csv` is a synthetic single-row FD-shaped boundary sample (2026-10-31, 1–3, 12:30 Europe/London).
It exercises an already-stored relation with a 15-minute clock difference, exact existing aliases, and
separate auxiliary provenance. It is not a downloaded match result or proof of account access.

The real-entry tests construct small, complete archives and exports from these and existing offline JC/APIF
fixtures. They execute the actual archive reader and gate without sports network requests.

Current/repeat reports in the entry fixture use actual offline `import_apif_games` output and exported
memory-database state. The default fixture records the documented legacy absorbed mode explicitly;
separate cases exercise retained results and a supplemental `assumed` count. Saved invocation/options
include the synthetic import clock, source/config hashes and before/after manifests. These prove importer
semantics and evidence validation, not that a production collector ran at those timestamps.
