"""Offline, explicitly approved creation of JC numeric canonical identities."""

import argparse
import csv
import io
import json
import re
from collections import defaultdict
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from sqlalchemy import delete, select, text

from zucai.db import get_engine, session_scope
from zucai.identity_binding import _category_veto, _utc
from zucai.identity_provider_only import candidates as provider_only_candidates
from zucai.identity_same_club import BASIS as SAME_CLUB_BASIS
from zucai.identity_same_club import link_decision, official_witness, witness_summary
from zucai.models import Base, Match, Team
from zucai.sources.apif_aliases import validate_approvals
from zucai.sources.base import _atomic_write
from zucai.sources.teams import normalize_team

RECEIPTS = "state/audit/jc_canonical_identities.csv"
FIELDS = ("official_team_id", "project_team_id", "canonical", "created_utc", "decision_json")
CREATE_ACTION = "create_canonical_identity"
LINK_ACTION = "link_existing_identity"


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _evidence(team):
    return sorted(
        [
            dict(
                label=r["label"],
                match_id=r["match_id"],
                side=r["side"],
                originals=[
                    {k: p[k] for k in ("path", "sha256", "pointer") if k in p}
                    for p in r["provenance"]
                    if p.get("path") and p.get("sha256")
                ],
            )
            for r in team["official_name_evidence"]
        ],
        key=_json,
    )


def _entry(team):
    evidence = _evidence(team)
    category = team.get("category")
    blockers = []
    if team["identity_status"] != "creation_proposed":
        blockers.append("existing_or_conflicting_official_identity")
    if team.get("category_status") != "approved" or not category:
        blockers.append("category_not_approved_or_ambiguous")
    if any(_category_veto(name, category) for name in team["official_names"]):
        blockers.append("individual_team_category_requires_review")
    if not evidence or any(not row["originals"] for row in evidence):
        blockers.append("numeric_original_evidence_missing")
    return dict(
        status="proposed",
        action="create_canonical_identity",
        official_team_id=team["official_team_id"],
        canonical=normalize_team(team["name"]),
        official_names=team["official_names"],
        category=category,
        evidence=evidence,
        evidence_sha256=sha256(_json(evidence).encode()).hexdigest(),
        blockers=blockers,
    )


INTERNATIONAL_COUNTRIES = {"World", "International", "Europe"}


def _import_configs(registry):
    # Country/league mapping covers every registered import route, whatever
    # its status: an undetermined country is treated as same-country below.
    # Index-only routes (欧冠) are registered competitions with a known country
    # too; leaving them out made their stored teams count as "country unknown"
    # and block every domestic creation, unlike 欧罗巴 / 欧协联.
    return {
        code: entry["config"]
        for code, entry in registry["providers"]["apif"].items()
        if entry.get("mode") in {"import", "index_only"}
    }


def _country_by_code(registry):
    countries = {}
    for row in registry.get("competitions", []):
        proposal = row.get("api_catalogue_proposal") or {}
        for entry in proposal.get("entries", []):
            code = str(entry.get("code"))
            if entry.get("country"):
                countries[code] = entry["country"]
    return countries


def _binding_index(facts):
    """jc official number -> the witness-complete binding proposals that carry it."""
    from zucai.identity_binding import propose_bindings

    proposals = propose_bindings(
        facts["apif"],
        facts["official"],
        facts["teams"],
        formal=facts["formal"],
        observed_utc=facts["summary"].get("observed_utc"),
        occupied=facts["occupied"],
        formal_sha256=facts["summary"].get("formal_aliases_sha256"),
    )
    index = defaultdict(list)
    for entries in proposals["apif"].values():
        for entry in entries.values():
            target = entry.get("canonical_team_id") or ""
            if target.startswith("jc-team:"):
                index[target[len("jc-team:") :]].append(entry)
    return index


def _unique_provider(entries):
    # strict_verified and conditional_verified both carry complete same-match
    # witnesses; conditional only lacks the stored-counterpart strengthening.
    providers = {
        str(entry.get("provider_team_id"))
        for entry in entries
        if entry.get("evidence_status") in {"strict_verified", "conditional_verified"}
        and entry.get("provider_team_id")
    }
    return next(iter(providers)) if len(providers) == 1 else None


def _formal_target(facts, provider_id):
    """Approved alias-table entry for the provider team id, when it names a stored team."""
    names = {
        normalize_team(other["name"])
        for other_key, other in facts["teams"].items()
        if other_key.startswith("project-team:") and other.get("name")
    }
    for entries in facts["formal"].values():
        for pid, entry in entries.items():
            if str(pid) != str(provider_id):
                continue
            if entry.get("status") not in {"approved", "auto_verified"}:
                continue
            canonical = normalize_team(entry.get("canonical"))
            if canonical not in names:
                continue
            for other_key, other in facts["teams"].items():
                if (
                    other_key.startswith("project-team:")
                    and other.get("name")
                    and normalize_team(other["name"]) == canonical
                ):
                    return other["project_team_id"], entry.get("canonical")
    return None


def _unique_formal_target(facts, provider_id):
    """Like _formal_target, but refuses a provider id whose approved entries name more than
    one stored team, or a canonical name that more than one stored team carries."""
    canonicals = {
        normalize_team(entry.get("canonical") or "")
        for entries in facts["formal"].values()
        for pid, entry in entries.items()
        if str(pid) == str(provider_id) and entry.get("status") in {"approved", "auto_verified"}
    }
    canonicals.discard("")
    owners = {
        other["project_team_id"]
        for other_key, other in facts["teams"].items()
        if other_key.startswith("project-team:")
        and other.get("name")
        and normalize_team(other["name"]) in canonicals
    }
    if len(owners) != 1:
        return None
    return _formal_target(facts, provider_id)


def _same_name_target(facts, team):
    for other_key, other in facts["teams"].items():
        if not other_key.startswith("project-team:") or other.get("identity_key") != other_key:
            continue
        if (
            team.get("name")
            and other.get("name")
            and normalize_team(other["name"]) == normalize_team(team["name"])
        ):
            return other["project_team_id"], other["name"]
    return None


def _competition_of(facts, team):
    for observation in team.get("official_name_evidence", []):
        for row in facts["official"]:
            if row.get("match_id") == observation.get("match_id"):
                return str(row.get("competition_id"))
    return None


def _registered_fdorg_placeholders():
    from zucai.sources.fdorg_verify import DEFAULT_ALIASES
    from zucai.sources.football_data import read_json

    return {
        (league, f"fdorg:team:{provider_id}")
        for league, entries in read_json(DEFAULT_ALIASES).items()
        if isinstance(entries, dict)
        for provider_id, entry in entries.items()
        if isinstance(entry, dict)
        and entry.get("status") in {"approved", "auto_verified"}
        and entry.get("canonical")
    }


def _fd_only_leagues_in_country(tables, facts, configs, countries, candidate_code):
    """Leagues whose stored, unbound, fd/fdorg-only teams share the candidate's country."""
    blockers = _fd_only_teams_in_country(
        tables, facts, configs, countries, countries.get(str(candidate_code))
    )
    return sorted({row["league"] for row in blockers})


def _fd_only_teams_in_country(
    tables, facts, configs, countries, candidate_country, *, keep_international_placeholders=False
):
    international = not candidate_country or candidate_country in INTERNATIONAL_COUNTRIES
    league_codes = {config.get("league"): code for code, config in configs.items()}
    teams_sources = defaultdict(set)
    teams_leagues = defaultdict(set)
    for row in tables["matches"]:
        for side in ("home", "away"):
            tid = row.get(f"{side}_team_id")
            if tid is None:
                continue
            teams_sources[str(tid)].add(row.get("source"))
            if row.get("league"):
                teams_leagues[str(tid)].add(row["league"])
    # T68 第八节第 13 项：别名表里已有获批接口编号的 fd/fdorg 球队不再算「未核验」。
    # 候选的见证编号若与它相同，前面的 _formal_target 已判「关联」；不同则是两支球队。
    registered = {
        normalize_team(entry["canonical"])
        for entries in facts.get("formal", {}).values()
        for entry in entries.values()
        if entry.get("status") in {"approved", "auto_verified"} and entry.get("canonical")
    }
    unbound = {
        other["project_team_id"]
        for other_key, other in facts["teams"].items()
        if other_key.startswith("project-team:")
        and other.get("identity_key") == other_key
        and normalize_team(other.get("name") or "") not in registered
    }
    # An fdorg placeholder row ("fdorg:team:<id>") whose id has an approved or verified entry
    # in the fdorg alias table for that league is a known club that only awaits its merge.
    placeholders = _registered_fdorg_placeholders()
    names = {
        other["project_team_id"]: other.get("name") or ""
        for other_key, other in facts["teams"].items()
        if other_key.startswith("project-team:")
    }
    blockers = []
    for tid in sorted(unbound):
        sources = teams_sources.get(tid, set())
        if not sources or not sources <= {"fd", "fdorg"}:
            continue
        name = names[tid]
        for league in sorted(teams_leagues.get(tid, set())):
            if (league, name) in placeholders:
                continue
            # An undetermined league country counts as same-country: when in
            # doubt the verdict must stay 待核, never fall through to 新建.
            country = countries.get(str(league_codes.get(league) or ""))
            is_international_placeholder = (
                keep_international_placeholders
                and country in INTERNATIONAL_COUNTRIES
                and re.fullmatch(r"fdorg:team:[1-9][0-9]*", name)
            )
            if (
                international
                or country is None
                or country == candidate_country
                or is_international_placeholder
            ):
                blockers.append(
                    dict(project_team_id=tid, name=name, league=league, country=country)
                )
    return blockers


def _country_creation_verdict(tables, facts, configs, countries, provider_id, country_proofs):
    """Evaluate already authenticated, normalized country proof for one witnessed provider ID."""
    evidence = sorted(country_proofs.get(provider_id, ()), key=_json)
    observed = set()
    for proof in evidence:
        value = proof.get("country")
        country = value.strip() if isinstance(value, str) else ""
        path, digest = proof.get("path"), proof.get("sha256")
        if (
            not country
            or country in INTERNATIONAL_COUNTRIES
            or not isinstance(path, str)
            or not path
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            observed.add(None)
        else:
            observed.add(country)
    country = next(iter(observed)) if len(observed) == 1 else None
    blockers = _fd_only_teams_in_country(
        tables, facts, configs, countries, country, keep_international_placeholders=True
    )
    details = dict(
        provider_team_id=provider_id,
        provider_team_country=dict(country=country, evidence=evidence),
        blocking_teams=blockers,
    )
    missing = []
    if country is None:
        missing.append("接口球队资料中的国家缺失、为空或不一致，仍按所有国家核对。")
    missing.extend(
        f"{row['league']} 的 {row['name']}（库内球队 {row['project_team_id']}）尚无获批接口编号。"
        for row in blockers
    )
    if missing:
        return dict(
            status="pending_review",
            basis=(
                "provider_team_country_unverified"
                if country is None
                else "unverified_same_country_fd_teams"
            ),
            missing=missing,
            **details,
        )
    return dict(
        status="new",
        basis="unique_witnessed_provider_team_without_existing_indication",
        说明=f"接口球队编号 {provider_id} 的国家为 {country}，无未核验的库内球队阻塞创建。",
        **details,
    )


def _existing_team_verdict(
    facts,
    tables,
    team,
    key,
    *,
    configs,
    countries,
    index,
    code=None,
    witness=None,
    country_proofs=None,
):
    """三结论之一：「新建」「关联库内既有球队」「待核」（T68 第八节第 7 项，2026-11-16 更正版）。

    关联既有球队只能来自：绑定提案给出唯一、见证齐全的接口球队编号 X，且
    X 在别名表已有获批条目指向库内球队；或库内已有同名中文球队（竞彩同步时
    建立、尚无官方编号证据）。名称相似度、译名、人工对照表一律不参与。
    没有检查到证据时不得默认「新建」。
    """
    official = team["official_team_id"]
    if code is None:
        code = _competition_of(facts, team)
    entries = index.get(str(official), [])
    provider_id = _unique_provider(entries)
    has_provider_originals = any(
        str(row.get("competition_id")) == str(code) for row in facts.get("apif", [])
    )
    if provider_id is None:
        # T68 第八节第 13 项第二阶段：绑定提案因「同一接口编号已属于库内另一支球队」
        # 给不出编号时，改看该官方球队全部官方比赛的见证。只有当见证编号在别名表里
        # 已有获批条目指向库内球队时才下结论「关联」；否则仍是待核，不开新的「新建」通道。
        observed = (witness or {}).get(str(official)) or {}
        if observed.get("provider_team_id"):
            target = _unique_formal_target(facts, observed["provider_team_id"])
            if target is not None:
                project_team_id, canonical = target
                return dict(
                    status="existing",
                    basis=SAME_CLUB_BASIS,
                    project_team_id=project_team_id,
                    provider_team_id=observed["provider_team_id"],
                    canonical=canonical,
                    witness=witness_summary(observed),
                    说明=(
                        f"该官方球队的 {observed['matches']} 场官方比赛全部见证接口球队编号 "
                        f"{observed['provider_team_id']}，该编号在别名表已有获批条目指向库内球队 "
                        f"{canonical}：同一家俱乐部，不是新建。"
                    ),
                )
        return dict(
            status="pending_review",
            missing=(
                ["归档里没有该赛事的接口原件，无法复核接口球队编号 X"]
                if not has_provider_originals
                else [
                    "绑定提案未给出唯一且见证齐全的接口球队编号 X"
                    "（无法与库内既有球队做比赛证据比对）"
                ]
            ),
            basis="no_unique_witnessed_provider_team",
        )
    formal_target = _formal_target(facts, provider_id)
    if formal_target is not None:
        project_team_id, canonical = formal_target
        return dict(
            status="existing",
            basis="approved_alias_entry",
            project_team_id=project_team_id,
            provider_team_id=provider_id,
            canonical=canonical,
            说明=(
                f"接口球队编号 {provider_id} 在别名表已有获批条目指向库内球队 "
                f"{canonical}（同一接口球队编号），不是新建。"
            ),
        )
    same_name = _same_name_target(facts, team)
    if same_name is not None:
        project_team_id, name = same_name
        return dict(
            status="existing",
            basis="same_chinese_name_without_official_evidence",
            project_team_id=project_team_id,
            说明=(
                f"库内已有同名中文球队 {name}（竞彩同步时建立、尚无官方编号证据），"
                "关联并补编号证据，不是新建。"
            ),
        )
    # None retains the established rule until the archive reader can verify the actual source.
    # An explicit empty index means the source was checked and has no usable country proof.
    if country_proofs is not None and countries.get(str(code)) in INTERNATIONAL_COUNTRIES:
        return _country_creation_verdict(
            tables, facts, configs, countries, provider_id, country_proofs
        )
    leagues = _fd_only_leagues_in_country(tables, facts, configs, countries, code)
    if leagues:
        return dict(
            status="pending_review",
            missing=[
                f"库内 {league} 存在只由 fd/fdorg 建立、无获批接口编号的球队，"
                f"与该赛事同属一个国家；需要 {league} 历史赛季的接口原件"
                "（只用于身份核对，不进训练）才能判断是否同一球队"
                for league in leagues
            ],
            basis="unverified_same_country_fd_teams",
        )
    return dict(
        status="new",
        basis="unique_witnessed_provider_team_without_existing_indication",
        说明=(
            f"接口球队编号 {provider_id} 见证齐全且唯一，别名表无获批条目、"
            "库内无同名中文球队、无同国 fd/fdorg 无编号球队，可新建。"
        ),
    )


def _verdict_context(facts, tables, registry):
    configs = _import_configs(registry)
    countries = _country_by_code(registry)
    index = _binding_index(facts)
    return configs, countries, index, official_witness(facts)


def propose(archive, *, codes=None):
    from zucai.identity_binding_archive import _tables, load_archive
    from zucai.league_registry import load_registry

    registry = load_registry()
    allowed = {c for c, e in registry["providers"]["apif"].items() if e["status"] == "verified"}
    codes = sorted(allowed if codes is None else codes)
    if not codes or not set(codes) <= allowed:
        raise ValueError("Creation proposals require verified catalogue routes")
    facts = load_archive(archive, leagues=codes)
    tables, _ = _tables(archive)
    configs, countries, index, witness = _verdict_context(facts, tables, registry)
    rows = [
        (key, team)
        for key, team in sorted(facts["teams"].items())
        if key.startswith("jc-team:") and team["identity_status"] == "creation_proposed"
    ]
    verdicts = {
        team["official_team_id"]: _existing_team_verdict(
            facts,
            tables,
            team,
            key,
            configs=configs,
            countries=countries,
            index=index,
            witness=witness,
        )
        for key, team in rows
    }
    # 「同一家俱乐部」提案：官方球队（库里可能已有一行只带竞彩比赛的中文名记录）的
    # 见证编号指向库内另一支既有球队。只出提案，逐条待所有者批准。
    links = []
    for key, team in sorted(facts["teams"].items()):
        if not key.startswith("jc-team:") or not team.get("official_team_id"):
            continue
        verdict = verdicts.get(team["official_team_id"])
        if verdict is None:
            verdict = _existing_team_verdict(
                facts,
                tables,
                team,
                key,
                configs=configs,
                countries=countries,
                index=index,
                witness=witness,
            )
        if verdict.get("basis") == SAME_CLUB_BASIS and str(verdict["project_team_id"]) != str(
            team.get("project_team_id") or ""
        ):
            links.append(
                link_decision(
                    team,
                    verdict,
                    tables,
                    evidence=_evidence(team),
                    canonical=normalize_team(team["name"]),
                    json_text=_json,
                )
            )
    return dict(
        version=1,
        archive_sha256=facts["summary"]["manifest"]["sha256"],
        decisions=[_entry(team) for _key, team in rows],
        link_decisions=links,
        codes=codes,
        existing_team_verdicts=verdicts,
        # 竞彩从未开售的来源独有球队（预备队）：只列事实与阻塞原因，逐队待所有者批准。
        provider_only_candidates=[
            row
            for code in codes
            if registry["providers"]["apif"][code]["config"].get("current_season")
            for row in provider_only_candidates(facts, code)
        ],
        production_writes=0,
    )


def _approval(decision, action=None):
    """``action`` pins the list a decision sits in; a receipt may carry either action."""
    allowed = {action} if action else {CREATE_ACTION, LINK_ACTION}
    if decision.get("status") != "approved" or decision.get("action") not in allowed:
        raise ValueError("Creation requires explicit approved decision")
    validate_approvals({"creation": {"1": decision}}, require_metadata=True)
    if (
        decision.get("approved_by") != "qw5563680"
        or not isinstance(decision.get("review_url"), str)
        or not re.fullmatch(
            r"https://github\.com/qw5563680/FOOTBALL/pull/[1-9][0-9]*(?:#[A-Za-z0-9_-]+)?",
            decision["review_url"],
        )
        or _utc(decision["approved_utc"]) > datetime.now(UTC)
        or not re.fullmatch(r"[1-9][0-9]*", str(decision.get("official_team_id", "")))
    ):
        raise ValueError("Creation requires actual owner approval and review")


def _approved_subset(decision, expected, field):
    """Approved evidence must be self-consistent and a subset of the current evidence:
    matches added after approval must not invalidate it."""
    approved = decision.get(field)
    if not approved or sha256(_json(approved).encode()).hexdigest() != decision.get(
        field + "_sha256"
    ):
        raise ValueError("Creation evidence digest invalid")
    current = {_json(row) for row in expected[field]}
    if any(_json(row) not in current for row in approved):
        raise ValueError("Approved evidence is not a subset of current evidence")


def _read_receipts(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        if reader.fieldnames != list(FIELDS):
            raise ValueError("Creation audit schema invalid")
        rows = list(reader)
    if any(None in row or None in row.values() for row in rows):
        raise ValueError("Creation audit row invalid")
    if len({r["official_team_id"] for r in rows}) != len(rows) or len(
        {r["project_team_id"] for r in rows}
    ) != len(rows):
        raise ValueError("Creation audit identity not unique")
    return rows


def _verify_creation_originals(root, decision, manifest):
    from zucai.identity_gate_evidence import proof

    evidence = decision["evidence"]
    if not evidence or sha256(_json(evidence).encode()).hexdigest() != decision["evidence_sha256"]:
        raise ValueError("Creation evidence digest invalid")
    for observation in evidence:
        matched = False
        side = observation["side"]
        if side not in {"home", "away"}:
            raise ValueError("Creation evidence side invalid")
        for reference in observation["originals"]:
            if manifest["files"].get(reference["path"]) != reference["sha256"]:
                raise ValueError("Creation original missing from archive manifest")
            raw = proof(root, reference)
            pointer = reference.get("pointer", "")
            if not pointer.startswith("/value/"):
                continue
            value = json.loads(raw)
            try:
                for part in pointer.lstrip("/").split("/"):
                    value = value[int(part)] if isinstance(value, list) else value[part]
            except (KeyError, IndexError, ValueError, TypeError) as exc:
                raise ValueError("Creation original pointer invalid") from exc
            official = value.get(side + "TeamId", value.get("sporttery" + side.title() + "TeamId"))
            match_id = value.get("matchId", value.get("sportteryMatchId"))
            if (
                str(official) == decision["official_team_id"]
                and "jc:" + str(match_id) == observation["match_id"]
            ):
                matched = True
        if not matched:
            raise ValueError("Creation original does not prove numeric match/team/side")


def absorbed_team_redirects(root, tables):
    """Absorbed stored team id -> kept id, from same-club receipts in the archive.

    Only ids that no longer exist in the current team table are redirected; the
    receipts themselves are authenticated in ``apply_created_identities``.
    """
    path = Path(root) / RECEIPTS
    if not path.exists():
        return {}
    present = {int(row["id"]) for row in tables["teams"]}
    redirects = {}
    for receipt in _read_receipts(path):
        decision = json.loads(receipt["decision_json"])
        absorbed = decision.get("absorb_project_team_id")
        if decision.get("action") != LINK_ACTION or not absorbed:
            continue
        absorbed, kept = int(absorbed), int(receipt["project_team_id"])
        if absorbed in present or kept not in present or absorbed in redirects:
            raise ValueError("Same-club receipt contradicts the current team inventory")
        redirects[absorbed] = kept
    return redirects


def apply_created_identities(root, teams, tables, manifest):
    """Restore the numeric association only with its authenticated approval receipt."""
    path = Path(root) / RECEIPTS
    if not path.exists():
        return
    from zucai.identity_gate_evidence import proof

    document = json.loads((Path(root) / "feed_manifest.json").read_bytes())
    digest = document["files"].get(RECEIPTS)
    if not digest:
        raise ValueError("Unlisted creation audit")
    proof(Path(root), dict(path=RECEIPTS, sha256=digest))
    projects = {str(t["id"]): t for t in tables["teams"]}
    owners = {}
    project_labels = {}
    for tid, project in projects.items():
        aliases = project["aliases"]
        if isinstance(aliases, str):
            aliases = json.loads(aliases)
        labels = {normalize_team(label).casefold() for label in (project["name"], *aliases)}
        project_labels[tid] = labels
        for label in labels:
            owners.setdefault(label, set()).add(tid)
    claimed = set()
    for receipt in _read_receipts(path):
        decision = json.loads(receipt["decision_json"])
        _approval(decision)
        tid = receipt["project_team_id"]
        official = receipt["official_team_id"]
        target = projects.get(tid)
        if target is None:
            raise ValueError("Created canonical team missing")
        aliases = target["aliases"]
        if isinstance(aliases, str):
            aliases = json.loads(aliases)
        if decision["action"] == LINK_ACTION and (
            decision["project_team_id"] != tid
            or decision["previous_name"] not in aliases
            or str(decision.get("absorb_project_team_id") or "") in projects
        ):
            raise ValueError("Same-club receipt/snapshot inconsistent")
        if (
            decision["official_team_id"] != official
            or receipt["canonical"] != decision["canonical"]
            or target["name"] != decision["canonical"]
            or f"jc:team:{official}" not in aliases
            or not _utc(decision["approved_utc"])
            <= _utc(receipt["created_utc"])
            <= _utc(document["created_utc"])
        ):
            raise ValueError("Created identity receipt/snapshot inconsistent")
        marker = "jc:team:" + official
        markers = {label for label in project_labels[tid] if label.startswith("jc:team:")}
        if owners.get(marker) != {tid} or markers != {marker}:
            raise ValueError("Created official numeric marker has conflicting current owners")
        if tid in claimed:
            raise ValueError("Created project identity claimed twice")
        claimed.add(tid)
        _verify_creation_originals(Path(root), decision, document)
        team = teams.get("jc-team:" + official)
        if team is None:
            continue  # Reader may select other competitions.
        if team["ambiguous"] or team.get("project_team_id") not in (None, tid):
            raise ValueError("Created identity contradicts current numbered evidence")
        if any(
            t.get("project_team_id") == tid and key != "jc-team:" + official
            for key, t in teams.items()
        ):
            raise ValueError("Created project has another official owner")
        team.update(
            project_team_id=tid, project_name=target["name"], identity_status="existing_numbered"
        )
        team["project_identity_evidence"].append(
            dict(
                kind=(
                    "approved_same_club_link"
                    if decision["action"] == LINK_ACTION
                    else "approved_numeric_creation"
                ),
                manifest=manifest,
                path=RECEIPTS,
                sha256=digest,
                decision=decision,
                created_utc=receipt["created_utc"],
            )
        )


def apply_approvals(database, archive, approvals):
    from zucai.identity_binding_archive import _tables, load_archive

    database, archive = Path(database), Path(archive)
    if not database.is_file():
        raise ValueError("Creation requires an existing restored database")
    document = json.loads(Path(approvals).read_bytes())
    tables, _ = _tables(archive)
    if document.get("version") != 1:
        raise ValueError("Creation decision document version invalid")
    # archive_sha256 is descriptive only: approvals must survive unrelated
    # daily archive rewrites, so every decision is re-verified against the
    # current archive below instead of binding the whole archive digest.
    decisions = document.get("decisions")
    link_decisions = document.get("link_decisions", [])
    if (
        not isinstance(decisions, list)
        or not isinstance(link_decisions, list)
        or not (decisions or link_decisions)
    ):
        raise ValueError("Explicit creation decisions required")
    from zucai.league_registry import load_registry

    registry = load_registry()
    allowed = {c for c, e in registry["providers"]["apif"].items() if e["status"] == "verified"}
    codes = document.get("codes")
    if not isinstance(codes, list) or not codes or not set(codes) <= allowed:
        raise ValueError("Creation decision route scope invalid")
    facts = load_archive(archive, leagues=codes)
    verdict_configs, verdict_countries, verdict_index, verdict_witness = _verdict_context(
        facts, tables, registry
    )
    verdicts = {}
    for key, team in facts["teams"].items():
        if key.startswith("jc-team:") and team.get("official_team_id"):
            verdicts[team["official_team_id"]] = _existing_team_verdict(
                facts,
                tables,
                team,
                key,
                configs=verdict_configs,
                countries=verdict_countries,
                index=verdict_index,
                witness=verdict_witness,
            )
    audit = database.parent / RECEIPTS
    before = audit.read_bytes() if audit.exists() else None
    receipts = _read_receipts(audit)
    receipt_by_id = {r["official_team_id"]: r for r in receipts}
    seen = set()
    pending, pending_links = [], []
    for group, action, queue in (
        (decisions, CREATE_ACTION, pending),
        (link_decisions, LINK_ACTION, pending_links),
    ):
        for decision in group:
            _approval(decision, action)
            official = decision["official_team_id"]
            if official in seen:
                raise ValueError("Duplicate creation decision")
            seen.add(official)
            existing = receipt_by_id.get(official)
            if existing is not None:
                if json.loads(existing["decision_json"]) != decision:
                    raise ValueError("Existing creation receipt differs from approved decision")
                # Identical receipt: already applied, count and skip, no re-verification.
                continue
            queue.append(decision)
    for decision in pending:
        team = facts["teams"].get("jc-team:" + decision["official_team_id"])
        if team is None:
            raise ValueError("Official numeric evidence missing")
        # T68 第八节第 7 项：apply 在运行时重新计算既有球队结论；
        # 不是「新建」的一律拒绝创建。
        verdict = verdicts.get(decision["official_team_id"])
        if verdict is None:
            verdict = _existing_team_verdict(
                facts,
                tables,
                team,
                "jc-team:" + decision["official_team_id"],
                configs=verdict_configs,
                countries=verdict_countries,
                index=verdict_index,
                witness=verdict_witness,
            )
        if verdict["status"] != "new":
            raise ValueError(
                f"创建被拒绝：官方编号 {decision['official_team_id']} "
                f"结论为 {verdict['status']}（{verdict.get('basis')}）；"
                f"{verdict.get('说明') or '；'.join(verdict.get('missing', []))}"
            )
        expected = _entry(team)
        if expected["blockers"] or any(
            decision.get(k) != expected[k] for k in ("canonical", "category", "official_names")
        ):
            raise ValueError("Creation evidence/category/name invalid or identity already exists")
        _approved_subset(decision, expected, "evidence")
    # 「同一家俱乐部」：运行时重新计算结论与提案，批准内容必须与当前事实逐项一致。
    for decision in pending_links:
        official = decision["official_team_id"]
        team = facts["teams"].get("jc-team:" + official)
        if team is None:
            raise ValueError("Official numeric evidence missing")
        verdict = verdicts.get(official) or {"status": "missing"}
        if verdict["status"] != "existing" or verdict.get("basis") != SAME_CLUB_BASIS:
            raise ValueError(
                f"关联被拒绝：官方编号 {official} 结论为 "
                f"{verdict['status']}（{verdict.get('basis')}）"
            )
        expected = link_decision(
            team,
            verdict,
            tables,
            evidence=_evidence(team),
            canonical=normalize_team(team["name"]),
            json_text=_json,
        )
        if expected["blockers"] or any(
            decision.get(k) != expected[k]
            for k in (
                "canonical",
                "category",
                "official_names",
                "project_team_id",
                "previous_name",
                "provider_team_id",
                "absorb_project_team_id",
            )
        ):
            raise ValueError(
                f"关联被拒绝：官方编号 {official} 的批准内容与当前事实不一致"
                f"（{'、'.join(expected['blockers']) or '字段不同'}）"
            )
        _approved_subset(decision, expected, "evidence")
        approved_witness = decision.get("witness")
        if (
            not isinstance(approved_witness, dict)
            or sha256(_json(approved_witness).encode()).hexdigest()
            != decision.get("witness_sha256")
            or not approved_witness.get("witnesses")
        ):
            raise ValueError("Link witness digest invalid")
        current = {_json(row) for row in expected["witness"]["witnesses"]}
        if any(_json(row) not in current for row in approved_witness["witnesses"]):
            raise ValueError("Approved witness is not a subset of current witness")
    # The restored DB must carry the originals that its exported receipt will cite.
    # Merely pointing at an external archive would produce a non-portable approval.
    from zucai.cloud_feeds import STATE_FILE

    source_manifest = json.loads((archive / "feed_manifest.json").read_bytes())
    for decision in pending + pending_links:
        for observation in decision["evidence"]:
            for reference in observation["originals"]:
                for name in (reference["path"], reference["path"] + ".meta.json"):
                    if not re.fullmatch(STATE_FILE, name):
                        raise ValueError("Creation original is outside feeds export policy")
        try:
            _verify_creation_originals(database.parent, decision, source_manifest)
        except (KeyError, OSError) as exc:
            raise ValueError("Creation originals must be restored beside the database") from exc
        # The approval must not precede the capture of any original it cites.
        for observation in decision["evidence"]:
            for reference in observation["originals"]:
                meta_path = database.parent / (reference["path"] + ".meta.json")
                try:
                    meta = json.loads(meta_path.read_bytes())
                except (OSError, ValueError) as exc:
                    raise ValueError(
                        "Creation original metadata missing beside the database"
                    ) from exc
                fetched = meta.get("fetched_at_utc")
                if not fetched or _utc(decision["approved_utc"]) < _utc(fetched):
                    raise ValueError("Approval cannot precede original capture")
    created, linked, moved = [], [], {}
    engine = get_engine(database)
    try:
        with session_scope(engine) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            known = list(session.scalars(select(Team)))
            expected_ids = {r["id"] for r in tables["teams"]} | {
                int(r["project_team_id"]) for r in receipts
            }
            if {t.id for t in known} != expected_ids:
                raise ValueError("Database team inventory changed since frozen proposal")
            for row in tables["teams"]:
                target = session.get(Team, row["id"])
                aliases = (
                    json.loads(row["aliases"])
                    if isinstance(row["aliases"], str)
                    else row["aliases"]
                )
                if (target.name, target.aliases) != (row["name"], aliases):
                    raise ValueError("Database team identity changed since frozen proposal")
            stored = list(session.scalars(select(Match)))
            if {m.match_id for m in stored} != {r["match_id"] for r in tables["matches"]}:
                raise ValueError("Database fixture inventory changed since proposal")
            for row in tables["matches"]:
                match = session.get(Match, row["match_id"])
                if any(
                    getattr(match, k) != row[k]
                    for k in ("source", "league", "home_team_id", "away_team_id")
                ) or _utc(match.kickoff_utc) != _utc(row["kickoff_utc"]):
                    raise ValueError("Database fixture identity changed since proposal")
            for decision in pending:
                official = decision["official_team_id"]
                marker = "jc:team:" + official
                labels = {normalize_team(label).casefold() for label in decision["official_names"]}
                labels.update((decision["canonical"].casefold(), marker))
                if any(
                    labels & {normalize_team(s).casefold() for s in (t.name, *t.aliases)}
                    for t in known
                ):
                    raise ValueError(
                        "Canonical name/alias/numeric ID already has an owner; "
                        "review existing identity"
                    )
                target = Team(name=decision["canonical"], aliases=[marker])
                session.add(target)
                session.flush()
                known.append(target)
                receipt = dict(
                    official_team_id=official,
                    project_team_id=str(target.id),
                    canonical=target.name,
                    created_utc=datetime.now(UTC).isoformat(),
                    decision_json=_json(decision),
                )
                receipts.append(receipt)
                created.append(receipt)
            for decision in pending_links:
                official = decision["official_team_id"]
                marker = "jc:team:" + official
                keep = session.get(Team, int(decision["project_team_id"]))
                absorb_id = decision["absorb_project_team_id"]
                absorb_id = int(absorb_id) if absorb_id else None
                if keep is None or any(marker in t.aliases for t in known):
                    raise ValueError("Link target missing or numeric ID already has an owner")
                labels = {normalize_team(label).casefold() for label in decision["official_names"]}
                labels.update((decision["canonical"].casefold(), marker))
                if any(
                    t.id not in (keep.id, absorb_id)
                    and labels & {normalize_team(s).casefold() for s in (t.name, *t.aliases)}
                    for t in known
                ):
                    raise ValueError(
                        "Canonical name/alias/numeric ID already has an owner; "
                        "review existing identity"
                    )
                previous, aliases = keep.name, list(keep.aliases)
                keep_id = keep.id
                if absorb_id is not None:
                    # 只带竞彩比赛的那一行并入保留行：所有引用球队的列一并改指，
                    # 任何唯一性或主客不同的约束被破坏都会让整个事务回滚。
                    for model_table in Base.metadata.sorted_tables:
                        for column in model_table.columns:
                            if not any(k.column.table.name == "teams" for k in column.foreign_keys):
                                continue
                            count = session.execute(
                                model_table.update()
                                .where(column == absorb_id)
                                .values({column.name: keep_id})
                            ).rowcount
                            if count:
                                name = f"{model_table.name}.{column.name}"
                                moved[name] = moved.get(name, 0) + count
                    session.execute(delete(Team).where(Team.id == absorb_id))
                    known = [t for t in known if t.id != absorb_id]
                    session.expire_all()
                    keep = session.get(Team, keep_id)
                keep.name = decision["canonical"]
                keep.aliases = list(dict.fromkeys([*aliases, previous, marker]))
                session.flush()
                receipt = dict(
                    official_team_id=official,
                    project_team_id=str(keep_id),
                    canonical=keep.name,
                    created_utc=datetime.now(UTC).isoformat(),
                    decision_json=_json(decision),
                )
                receipts.append(receipt)
                linked.append(receipt)
            if created or linked:
                stream = io.StringIO(newline="")
                writer = csv.DictWriter(stream, FIELDS)
                writer.writeheader()
                writer.writerows(receipts)
                audit.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write(audit, stream.getvalue().encode())
    except Exception:
        if before is None:
            audit.unlink(missing_ok=True)
        else:
            _atomic_write(audit, before)
        raise
    finally:
        engine.dispose()
    return dict(
        created=len(created),
        linked=len(linked),
        already_applied=len(decisions) + len(link_decisions) - len(created) - len(linked),
        records=created + linked,
        references_moved=moved,
        matches_changed=sum(n for name, n in moved.items() if name.startswith("matches.")),
        formal_bindings_changed=0,
        daily_changed=False,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["plan", "apply"])
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--codes")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--decisions", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.operation == "plan":
        result = propose(args.archive, codes=args.codes.split(",") if args.codes else None)
    else:
        if args.db is None or args.decisions is None:
            parser.error("apply requires --db and --decisions")
        result = apply_approvals(args.db, args.archive, args.decisions)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(args.out, (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode())


if __name__ == "__main__":
    main()
