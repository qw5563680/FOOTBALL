"""List existing identity collisions for later review; never merge project teams."""

from collections import defaultdict


def duplicate_register(inputs, result):
    groups, evidence = defaultdict(set), defaultdict(list)
    names = {
        str(t["project_team_id"]): t.get("project_name") or t["name"]
        for t in inputs["teams"].values()
        if t.get("project_team_id")
    }
    for team in inputs["teams"].values():
        if not team.get("official_team_id"):
            continue
        source = "jc:" + team["official_team_id"]
        for proof in team.get("project_identity_evidence", []):
            for side, official in (proof.get("official_ids") or {}).items():
                if official == team["official_team_id"]:
                    groups[source].add((proof.get("project_ids") or {})[side])
                    evidence[source].append(proof)
    proposed = {}
    for conflict in result.get("existing_identity_conflicts", []):
        source = "apif:" + conflict["provider_team_id"]
        keys = set(conflict["existing_identity_keys"]) | {
            r["canonical_team_id"] for r in conflict["observations"]
        }
        for key in sorted(keys):
            team = inputs["teams"].get(key, {})
            tid = team.get("project_team_id")
            identifier = tid or key
            groups[source].add(identifier)
            if not tid:
                proposed[identifier] = dict(
                    identity_key=key,
                    project_team_id=None,
                    official_team_id=team.get("official_team_id"),
                    name=team.get("name", key),
                )
        evidence[source].append(dict(kind="independent_unique_numbered_conflict", **conflict))
    # Existing formal targets and occupied provider aliases are independently numbered claims.
    for code, entries in inputs["formal"].items():
        for pid, entry in entries.items():
            if entry.get("status") not in {"approved", "auto_verified"}:
                continue
            for tid, name in names.items():
                if name == entry.get("canonical"):
                    source = "apif:" + str(pid)
                    groups[source].add(tid)
                    evidence[source].append(
                        dict(
                            kind="existing_formal_target",
                            league=code,
                            provider_team_id=pid,
                            entry=entry,
                        )
                    )
    for pid, identities in inputs.get("occupied", {}).items():
        for key in identities:
            team = inputs["teams"].get(str(key), {})
            if team.get("project_team_id"):
                source = "apif:" + str(pid)
                groups[source].add(team["project_team_id"])
                evidence[source].append(
                    dict(kind="existing_numeric_alias", identity_key=key, team=team)
                )
    rows = [
        dict(
            source_id=source,
            identities=[
                proposed.get(tid, dict(project_team_id=tid, name=names.get(tid)))
                for tid in sorted(ids)
            ],
            evidence=evidence[source],
            status="review_required_no_merge",
        )
        for source, ids in sorted(groups.items())
        if len(ids) > 1
    ]
    return dict(
        rows=rows,
        count=len(rows),
        production_writes=0,
        warning="共享数字编号的既有身份待核清单；不是自动认定同队，更不执行合并。",
    )


def duplicate_markdown(register):
    lines = [
        "# 既有身份重复嫌疑：另案核对，本 PR 不合并",
        "",
        register["warning"],
        "",
        "| 共享来源编号 | 项目球队编号与名称 | 结论 |",
        "| --- | --- | --- |",
    ]
    for row in register["rows"]:
        identities = " / ".join(
            str(t["project_team_id"] or t.get("identity_key"))
            + " "
            + str(t["name"]).replace("|", "\\|")
            for t in row["identities"]
        )
        lines.append(f"| {row['source_id']} | {identities} | 待人工核对，未合并 |")
    lines.extend(
        [
            "",
            "全部数字见证、原件路径与哈希见同目录 `existing_identity_duplicates.json`"
            "（打包时可为无损 `.json.gz`）。",
            "",
        ]
    )
    return "\n".join(lines)
