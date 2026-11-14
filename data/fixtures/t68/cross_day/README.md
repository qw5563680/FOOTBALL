# T68 cross-day risk fixture

`real12.json` is a byte-preserving copy of the parent-prepared 26,972-byte clip from the immutable result-only fixture database. It contains 24 matches, 24 results, 11 teams and 12 original identity proof rows; its `pairs` field lists the fixed twelve reviewed APIF/FD risk pairs.

- Frozen data reference: `data/football-feeds@e370db1d5cabe1cb33ddbfff3029c6d802bb8992` (partial saved archive, not a complete production backup).
- Result-only database SHA256: `719f06b8cf800ccccc70f4eb158ecb092aa15d8da3ee8e29f98a4fca43abfb3a`.
- Clip SHA256: `36352b09a31f0eb6b5e32fd4eba71dfec297872fdb2edfaf7a31b0abc9585a40`.
- Inputs retain source IDs, canonical IDs, names, scores, kickoff/availability/observation clocks, pointers and identity proof timestamps.

This is a risk inventory, not authorization that any pair is the same match. Tests retain FD and quarantine APIF until an explicit decision has both approval metadata and a real application receipt. Other cases mutate this offline fixture as labeled synthetic counterexamples; synthetic approval PR numbers and reviewer evidence are test data only. No sports request or key is used.

The former review's eleven one-sided-unbound risks have no original ID list in the available materials. This fixture does not pretend to reproduce those eleven; a synthetic independently validated binding transition verifies that new same-ID pairs cannot leave quarantine merely because identity becomes known.
