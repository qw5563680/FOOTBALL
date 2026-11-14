# T38 injuries fixtures

`injuries.json`, `injuries_context.json`, and `injuries_lineups.json` are synthetic, small offline examples. They test capture boundaries, identities, evolving lists, and complete starting elevens; they are not prediction-accuracy evidence.

`injuries_actual_excerpt.json` retains the first four unchanged response rows from an existing Premier League capture. Only the envelope results count is adjusted from 1080 to 4. All provider fields (including photos, logos, timestamps and original injury text) are preserved.

- Existing Actions run: 41877829928.
- Original URL: https://v3.football.api-sports.io/injuries?league=39&season=2026
- Original captured UTC: 2026-11-04T17:30:34.891092Z
- Original raw SHA256: `2e0c93224139faa6740b83ce8322e8408c5e7f984ed005b215cc26a761f60e4d`.
- Excerpt SHA256: `2016c0f0f2f662f889d90eeb6c58e95e9d6c12d697428c7a8fa26b04decb2f8c`.

`injuries_actual_provenance.json` independently records the original capture metadata and both hashes. It is provenance for the excerpt, not a fabricated RawStore sidecar for an original full page. This excerpt came from the root agent’s download of an already existing artifact; it does not represent a new API request, probe, permission check, or accuracy evaluation.
