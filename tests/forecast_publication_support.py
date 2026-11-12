"""Explicit synthetic receipts for isolated scoring fixtures, never production evidence."""

import json

from zucai.forecast_publication import candidates


def confirm_fixture(archive, published=None):
    path = archive / "forecast_manifest.json"
    manifest = json.loads(path.read_bytes())
    for entry, name, prefix in candidates(manifest):
        stamp = published or entry.get(prefix + "published_utc") or entry["generated_utc"]
        entry[prefix + "published_utc"] = stamp
        entry[prefix + "publication_evidence"] = dict(
            method="post_push_confirmation",
            candidate_path=name,
            candidate_sha256=manifest["files"][name],
            published_utc=stamp,
            commit="0" * 40,
            ref="refs/remotes/offline-fixture/data/football-feeds",
        )
    path.write_text(json.dumps(manifest))
