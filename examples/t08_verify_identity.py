"""Offline audit of saved FD CSVs against an official SQLite archive.

Run from an installed checkout:
python examples/t08_verify_identity.py --db official.db --index current_index.json \
    --config candidates.json --year 2026 --output identity_verified

The index maps source codes to {path, sha256}. The candidate config is a list
(or code-keyed dictionary) of {div_code, league, candidate_zones}, for example:
[{"div_code":"SP1", "league":"西甲",
  "candidate_zones":["Europe/London", "Europe/Madrid"]}].
No network access, aliases/config changes, or database writes are performed.
"""

from zucai.sources.fd_verify import main

if __name__ == "__main__":
    raise SystemExit(main())
