"""Deterministic, owner-readable views of the numbered proposal register."""

import json
from collections import defaultdict
from html import escape

REASONS_ZH = {
    "cancelled_fixture_not_identity_witness": (
        "双方均记录为取消且没有赛果，这场不作身份见证；不影响其他独立完赛证据"
    ),
    "not_bidirectionally_unique": "同时有多场时间和主客方向仍可能对应的比赛，不能靠比分挑选",
    "incomplete_competitor_window": "前后三小时的全部竞争比赛或准确时刻证据不完整",
    "global_provider_id_conflict": "同一个接口球队编号对应了不同身份，须核对原件",
    "withdrawn_conflict": "身份证据互相冲突，已撤下这条候选",
    "withdrawn_dependency": "用作编号锚的候选已撤下，因此这条候选也撤下",
    "canonical_target_not_unique": "规范球队身份有冲突或不能唯一确定",
    "category_evidence_missing": "球队类别没有唯一且完整的登记证据",
    "official_project_identity_conflict": "官方编号与现有项目球队的数字关联互相冲突",
    "regular_time_score_mismatch": "已经按时间和方向找到的比赛，常规时间比分不一致",
    "no_verified_numbered_witness": "还没有满足全部编号、时间、方向规则的比赛见证",
    "category_conflict": "队名中的预备队、青年或女足标记与赛事类别不一致，须单独核对",
    "current_snapshot_identity_conflict": "当前库的同场记录与官方原件或已归档快照不一致",
    "no_provider_in_window": "前后三小时内没有找到可比较的接口比赛",
    "kickoff_not_exact": "两份记录的开球时刻没有逐秒一致",
    "orientation_unproven_draw_only": "只有平局见证且没有独立对手编号锚，不能确定主客方向",
    "missing_regular_time_score": "缺少明确的常规时间比分",
    "not_finished_regular_time": "记录不是可核实的已完赛常规时间结果",
    "missing_kickoff_evidence": "缺少准确开球时刻，日期或占位时间不能作证",
    "conflicting_fixture_originals": "同一比赛的原件内容互相矛盾",
    "conflicting_jc_kickoff_evidence": "官方开球时间或主客编号证据互相矛盾",
    "canonical_has_other_provider_id": "该规范球队在同一赛事已被另一个接口编号占用",
    "competition_mapping_unverified": "双方赛事编号的对应关系尚未核实",
    "linked_entity_evidence_conflict": "已有同场关联组件中的记录不一致",
    "rejected": "该候选已被明确否决",
    "category_approval_pending": "赛事类别登记仍待审查和所有者核准",
    "canonical_identity_creation_pending": "需按官方编号创建规范身份，逐条待核准",
}


def _witness(proof, official_id):
    source, official = proof.get("apif"), proof.get("counterpart")
    if not source or not official:
        return None
    side = next(
        (s for s in ("home", "away") if official.get(f"{s}_source_id") == official_id), None
    )
    if side is None:
        return None
    opponent = "away" if side == "home" else "home"
    return dict(
        apif_match_id=source["match_id"],
        official_match_id=official["match_id"],
        date=official.get("match_date") or str(official.get("kickoff_utc", ""))[:10],
        kickoff_utc=official.get("kickoff_utc"),
        side=side,
        score=official.get("score"),
        official_opponent=dict(
            name=official.get(f"{opponent}_name", ""),
            official_team_id=official.get(f"{opponent}_source_id"),
        ),
        apif_opponent=dict(
            name=source.get(f"{opponent}_name", ""), provider_team_id=source[opponent]
        ),
        apif_provenance=source["provenance"],
        official_provenance=official["provenance"],
    )


def enrich_register(rows, inputs, result):
    candidates = [e for entries in result["apif"].values() for e in entries.values()]
    candidates += result.get("existing_candidate_reviews", [])
    sources, issues = defaultdict(list), defaultdict(list)
    for source in inputs["apif"]:
        sources[source["match_id"]].append(source)
    for issue in result["unverified"]:
        if issue.get("counterpart_id"):
            issues[issue["counterpart_id"]].append(issue)
    for row in rows:
        row["api_candidates"] = []
        for candidate in candidates:
            if (
                str(candidate.get("provider_competition_id")) != row["competition_id"]
                or candidate.get("official_team_id") != row["official_team_id"]
            ):
                continue
            row["api_candidates"].append(
                dict(
                    candidate_id=candidate["candidate_id"],
                    provider_team_id=candidate["provider_team_id"],
                    name=candidate.get("name", ""),
                    evidence_status=candidate["evidence_status"],
                    witnesses=[
                        w
                        for proof in candidate.get("evidence", [])
                        if (w := _witness(proof, row["official_team_id"]))
                    ],
                )
            )
        possible = {}
        for mid in row["official_match_ids"]:
            for issue in issues[mid]:
                for source in sources[issue.get("apif_id")]:
                    item = dict(
                        official_match_id=mid,
                        apif_match_id=source["match_id"],
                        kickoff_utc=source.get("kickoff_utc"),
                        score=source.get("score"),
                        home=dict(
                            name=source.get("home_name", ""), provider_team_id=source["home"]
                        ),
                        away=dict(
                            name=source.get("away_name", ""), provider_team_id=source["away"]
                        ),
                        reasons=issue["reasons"],
                        reasons_zh=[REASONS_ZH.get(r, r) for r in issue["reasons"]],
                        provenance=source["provenance"],
                    )
                    possible[json.dumps(item, sort_keys=True, ensure_ascii=False)] = item
        row["possible_matches"] = list(possible.values())
        row["reasons_zh"] = [REASONS_ZH.get(r, r) for r in row["reasons"]]
        row["approval_gaps_zh"] = [REASONS_ZH.get(r, r) for r in row["approval_gaps"]]
        row["conclusion_zh"] = (
            "比赛证据一致，仍待核准：" + "；".join(row["approval_gaps_zh"])
            if row["candidate_ids"]
            else "待核：" + "；".join(row["reasons_zh"])
        )


def owner_html(register):
    """Plain local HTML: expandable teams, escaped data, no requests or scripts."""
    groups = defaultdict(list)
    for row in register["rows"]:
        groups[row["league"]].append(row)
    parts = [
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">',
        "<title>T68 球队身份逐项核对</title>",
        "<style>body{font:16px/1.6 system-ui;max-width:1100px;margin:2em auto;padding:0 1em}"
        "details{border:1px solid #ccd;padding:.7em;margin:.5em 0}summary{cursor:pointer}"
        "pre{white-space:pre-wrap;word-break:break-word;font-size:12px}li{margin:.5em 0}</style>",
        "<h1>球队身份逐项核对</h1><p>全部都是待审提案，不是正式绑定。"
        "比分只用于核验已经由编号、时间和方向找到的比赛，不用于挑选候选。</p>",
        f"<p>A范围：{register['a_official_ids']}个官方编号，"
        f"{register['a_competition_items']}个赛事球队项。</p>",
    ]
    for league, rows in sorted(groups.items()):
        parts.append(f"<h2>{escape(league)}</h2>")
        for row in rows:
            title = f"{' / '.join(row['official_names'])}（官方编号 {row['official_team_id']}）"
            parts.append(
                f"<details><summary>{escape(title)} — {escape(row['conclusion_zh'])}</summary>"
            )
            if row["identity_proposal"]:
                parts.append(
                    "<p>创建身份提案：使用这个官方编号和官方名称新建规范球队，待逐条核准。</p>"
                )
            for candidate in row["api_candidates"]:
                label = f"{candidate['name']}（接口编号 {candidate['provider_team_id']}）"
                parts.append(f"<h3>{escape(label)}</h3><ul>")
                for witness in candidate["witnesses"]:
                    score = ":".join(map(str, witness["score"] or []))
                    label = (
                        f"{witness['date']} 对手 {witness['official_opponent']['name']}"
                        f"（官方编号 {witness['official_opponent']['official_team_id']}），"
                        f"常规时间主客比分 {score}；"
                        f"{witness['official_match_id']} ↔ {witness['apif_match_id']}"
                    )
                    parts.append(
                        f"<li>{escape(label)}<details><summary>原始记录：文件、哈希、位置</summary><pre>"
                    )
                    parts.append(escape(json.dumps(witness, ensure_ascii=False, indent=2)))
                    parts.append("</pre></details></li>")
                parts.append("</ul>")
            if row["possible_matches"]:
                parts.append(
                    "<h3>仍待核对的全部可能比赛</h3><p>以下逐项保留，不用比分选一个。</p><ul>"
                )
                for possible in row["possible_matches"]:
                    label = (
                        f"{possible['kickoff_utc']} {possible['home']['name']}"
                        f"（{possible['home']['provider_team_id']}）— {possible['away']['name']}"
                        f"（{possible['away']['provider_team_id']}），{possible['apif_match_id']}"
                    )
                    parts.append(
                        f"<li>{escape(label)}：{escape('；'.join(possible['reasons_zh']))}"
                        "<details><summary>查看记录</summary><pre>"
                        f"{escape(json.dumps(possible, ensure_ascii=False, indent=2))}"
                        "</pre></details></li>"
                    )
                parts.append("</ul>")
            parts.append("</details>")
    parts.append("</html>\n")
    return "\n".join(parts)


def _md(value):
    return escape(str(value)).replace("\n", " ").replace("|", "\\|").replace("*", "\\*")


def _references(proofs):
    refs = []
    for proof in proofs:
        if proof.get("path") and proof.get("sha256"):
            refs.append(
                f"`{proof['path']}` · SHA256 `{proof['sha256']}`"
                f" · 位置 `{proof.get('pointer', '/')}`"
            )
        if proof.get("matches_path"):
            refs.append(
                f"`{proof['matches_path']}` · SHA256 `{proof.get('matches_sha256')}`"
                f" · 位置 `{proof.get('matches_row_pointer', '/')}`"
            )
    return sorted(set(refs))


def owner_markdown(register):
    """A small index and one GitHub-readable file per competition."""
    groups = defaultdict(list)
    for row in register["rows"]:
        groups[row["competition_id"]].append(row)
    index = [
        "# T68 球队身份逐项核对",
        "",
        "全部为待审提案，不能用于正式绑定或开启每日采集。点赛事，再点球队目录。",
        "",
        f"A 范围：{register['a_official_ids']} 个官方编号，"
        f"{register['a_competition_items']} 个赛事球队项。",
        "",
    ]
    pages = {}
    for code, rows in sorted(groups.items(), key=lambda item: int(item[0])):
        league = rows[0]["league"]
        index.append(f"- [{_md(league)}](owner-review/{code}.md)：{len(rows)} 个球队项")
        page = [
            f"# {_md(league)} · 接口赛事编号 {code}",
            "",
            "类别是待核准的赛事默认值；预备队、青年队和身份冲突逐队核对。比分不用于挑选候选。",
            "",
        ]
        page.extend(
            f"- [{_md(' / '.join(r['official_names']))}（{r['official_team_id']}）]"
            f"(#jc-{r['official_team_id']})"
            for r in rows
        )
        for row in rows:
            project = row["project_team_id"] or "尚无可靠数字关联，创建规范身份提案待核准"
            page.extend(
                [
                    "",
                    f'<a id="jc-{row["official_team_id"]}"></a>',
                    f"## {_md(' / '.join(row['official_names']))}"
                    f" · 官方编号 {row['official_team_id']}",
                    "",
                    row["conclusion_zh"],
                    "",
                    f"项目球队编号：{project}。",
                    "",
                ]
            )
            for candidate in row["api_candidates"]:
                page.extend(
                    [
                        f"### 接口球队 {_md(candidate['name'])}"
                        f" · 编号 {candidate['provider_team_id']}",
                        "",
                    ]
                )
                for witness in candidate["witnesses"]:
                    score = ":".join(map(str, witness["score"] or []))
                    opponent = witness["official_opponent"]
                    page.append(
                        f"- {witness['date']}：对手 {_md(opponent['name'])}"
                        f"（官方编号 {opponent['official_team_id']}），常规时间主客比分 {score}；"
                        f"`{witness['official_match_id']}` ↔ `{witness['apif_match_id']}`。"
                    )
                    for reference in _references(
                        [*witness["apif_provenance"], *witness["official_provenance"]]
                    ):
                        page.append(f"  - {reference}")
                page.append("")
            if row["possible_matches"]:
                page.extend(["### 全部待核对比赛（不用比分挑选）", ""])
                for possible in row["possible_matches"]:
                    score = ":".join(map(str, possible["score"] or [])) or "比分缺失"
                    page.append(
                        f"- `{possible['apif_match_id']}` · {possible['kickoff_utc']} · "
                        f"{_md(possible['home']['name'])}"
                        f"（{possible['home']['provider_team_id']}）— "
                        f"{_md(possible['away']['name'])}（{possible['away']['provider_team_id']}）"
                        f"· 主客比分 {score}；对应官方场次 `{possible['official_match_id']}`。"
                    )
                    page.append("  - " + "；".join(possible["reasons_zh"]))
                    for reference in _references(possible["provenance"]):
                        page.append(f"  - {reference}")
            page.append("")
        pages[f"owner-review/{code}.md"] = "\n".join(page) + "\n"
    return {"owner-review.md": "\n".join(index) + "\n", **pages}
