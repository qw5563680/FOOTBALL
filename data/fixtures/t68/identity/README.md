# T68 identity fixtures

- `strict-pair.json`, `apif-page.json`, and `fd-date-window.csv` are **synthetic** offline examples. They contain explicit identity, clock, regular-time score, and completeness assumptions for positive and negative validator tests. They are not collection or approval evidence.
- `legacy-queue.json` is one unchanged league/team entry clipped from the frozen `e370db1d5cabe1cb33ddbfff3029c6d802bb8992` candidate queue. `legacy-queue.provenance.json` records the original path/hash, exact JSON pointer, and clipped hash. Its legacy `auto_verified` state is preserved as queue data; it does not authorize a new T68 binding.
- Tests retain unplayed, rejected, unknown-identity, and date-only competitors. They cover full-window uniqueness before score checks, exact second precision, FT/AET/PEN regulation scores, type separation, layered withdrawal, prior-queue conflicts, formal time scope, head contradictions, manifest verification, and repeat replay.
- Real fixed-input replay and itemized 93/107/88 comparisons are generated under `work/t68-a/`; no full sports archive or production database is included in these fixtures.
