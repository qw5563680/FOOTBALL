# T68 daily-gate offline fixtures

`facts.json` is synthetic validator input with explicit project IDs, regular-time scores and capture metadata.
Its placeholder hashes are not retained-source evidence.

`fd.csv` is a synthetic single-row FD-shaped boundary sample (2026-10-31, 1–3, 12:30 Europe/London).
It exercises an already-stored relation with a 15-minute clock difference, exact existing aliases, and
separate auxiliary provenance. It is not a downloaded match result or proof of account access.

The real-entry tests construct small, complete archives and exports from these and existing offline JC/APIF
fixtures. They execute the actual archive reader and gate without sports network requests.
