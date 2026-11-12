"""Append hash-bound paper views without changing their source forecast bytes."""

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from zucai.cloud_feeds import archive_info, digest
from zucai.db import get_engine
from zucai.digest_context import audit_rows
from zucai.features.injuries import InjuryFeatures
from zucai.forecast_archive import MANIFEST, _path, verify_forecasts
from zucai.forecast_publication import publication_times
from zucai.models import Match, Team
from zucai.parlay.single_double import RULE, SUPPORTED_RULES, Config, build
from zucai.report.render import load_batch
from zucai.scoreboard.score import instant
from zucai.sources.base import _atomic_write


def raw_json(value):
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode()


def capture_injuries(database, batches):
    """Use T38 strict capture-time evidence, reusing the already restored experiment DB."""
    engine = get_engine(database)
    evidence = {}
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA query_only=ON")
            with Session(bind=connection) as session:
                builder = InjuryFeatures(session)
                for batch in batches:
                    for row in batch["predictions"]:
                        match = builder.matches.get(row["match_id"])
                        if match is None:
                            continue
                        feature = builder.build(match, as_of=batch["as_of_utc"], mode="strict")
                        evidence[row["match_id"]] = {
                            side: {
                                **feature[side]["missing_n"],
                                "confirmed_injured_out": feature[side]["missing_n"]["value"],
                                "feature_cutoff": feature["feature_cutoff"],
                                "availability_mode": "strict_capture_time",
                            }
                            for side in ("home", "away")
                        }
    finally:
        engine.dispose()
    return evidence


def _identities(database):
    engine = get_engine(database)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA query_only=ON")
            with Session(bind=connection) as session:
                teams = {r.id: r.name for r in session.scalars(select(Team))}
                return {
                    r.match_id: dict(
                        match_id=r.match_id,
                        source=r.source,
                        league=r.league,
                        home=teams[r.home_team_id],
                        away=teams[r.away_team_id],
                        home_team_id=r.home_team_id,
                        away_team_id=r.away_team_id,
                        kickoff_utc=r.kickoff_utc,
                        status=r.status,
                    )
                    for r in session.scalars(select(Match).where(Match.source == "jc"))
                }
    finally:
        engine.dispose()


def load_documents(archive, manifest, as_of, *, publications=None):
    publications = publication_times(archive, manifest) if publications is None else publications
    documents = []
    for run in (manifest or {}).get("runs", {}).values():
        for day, entry in run["forecasts"].items():
            name = entry.get("parlay_path")
            formal = publications.get(entry["path"] + "/forecast.json")
            attachment = publications.get(name)
            if not name or not formal or not attachment:
                continue
            published = max(instant(formal["published_utc"]), instant(attachment["published_utc"]))
            if published > instant(as_of):
                continue
            doc = json.loads(_path(archive, name).read_bytes())
            if doc.get("rule") not in SUPPORTED_RULES or doc.get("batch_id") != entry["batch_id"]:
                raise ValueError("复式冻结文件与批次不符")
            if instant(doc["generated_utc"]) > published:
                raise ValueError("复式生成时间晚于归档时间")
            documents.append(
                {
                    **doc,
                    "path": name,
                    "sha256": manifest["files"][name],
                    "published_utc": published.isoformat(),
                    "day": day,
                }
            )
    return sorted(documents, key=lambda d: (instant(d["generated_utc"]), d["batch_id"]))


def publish_document(archive, run_id, day, document, *, clock=None):
    """The first frozen decision locks this batch, even before its ledger is advanced."""
    clock = clock or (lambda: datetime.now(UTC))
    manifest = verify_forecasts(archive)
    before = (archive / MANIFEST).read_bytes()
    entry = manifest["runs"][run_id]["forecasts"][day]
    if document["batch_id"] != entry["batch_id"] or document["rule"] != RULE:
        raise ValueError("复式批次不匹配")
    name = entry["path"] + "/parlay_sd.json"
    previous = None
    if entry.get("parlay_path"):
        return manifest["runs"][run_id]
    doc = dict(document)
    doc["previous_version_sha256"] = digest(archive / name) if previous else None
    doc["replaced_groups"] = sorted(
        {g["group_id"] for g in (previous or {}).get("groups", [])}
        - {g["group_id"] for g in doc["groups"]}
    )
    raw = raw_json(doc)
    sha = hashlib.sha256(raw).hexdigest()
    version = entry["path"] + f"/parlay_sd-{sha}.json"
    publication = instant(clock())
    if publication < instant(doc["generated_utc"]) or any(
        instant(leg["kickoff_utc"]) <= publication
        for group in doc["groups"]
        for leg in (group["single"], group["double"])
    ):
        raise ValueError("复式冻结时已有比赛开球，拒绝发布")
    paths = {_path(archive, name): raw, _path(archive, version): raw}
    backups = {p: p.read_bytes() if p.exists() else None for p in paths}
    entry.update(
        parlay_path=name,
        parlay_published_utc=publication.isoformat(),
        parlay_versions=[*entry.get("parlay_versions", []), version],
    )
    manifest["files"].update({name: sha, version: sha})
    try:
        for path, content in paths.items():
            if path.exists() and path.name != "parlay_sd.json" and path.read_bytes() != content:
                raise ValueError("复式历史版本不可覆盖")
            _atomic_write(path, content)
        _atomic_write(archive / MANIFEST, raw_json(manifest))
        verify_forecasts(archive)
    except BaseException:
        (archive / MANIFEST).write_bytes(before)
        for path, content in backups.items():
            path.unlink(missing_ok=True) if content is None else path.write_bytes(content)
        raise
    return manifest["runs"][run_id]


def freeze(archive, database, run_id, *, injuries=None, clock=None, config=None):
    clock = clock or (lambda: datetime.now(UTC))
    manifest, feed = verify_forecasts(archive), archive_info(archive)
    identities = _identities(database)
    run = manifest["runs"][run_id]
    config = config or Config()
    for day, entry in run["forecasts"].items():
        if day in run.get("sfc_extra_dates", []) or entry.get("parlay_path"):
            continue  # A changed rule/configuration never rewrites an already frozen batch.
        path = entry["path"] + "/forecast.json"
        batch = load_batch(archive / path)
        experimental = (
            json.loads((archive / entry["experimental_path"]).read_bytes())
            if entry.get("experimental_path")
            else None
        )
        now = clock()
        catalog = audit_rows(archive, feed, "jc_catalog", instant(batch["as_of_utc"])) or []
        ids = {r["match_id"] for r in batch["predictions"]}
        context = dict(
            identities={k: v for k, v in identities.items() if k in ids},
            injuries={k: v for k, v in (injuries or {}).items() if k in ids},
            catalog=[r for r in catalog if r["match_id"] in ids],
        )
        doc = build(batch, **context, frozen_utc=now, experimental=experimental, config=config)
        doc["sources"] = {
            "forecast": dict(path=path, sha256=manifest["files"][path]),
            "experimental": dict(
                path=entry["experimental_path"],
                sha256=manifest["files"][entry["experimental_path"]],
            )
            if entry.get("experimental_path")
            else None,
        }
        doc["input_sha256"] = hashlib.sha256(
            raw_json(dict(rule=RULE, context=context, config=doc["config"], sources=doc["sources"]))
        ).hexdigest()
        run = publish_document(archive, run_id, day, doc, clock=clock)
    return run
